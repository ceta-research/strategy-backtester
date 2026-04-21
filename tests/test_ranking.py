"""Tests for engine/ranking.py and scanner per-bar behavior.

Covers Phase 3 audit items P3.1, P3.2, P3.3, P3.7 from
docs/AUDIT_FINDINGS.md. Each test locks in a convention that was
either documented or fixed during the Phase 3 pass.

  P3.1: sort_orders_by_highest_avg_txn uses PREV-DAY volume × average_price
        (look-ahead-safe for ranking entries). ATO parity confirmed.
  P3.2: sort_orders_by_highest_gainer formula matches ATO's
        (prev_close - prev_close.shift(N)) / prev_close.shift(N).
  P3.3: remove_overlapping_orders is deterministic across runs.
        group_by("instrument", maintain_order=True) is the load-bearing bit.
  P3.7: scanner.price_threshold is a PER-BAR filter, not "stock ever exceeded".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.ranking import (
    calculate_daywise_instrument_score,
    sort_orders_by_highest_avg_txn,
    sort_orders_by_highest_gainer,
)
from engine.scanner import process as scanner_process
from engine.constants import SECONDS_IN_ONE_DAY


BASE_EPOCH = 1577836800  # 2020-01-01


def _tick(instrument, days, closes, volumes=None, avg_prices=None):
    """Build a tiny tick-data frame for one instrument across N days."""
    rows = []
    for i, d in enumerate(days):
        rows.append({
            "date_epoch": BASE_EPOCH + d * SECONDS_IN_ONE_DAY,
            "instrument": instrument,
            "close": closes[i],
            "volume": (volumes[i] if volumes else 1_000_000),
            "average_price": (avg_prices[i] if avg_prices else closes[i]),
        })
    return pl.DataFrame(rows)


# ── P3.1: avg_txn uses PREV-DAY values ───────────────────────────────────

class TestAvgTxnPrevDay(unittest.TestCase):
    """Ranking must NOT peek at the current bar's volume; it must use the
    previous bar's values. This locks in the shift(1) behavior in
    ranking.py:sort_orders_by_highest_avg_txn."""

    def test_rank_driven_by_prev_day_not_same_day(self):
        # Two instruments, 4 days. On day 3 (entry day), SAME-DAY values
        # reverse the ordering vs PREV-DAY values — so we can distinguish
        # the two conventions unambiguously.
        # Day index:          0        1        2        3
        # SYM_A avg_price:  10       10       10      100  (spike on day 3)
        # SYM_A volume:    100      100      100     1000  (spike on day 3)
        # SYM_B avg_price: 100      100      100       10  (drop on day 3)
        # SYM_B volume:   1000     1000     1000      100  (drop on day 3)
        days = [0, 1, 2, 3]
        df_a = _tick("NSE:A", days,
                     closes=[10, 10, 10, 100],
                     volumes=[100, 100, 100, 1000],
                     avg_prices=[10, 10, 10, 100])
        df_b = _tick("NSE:B", days,
                     closes=[100, 100, 100, 10],
                     volumes=[1000, 1000, 1000, 100],
                     avg_prices=[100, 100, 100, 10])
        df_tick = pl.concat([df_a, df_b])

        # Order entry on day 3 for both.
        entry_epoch = BASE_EPOCH + 3 * SECONDS_IN_ONE_DAY
        df_orders = pl.DataFrame([
            {"instrument": "NSE:A", "entry_epoch": entry_epoch},
            {"instrument": "NSE:B", "entry_epoch": entry_epoch},
        ])

        # With PREV-DAY (shift 1), B outranks A on day 3 (B's prev-day
        # avg_txn = 1000*100 = 100K >> A's 100*10 = 1K).
        # With SAME-DAY, A outranks B on day 3 (A's same-day = 1000*100 =
        # 100K >> B's 100*10 = 1K). Opposite orderings.
        out = sort_orders_by_highest_avg_txn(df_orders, df_tick, order_ranking_window_days=1)
        insts = out["instrument"].to_list()
        # Rank 1 (highest avg_txn) first.
        self.assertEqual(insts[0], "NSE:B",
                         "Expected PREV-DAY ranking: NSE:B first. "
                         "If NSE:A appears first, ranking is using same-day "
                         "values (look-ahead bias).")


# ── P3.2: highest_gainer formula matches ATO ────────────────────────────

class TestHighestGainerFormula(unittest.TestCase):
    """Rank = (prev_close - prev_close.shift(N)) / prev_close.shift(N).
    Matches ATO_Simulator/util.py:281-283."""

    def test_five_day_gainer_ordering(self):
        # 3 instruments, each with a known 5-day return PRIOR to entry day.
        # Day 0..5. Entry on day 5. N = 2.
        # For entry on day 5:
        #   prev_close = close[day 4]
        #   ref_close  = close[day 4].shift(2) = close[day 2]
        #   rank = (close[4] - close[2]) / close[2]
        # Instrument returns over days 2 -> 4:
        #   FAST: 100 -> 150 (+50%)
        #   MID:  100 -> 120 (+20%)
        #   SLOW: 100 -> 110 (+10%)
        days = list(range(6))
        df_fast = _tick("NSE:FAST", days,
                        closes=[90, 95, 100, 120, 150, 155])
        df_mid = _tick("NSE:MID", days,
                       closes=[95, 98, 100, 110, 120, 125])
        df_slow = _tick("NSE:SLOW", days,
                        closes=[99, 100, 100, 105, 110, 111])
        df_tick = pl.concat([df_fast, df_mid, df_slow])

        entry_epoch = BASE_EPOCH + 5 * SECONDS_IN_ONE_DAY
        df_orders = pl.DataFrame([
            {"instrument": "NSE:FAST", "entry_epoch": entry_epoch},
            {"instrument": "NSE:MID", "entry_epoch": entry_epoch},
            {"instrument": "NSE:SLOW", "entry_epoch": entry_epoch},
        ])

        out = sort_orders_by_highest_gainer(df_orders, df_tick, order_ranking_window_days=2)
        insts = out["instrument"].to_list()
        self.assertEqual(insts, ["NSE:FAST", "NSE:MID", "NSE:SLOW"],
                         "Expected descending-gain order based on "
                         "(close[4] - close[2]) / close[2].")


# ── P3.3: remove_overlapping_orders determinism ──────────────────────────

class TestRemoveOverlappingDeterministic(unittest.TestCase):
    """calculate_daywise_instrument_score internally dedups overlapping
    orders via group_by. With maintain_order=True, repeated runs on the
    same (shuffled) input must yield byte-identical output."""

    def _build_orders(self):
        # 4 instruments, each with 2 overlapping orders — inner dedup must
        # drop the second.
        entry0 = BASE_EPOCH
        rows = []
        for i, inst in enumerate(["NSE:A", "NSE:B", "NSE:C", "NSE:D"]):
            rows.append({
                "instrument": inst,
                "entry_epoch": entry0 + i * SECONDS_IN_ONE_DAY,
                "exit_epoch": entry0 + (i + 10) * SECONDS_IN_ONE_DAY,
                "entry_price": 100.0,
                "exit_price": 110.0,
            })
            # Overlapping second order (exit < first order's exit -> dropped).
            rows.append({
                "instrument": inst,
                "entry_epoch": entry0 + (i + 2) * SECONDS_IN_ONE_DAY,
                "exit_epoch": entry0 + (i + 5) * SECONDS_IN_ONE_DAY,
                "entry_price": 100.0,
                "exit_price": 105.0,
            })
        return pl.DataFrame(rows)

    def test_determinism_across_runs(self):
        # Minimal day_wise_close to let calculate_daywise_instrument_score
        # not crash on the unrealized-P&L branch. All orders exit before
        # any scoring epoch the test looks at, so this dict can be empty.
        orders = self._build_orders()
        window_days = 30 * SECONDS_IN_ONE_DAY
        results = []
        for _ in range(10):
            # Feed deliberately-shuffled input each run to stress
            # ordering assumptions.
            shuffled = orders.sample(fraction=1.0, shuffle=True, seed=None)
            out = calculate_daywise_instrument_score(shuffled, {}, window_days)
            results.append(out.to_pandas().to_csv(index=False))
        self.assertEqual(len(set(results)), 1,
                         "calculate_daywise_instrument_score must produce "
                         "identical output across runs. If this fails, "
                         "group_by ordering in remove_overlapping_orders "
                         "is non-deterministic.")


# ── P3.7: scanner price_threshold is per-bar ─────────────────────────────

class TestScannerPriceThresholdPerBar(unittest.TestCase):
    """A stock trading BELOW the threshold on some days and ABOVE on
    others should appear in the scanner output only for the above-threshold
    days."""

    def test_below_threshold_days_excluded(self):
        # One instrument crossing the 50 threshold mid-window.
        # Days 0-4: close = 45 (below). Days 5-9: close = 55 (above).
        days = list(range(10))
        closes = [45] * 5 + [55] * 5
        df = _tick("NSE:X", days, closes=closes,
                   volumes=[10_000_000] * 10,
                   avg_prices=closes)
        # Add required columns that scanner.process expects.
        df = df.with_columns([
            pl.col("close").alias("open"),
            pl.col("close").alias("high"),
            pl.col("close").alias("low"),
            pl.lit("X").alias("symbol"),
            pl.lit("NSE").alias("exchange"),
        ])

        context = {
            "start_epoch": BASE_EPOCH,
            "static_config": {"start_epoch": BASE_EPOCH},
            "scanner_config_input": {
                "instruments": [[{"exchange": "NSE", "symbols": []}]],
                "price_threshold": [50],
                "avg_day_transaction_threshold": [{"period": 3, "threshold": 1000}],
                "n_day_gain_threshold": [{"n": 1, "threshold": -999}],
            },
        }

        out = scanner_process(context, df)
        # Scanner output should include all days (forward-filled), but only
        # days with close > 50 should have a non-null scanner_config_ids.
        flagged = out.filter(pl.col("scanner_config_ids").is_not_null())
        flagged_closes = flagged["close"].to_list()
        for c in flagged_closes:
            self.assertGreater(c, 50,
                               "scanner_config_ids set on a row where "
                               "close <= price_threshold. Filter is NOT "
                               "per-bar (regression).")
        # At least one flagged row should exist (days 5-9).
        self.assertGreater(len(flagged_closes), 0)


if __name__ == "__main__":
    unittest.main()

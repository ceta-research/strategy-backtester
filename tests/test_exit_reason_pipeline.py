"""End-to-end tests for exit_reason propagation.

Code review (2026-04-21) found that `generate_order_df` was projecting
`exit_reason` out of `df_orders` via a fixed `column_order` list, so
order-generator-path strategies always logged "natural" regardless of the
actual exit condition (anomalous_drop / trailing_stop / end_of_data). These
tests lock in the end-to-end wire.

Coverage:
  - `generate_order_df` preserves `exit_reason` in its output schema and data.
  - An anomalous-drop decision survives the generate_order_df -> simulator ->
    trade_log -> BacktestResult.add_trade path with the correct tag.
  - BacktestResult.add_trade round-trips a non-default exit_reason.
  - Empty df_orders still has the `exit_reason` column in its schema.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.order_generator import OrderGenerationUtil
from engine.simulator import process as simulator_process
from engine.constants import SECONDS_IN_ONE_DAY
from lib.backtest_result import BacktestResult


class TestGenerateOrderDfPreservesExitReason(unittest.TestCase):
    """`generate_order_df` must include `exit_reason` in its output schema
    (both empty and non-empty paths)."""

    def test_empty_schema_includes_exit_reason(self):
        """Empty df_orders must still have the column so downstream code
        can rely on its presence without type errors."""
        util = OrderGenerationUtil(pl.DataFrame())
        util.order_config_mapping = {}  # no orders => empty path
        df = util.generate_order_df()
        self.assertIn("exit_reason", df.columns)
        self.assertEqual(df.schema["exit_reason"], pl.Utf8)

    def test_non_empty_path_preserves_reason(self):
        """When the attrs dict carries exit_reason, the DataFrame does too."""
        util = OrderGenerationUtil(pl.DataFrame())
        util.order_config_mapping = {
            "NSE:TEST": {
                1000: {
                    2000: {
                        "entry_price": 100.0,
                        "exit_price": 80.0,
                        "entry_volume": 1_000_000,
                        "exit_volume": 1_000_000,
                        "scanner_config_ids": "s0",
                        "entry_config_ids": "e0",
                        "exit_config_ids": "x0",
                        "exit_reason": "anomalous_drop",
                    }
                }
            }
        }
        df = util.generate_order_df()
        self.assertIn("exit_reason", df.columns)
        self.assertEqual(df["exit_reason"][0], "anomalous_drop")

    def test_missing_reason_defaults_to_natural(self):
        """Signal generators using walk_forward_exit don't emit exit_reason.
        The DataFrame must still have the column, populated with 'natural'."""
        util = OrderGenerationUtil(pl.DataFrame())
        util.order_config_mapping = {
            "NSE:TEST": {
                1000: {
                    2000: {
                        "entry_price": 100.0,
                        "exit_price": 110.0,
                        "entry_volume": 1_000_000,
                        "exit_volume": 1_000_000,
                        "scanner_config_ids": "s0",
                        "entry_config_ids": "e0",
                        "exit_config_ids": "x0",
                        # no exit_reason
                    }
                }
            }
        }
        df = util.generate_order_df()
        self.assertIn("exit_reason", df.columns)
        self.assertEqual(df["exit_reason"][0], "natural")


class TestSimulatorPropagatesExitReason(unittest.TestCase):
    """Synthetic df_orders with a known exit_reason flows through the
    simulator's trade_log correctly."""

    def test_anomalous_drop_reason_survives_to_trade_log(self):
        start = 1577836800  # 2020-01-01
        end = start + 30 * SECONDS_IN_ONE_DAY
        entry = start + 2 * SECONDS_IN_ONE_DAY
        exit_ = start + 5 * SECONDS_IN_ONE_DAY

        orders = pl.DataFrame([{
            "instrument": "NSE:GAPDOWN",
            "entry_epoch": entry, "exit_epoch": exit_,
            "entry_price": 100.0,
            "exit_price": 80.0,  # anomalous_drop haircut
            "scanner_config_ids": "s0",
            "entry_config_ids": "e0",
            "exit_config_ids": "x0",
            "exit_reason": "anomalous_drop",
        }])

        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = {e: {"NSE:GAPDOWN": {"close": 100.0, "avg_txn": 10_000_000}}
                 for e in mtm_epochs}

        context = {
            "start_margin": 1_000_000, "start_epoch": start, "end_epoch": end,
            "slippage_rate": 0.0005,
        }
        sim_cfg = {"max_positions": 5, "max_positions_per_instrument": 1}

        _, _, _, _, trade_log = simulator_process(
            context, orders, stats, {}, sim_cfg, "reason_test")

        self.assertEqual(len(trade_log), 1)
        self.assertEqual(trade_log[0]["exit_reason"], "anomalous_drop")

    def test_trailing_stop_reason_survives(self):
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        entry = start + 2 * SECONDS_IN_ONE_DAY
        exit_ = start + 5 * SECONDS_IN_ONE_DAY

        orders = pl.DataFrame([{
            "instrument": "NSE:TSL",
            "entry_epoch": entry, "exit_epoch": exit_,
            "entry_price": 100.0, "exit_price": 95.0,
            "scanner_config_ids": "s0",
            "entry_config_ids": "e0", "exit_config_ids": "x0",
            "exit_reason": "trailing_stop",
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = {e: {"NSE:TSL": {"close": 100.0, "avg_txn": 10_000_000}}
                 for e in mtm_epochs}
        context = {"start_margin": 1_000_000, "start_epoch": start,
                   "end_epoch": end, "slippage_rate": 0.0005}
        sim_cfg = {"max_positions": 5, "max_positions_per_instrument": 1}

        _, _, _, _, trade_log = simulator_process(
            context, orders, stats, {}, sim_cfg, "tsl_test")
        self.assertEqual(trade_log[0]["exit_reason"], "trailing_stop")

    def test_natural_default_when_column_absent(self):
        """Backwards compat: df_orders without exit_reason column -> 'natural'."""
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        entry = start + 2 * SECONDS_IN_ONE_DAY
        exit_ = start + 5 * SECONDS_IN_ONE_DAY

        # No exit_reason column — simulator should default to "natural".
        orders = pl.DataFrame([{
            "instrument": "NSE:NAT",
            "entry_epoch": entry, "exit_epoch": exit_,
            "entry_price": 100.0, "exit_price": 105.0,
            "scanner_config_ids": "s0",
            "entry_config_ids": "e0", "exit_config_ids": "x0",
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = {e: {"NSE:NAT": {"close": 100.0, "avg_txn": 10_000_000}}
                 for e in mtm_epochs}
        context = {"start_margin": 1_000_000, "start_epoch": start,
                   "end_epoch": end, "slippage_rate": 0.0005}
        sim_cfg = {"max_positions": 5, "max_positions_per_instrument": 1}

        _, _, _, _, trade_log = simulator_process(
            context, orders, stats, {}, sim_cfg, "nat_test")
        self.assertEqual(trade_log[0]["exit_reason"], "natural")


class TestBacktestResultAddTradeRoundTrip(unittest.TestCase):
    """add_trade must preserve a non-default exit_reason through to the
    result dict."""

    def test_exit_reason_round_trips(self):
        br = BacktestResult("unit_test", {}, "TEST", "NSE", 1_000_000)
        br.add_equity_point(1000, 1_000_000)
        br.add_equity_point(2000, 1_010_000)
        br.add_trade(
            entry_epoch=1000, exit_epoch=2000,
            entry_price=100.0, exit_price=80.0,
            quantity=10, charges=50.0, symbol="NSE:TEST",
            exit_reason="anomalous_drop",
        )
        br.compute()
        trades = br.to_dict()["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "anomalous_drop")

    def test_empty_exit_reason_silently_dropped(self):
        """Known shortcoming (PL-1 in SESSION_CODE_REVIEW): empty string
        exit_reason is dropped by the `if exit_reason:` guard in add_trade.
        Lock in current behavior so a future change is deliberate."""
        br = BacktestResult("unit_test", {}, "TEST", "NSE", 1_000_000)
        br.add_equity_point(1000, 1_000_000)
        br.add_equity_point(2000, 1_010_000)
        br.add_trade(
            entry_epoch=1000, exit_epoch=2000,
            entry_price=100.0, exit_price=101.0,
            quantity=10, charges=50.0, symbol="NSE:TEST",
            exit_reason="",  # empty, should drop key
        )
        br.compute()
        trade = br.to_dict()["trades"][0]
        self.assertNotIn("exit_reason", trade)


class TestOrderGeneratorNoneCloseGuard(unittest.TestCase):
    """Lock in the OG-1 guard from the first code review:
    `generate_exit_attributes_for_instrument` must skip entry days whose
    close is None (e.g. forward-filled weekend bars). Without the guard,
    downstream `trailing_stop(close_price=None, max_price=None, ...)`
    would TypeError on numeric comparisons.
    """

    def test_none_close_at_entry_epoch_is_skipped(self):
        from engine.order_generator import generate_exit_attributes_for_instrument

        # 5 bars, one instrument. Bar 2 has a None close (forward-filled).
        # An entry is scheduled for bar 2. The guard must skip it.
        date_epochs = [1000, 2000, 3000, 4000, 5000]
        closes =      [100.0, 101.0, None,  103.0, 104.0]
        opens =       [99.5,  100.5, 101.5, 102.5, 103.5]
        next_opens =  [100.5, 101.5, 102.5, 103.5, None]
        next_volumes = [1000]*5
        next_epochs = [2000, 3000, 4000, 5000, None]

        df_instrument = pl.DataFrame({
            "date_epoch": date_epochs,
            "close": closes,
            "open": opens,
            "next_open": next_opens,
            "next_volume": next_volumes,
            "next_epoch": next_epochs,
        })

        # Two entry configs scheduled: one at the None-close bar (bar 2 epoch=3000)
        # and one at a valid bar (bar 1 epoch=2000).
        original_attrs_at_3000 = {
            "entry_price": 101.0, "exit_price": None,
            "entry_volume": 1000, "exit_volume": None,
            "scanner_config_ids": "s0", "entry_config_ids": "e0",
            "exit_config_ids": "",
        }
        instrument_order_config = {
            2000: {"entry_price": 100.0, "exit_price": None,
                   "entry_volume": 1000, "exit_volume": None,
                   "scanner_config_ids": "s0", "entry_config_ids": "e0",
                   "exit_config_ids": ""},
            3000: original_attrs_at_3000,
        }

        context = {
            "exit_config_input": {
                "trailing_stop_pct": [5],
                "min_hold_time_days": [0],
            },
            "total_exit_configs": 1,
        }

        # Pre-guard: would TypeError on `(None - None) * 100 / None` or
        # `close_price > None`. Post-guard: the None-close entry is skipped
        # cleanly; no exception, original attrs untouched for that epoch.
        instrument, result = generate_exit_attributes_for_instrument(
            "NSE:TEST", instrument_order_config, df_instrument, context,
            drop_threshold=20,
        )

        # Instrument returned unchanged.
        self.assertEqual(instrument, "NSE:TEST")
        # Entry at 3000 (None close) preserved in original form — NOT reset
        # to {} and populated with bogus exit attrs.
        self.assertEqual(result[3000], original_attrs_at_3000)
        # Entry at 2000 (valid close) was processed — reset to {} and
        # typically populated with exit attrs (or left empty if no exit fired).
        # Either way, its structure differs from original_attrs dict.
        self.assertIsInstance(result[2000], dict)


class TestPipelineExitReasonRegression(unittest.TestCase):
    """End-to-end regression lock-in for REV-1: a real order_generator run
    on synthetic OHLCV data must tag the resulting df_orders with actual
    exit reasons, not a sea of "natural".

    This is the integration-level guard. If a future refactor re-introduces
    a fixed column_order that drops exit_reason, this test will catch it
    even if the unit tests above pass.
    """

    def test_synthetic_pipeline_produces_real_exit_reasons(self):
        import tempfile
        from engine.config_loader import load_config
        from engine.config_sweep import create_config_iterator
        from engine import scanner, order_generator

        # Synthetic OHLCV: trending up with periodic dips. The dips trigger
        # TSL exits; the end of the window triggers end_of_data exits for
        # any still-open position.
        base_epoch = 1577836800  # 2020-01-01
        n_days = 60
        instruments = [
            ("SYM0", 100), ("SYM1", 150), ("SYM2", 200),
            ("SYM3", 80), ("SYM4", 120),
        ]

        rows = []
        for sym, base_price in instruments:
            for d in range(n_days):
                epoch = base_epoch + d * 86400
                trend = d * 0.8
                dip = -15 if d % 20 == 19 else 0
                price = base_price + trend + dip
                rows.append({
                    "date_epoch": epoch,
                    "open": price - 0.5, "high": price + 1.5,
                    "low": price - 1.5, "close": price,
                    "average_price": (price + 1.5 + price - 1.5 + price) / 3,
                    "volume": 2_000_000,
                    "symbol": sym,
                    "instrument": f"NSE:{sym}",
                    "exchange": "NSE",
                })
        df = pl.DataFrame(rows)

        # eod_technical-style config: uses order_generator.process path
        # (not walk_forward_exit).
        config_yaml = """
static:
  strategy_type: eod_technical
  start_margin: 1000000
  start_epoch: 1577836800
  end_epoch: 1582934400
  prefetch_days: 0
  data_granularity: day
scanner:
  instruments:
    - [{exchange: NSE, symbols: []}]
  price_threshold: [50]
  avg_day_transaction_threshold:
    - {period: 5, threshold: 100}
  n_day_gain_threshold:
    - {n: 3, threshold: -999}
entry:
  n_day_ma: [3]
  n_day_high: [2]
  direction_score:
    - {n_day_ma: 3, score: 0.0}
exit:
  min_hold_time_days: [0]
  trailing_stop_pct: [10]
simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [30]
  max_positions: [5]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {type: fixed, value: 200000}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_yaml)
            f.flush()
            config = load_config(f.name)
        os.unlink(f.name)

        static = config["static_config"]
        exit_total, _ = create_config_iterator(**config["exit_config_input"])
        context = {
            **config,
            "start_margin": static["start_margin"],
            "start_epoch": static["start_epoch"],
            "end_epoch": static["end_epoch"],
            "prefetch_days": static["prefetch_days"],
            "total_exit_configs": exit_total,
        }

        df_scanned = scanner.process(context, df)
        df_orders = order_generator.process(context, df_scanned)

        # REV-1 core assertion: exit_reason column survives generate_order_df's
        # projection and is populated with real, non-"natural" values.
        self.assertIn("exit_reason", df_orders.columns,
                      "exit_reason was projected away — REV-1 regression")

        reasons = df_orders["exit_reason"].to_list()
        distinct = set(reasons)

        # Some real exit semantics should be represented. Pre-fix every value
        # was "natural"; post-fix we expect at least trailing_stop or
        # end_of_data.
        self.assertTrue(
            distinct - {"natural"},
            f"All exit_reasons are 'natural' — REV-1 regression. Got: {distinct}"
        )
        # No value should be null/empty (fill_null("natural") invariant).
        self.assertTrue(all(r for r in reasons),
                        "exit_reason has null or empty values")


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the review-findings fixes.

Each test here locks in the behavior of a specific bug that was identified
during the Layer 0-5 review (docs/AUDIT_FINDINGS.md). They prevent the same
bug from recurring silently.

  T1: end_epoch off-by-one fix (>= -> >) — exercised when end_epoch is NOT
      in processing_dates, so the > guard is the thing that matters.
  T2: BacktestResult with equity_curve_frequency=DAILY_TRADING produces
      correct vol annualization (vs a curve with same wall-clock span but
      forward-filled to DAILY_CALENDAR).
  T3: compute_metrics_from_curve handles port_curve starting at 0 (CAGR=None)
      without crashing in _compute_comparison.
  B1: snapshot._extract pulls real metric values from sweep-shaped results,
      not nulls.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.simulator import process
from lib.equity_curve import EquityCurve, Frequency, SECONDS_PER_DAY
from lib.metrics import compute_metrics_from_curve
from lib.backtest_result import BacktestResult
from tests.regression.snapshot import _extract, _identity, capture, compare


# ── T1: end_epoch off-by-one ─────────────────────────────────────────────

class TestEndEpochOffByOne(unittest.TestCase):
    """Pre-fix used `>= end_epoch: break`, so end_epoch was excluded.
    This test exercises the strict-inequality fix in isolation by making
    end_epoch a date that the loop would otherwise try to process."""

    def test_loop_processes_epochs_equal_to_end_epoch(self):
        start = 1577836800
        # end_epoch aligned on a specific day; processing_dates will contain it.
        end = start + 10 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:T",
            "entry_epoch": start + 2 * SECONDS_IN_ONE_DAY,
            "exit_epoch": end,               # exit ON end_epoch itself
            "entry_price": 100.0,
            "exit_price": 115.0,
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 11)]
        stats = {e: {"NSE:T": {"close": 115.0, "avg_txn": 1_000_000}} for e in mtm_epochs}

        context = {"start_epoch": start, "end_epoch": end,
                   "start_margin": 1_000_000, "slippage_rate": 0.0005}
        sim_config = {"max_positions": 5, "max_positions_per_instrument": 1}

        _, _, _, _, trade_log = process(context, df_orders, stats, {}, sim_config, "t")

        # Natural exit was scheduled on end_epoch. Pre-fix (`>= end_epoch: break`)
        # would skip that day, falling through to end-of-sim at close.
        # Post-fix (`> end_epoch: break`), the natural exit runs at exit_price=115.
        self.assertEqual(len(trade_log), 1)
        trade = trade_log[0]
        self.assertEqual(trade["exit_epoch"], end)
        # exit_price came from df_orders (115), not from the fallback end-of-sim
        # MTM path. Both happen to equal 115 here because close==exit_price; the
        # important check is that a natural exit was recorded, not end_of_sim.
        self.assertNotEqual(trade.get("exit_reason"), "end_of_sim")


# ── T2: BacktestResult DAILY_TRADING frequency ───────────────────────────

class TestBacktestResultFrequency(unittest.TestCase):
    """When a caller emits one equity point per trading day, passing
    DAILY_TRADING must produce the same CAGR as the same backtest with
    DAILY_CALENDAR forward-fill. This is the forward-fill invariance
    contract from Layer 1 applied at the BacktestResult boundary."""

    def test_trading_day_frequency_produces_correct_cagr_and_vol(self):
        """Strong test: verifies BOTH CAGR (frequency-invariant) AND vol
        (frequency-dependent). A buggy default would pass the CAGR assertion
        but FAIL the vol assertion — catching the exact class of mistake that
        B2 introduced."""
        import math
        # Use CALENDAR-day epochs but noisy returns so vol is non-trivial.
        # Same raw data under two different frequency labels -> vol differs by
        # exactly sqrt(365/252) ≈ 1.2035.
        start = 1577836800
        days = 252 * 2
        # Deterministic wiggle so variance is non-zero
        values = [100_000.0 * (1.0 + 0.001 * ((i % 7) - 3)) for i in range(days)]
        # Make the series trend slightly so CAGR is also meaningful
        values = [v * (1.0001 ** i) for i, v in enumerate(values)]
        epochs = [start + i * 86400 for i in range(days)]

        br_trd = BacktestResult("t", {}, "X", "NSE", 100_000,
                                equity_curve_frequency=Frequency.DAILY_TRADING,
                                risk_free_rate=0.0)
        br_cal = BacktestResult("t", {}, "X", "NSE", 100_000,
                                equity_curve_frequency=Frequency.DAILY_CALENDAR,
                                risk_free_rate=0.0)
        for e, v in zip(epochs, values):
            br_trd.add_equity_point(e, v)
            br_cal.add_equity_point(e, v)
        br_trd.compute()
        br_cal.compute()

        s_trd = br_trd.to_dict()["summary"]
        s_cal = br_cal.to_dict()["summary"]

        # CAGR is frequency-invariant (Layer 1 invariant).
        self.assertAlmostEqual(s_trd["cagr"], s_cal["cagr"], places=6)

        # Vol IS frequency-dependent. Ratio must be exactly sqrt(365/252).
        ratio = s_cal["annualized_volatility"] / s_trd["annualized_volatility"]
        expected_ratio = math.sqrt(365 / 252)
        self.assertAlmostEqual(ratio, expected_ratio, places=4,
            msg=f"Vol ratio {ratio:.4f} != sqrt(365/252) {expected_ratio:.4f}; "
                "frequency wiring to vol annualization is broken.")


# ── T3: None CAGR flows through _compute_comparison cleanly ──────────────

class TestNoneCagrComparison(unittest.TestCase):
    """_cagr_from_curve returns None when the curve starts at 0. The
    comparison-metrics path must not crash on port_cagr=None or
    bench_cagr=None."""

    def test_zero_start_port_curve_no_crash(self):
        # Port starts at zero -> CAGR None. Bench starts positive -> CAGR defined.
        epochs = tuple(1577836800 + i * SECONDS_PER_DAY for i in range(10))
        port = EquityCurve(epochs=epochs, values=(0.0,) * 10,
                           frequency=Frequency.DAILY_TRADING)
        bench = EquityCurve(epochs=epochs,
                            values=tuple(100.0 + i for i in range(10)),
                            frequency=Frequency.DAILY_TRADING)
        result = compute_metrics_from_curve(port, bench)
        # Must not raise. CAGR-dependent comparison fields are None.
        self.assertIsNone(result["portfolio"]["cagr"])
        self.assertIsNone(result["comparison"]["excess_cagr"])
        self.assertIsNone(result["comparison"]["alpha"])
        # Non-CAGR comparison fields still populate.
        self.assertIn("tracking_error", result["comparison"])


# ── B1: snapshot extraction matches real sweep shape ─────────────────────

class TestSnapshotExtraction(unittest.TestCase):
    """_extract must pull real values (not nulls) from the actual sweep
    output shape (`detailed[0].summary`)."""

    def _synthetic_sweep(self):
        return {
            "version": "1.0",
            "type": "sweep",
            "meta": {"strategy_name": "fake", "instrument": "PORTFOLIO",
                     "exchange": "NSE", "capital": 1_000_000},
            "detailed": [{
                "rank": 1,
                "params": {"tsl": 5},
                "summary": {
                    "cagr": 0.12, "total_return": 0.95, "max_drawdown": -0.2,
                    "annualized_volatility": 0.15, "sharpe_ratio": 0.7,
                    "sortino_ratio": 0.9, "calmar_ratio": 0.6,
                    "total_trades": 100, "win_rate": 0.45, "profit_factor": 1.8,
                    "final_value": 1_950_000, "peak_value": 2_100_000,
                },
            }],
            "all_configs": [],
        }

    def test_extract_pulls_real_values_not_nulls(self):
        sweep = self._synthetic_sweep()
        pinned = _extract(sweep)
        self.assertEqual(pinned["cagr"], 0.12)
        self.assertEqual(pinned["total_trades"], 100)
        self.assertEqual(pinned["sharpe_ratio"], 0.7)
        # No field should be silently None
        for k, v in pinned.items():
            self.assertIsNotNone(v, f"Field {k} was None; snapshot extraction broken")

    def test_identity_reads_meta(self):
        sweep = self._synthetic_sweep()
        ident = _identity(sweep)
        self.assertEqual(ident["strategy"], "fake")
        self.assertEqual(ident["exchange"], "NSE")

    def test_compare_detects_drift_and_tolerates_noise(self):
        """End-to-end: capture -> compare produces no diff on identical input,
        flags a diff when a pinned field moves beyond tolerance."""
        sweep = self._synthetic_sweep()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "result.json")
            with open(src, "w") as f:
                json.dump(sweep, f)
            snap_name = "test_synthetic_" + str(os.getpid())
            capture(src, snap_name)
            # Identical -> no diff
            ok, diffs = compare(snap_name, src)
            self.assertTrue(ok, f"Expected no diff, got {diffs}")

            # Perturb CAGR beyond tolerance
            perturbed = dict(sweep)
            perturbed["detailed"] = [dict(sweep["detailed"][0])]
            perturbed["detailed"][0]["summary"] = dict(sweep["detailed"][0]["summary"])
            perturbed["detailed"][0]["summary"]["cagr"] = 0.15  # 25% change
            p_path = os.path.join(tmp, "perturbed.json")
            with open(p_path, "w") as f:
                json.dump(perturbed, f)
            ok, diffs = compare(snap_name, p_path)
            self.assertFalse(ok)
            self.assertTrue(any(d["field"] == "cagr" for d in diffs))

            # Cleanup the snapshot we created
            from tests.regression.snapshot import SNAPSHOT_DIR
            os.remove(SNAPSHOT_DIR / f"{snap_name}.json")


# ── P1.3: yearly MDD carries running peak across year boundaries ─────────

class TestYearlyMDDRunningPeak(unittest.TestCase):
    """Phase 1.3: pre-fix reset the peak at each year-start, so a year spent
    wholly under a prior-year high reported MDD=0. Post-fix, the running peak
    survives year boundaries and MDD reflects drawdown-from-ATH within each year.
    """

    def test_mdd_reflects_carry_peak_from_prior_year(self):
        # Year 1: climb 100 -> 200 (peak). Year 2: fall to 150 early,
        # then rally monotonically to 180. Under the old code Year 2 MDD
        # would read 0% (no new peak set within year 2). Under the fix,
        # Year 2 MDD reads ~(200-150)/200 = -25%.
        y1_start = 1577836800  # 2020-01-01 UTC
        # 12 monthly points in year 1: 100, 110, ..., 210 (peak 210)
        y1 = [(y1_start + i * 30 * 86400, 100.0 + 10.0 * i) for i in range(12)]
        y2_start = 1609459200  # 2021-01-01 UTC
        # 6 monthly points in year 2: 150, 155, 160, 165, 170, 180
        y2 = [(y2_start + i * 30 * 86400, 150.0 + i * 5.0) for i in range(5)]
        y2.append((y2_start + 5 * 30 * 86400, 180.0))

        br = BacktestResult("t", {}, "X", "NSE", 100_000,
                            equity_curve_frequency=Frequency.DAILY_CALENDAR,
                            risk_free_rate=0.0)
        for e, v in y1 + y2:
            br.add_equity_point(e, v)
        br.compute()

        yearly = br.to_dict()["yearly_returns"]
        by_year = {y["year"]: y for y in yearly}

        # Year 1: up-only, peak never retraced -> MDD = 0
        self.assertAlmostEqual(by_year[2020]["mdd"], 0.0, places=4)
        # Year 2: global peak was 210 (end of 2020); low was 150 (start of 2021)
        # Drawdown from 210 to 150 = -0.2857...
        expected_y2_mdd = -(210.0 - 150.0) / 210.0
        self.assertAlmostEqual(by_year[2021]["mdd"], expected_y2_mdd, places=4)


# ── P1.4: time_in_market uses interval-union, not sum of hold_days ───────

class TestTimeInMarketIntervalUnion(unittest.TestCase):
    """Phase 1.4: pre-fix summed per-trade hold_days, so a portfolio with
    many concurrent positions saturated time_in_market at 1.0. Post-fix,
    overlapping intervals are counted once."""

    def test_two_fully_overlapping_trades_count_as_one(self):
        # Sim spans 100 days. Two trades both open for days 10-50 (40 days).
        # Pre-fix: 40 + 40 = 80 / 100 = 0.8.
        # Post-fix: union = 40 days. 40 / 100 = 0.4.
        start = 1577836800
        br = BacktestResult("t", {}, "X", "NSE", 100_000,
                            equity_curve_frequency=Frequency.DAILY_CALENDAR,
                            risk_free_rate=0.0)
        for i in range(101):
            br.add_equity_point(start + i * 86400, 100_000 + i * 10)
        # Add two overlapping trades directly to self.trades (bypass charges).
        for _ in range(2):
            br.trades.append({
                "entry_epoch": start + 10 * 86400,
                "exit_epoch": start + 50 * 86400,
                "entry_price": 100, "exit_price": 105, "quantity": 1,
                "side": "LONG", "gross_pnl": 5.0, "net_pnl": 5.0,
                "pnl_pct": 5.0, "hold_days": 40.0,
                "charges": 0.0, "slippage": 0.0,
            })
        br.compute()
        s = br.to_dict()["summary"]
        # total_days = 100, union = 40 -> 0.40
        self.assertAlmostEqual(s["time_in_market"], 0.4, places=4)

    def test_disjoint_intervals_sum_durations(self):
        start = 1577836800
        br = BacktestResult("t", {}, "X", "NSE", 100_000,
                            equity_curve_frequency=Frequency.DAILY_CALENDAR,
                            risk_free_rate=0.0)
        for i in range(101):
            br.add_equity_point(start + i * 86400, 100_000 + i)
        # Disjoint: days 0-20 (20d) and 60-80 (20d) -> union 40 / 100 = 0.4
        br.trades.append({
            "entry_epoch": start, "exit_epoch": start + 20 * 86400,
            "entry_price": 1, "exit_price": 1, "quantity": 1,
            "side": "LONG", "gross_pnl": 0.0, "net_pnl": 0.0,
            "pnl_pct": 0.0, "hold_days": 20.0,
            "charges": 0.0, "slippage": 0.0,
        })
        br.trades.append({
            "entry_epoch": start + 60 * 86400, "exit_epoch": start + 80 * 86400,
            "entry_price": 1, "exit_price": 1, "quantity": 1,
            "side": "LONG", "gross_pnl": 0.0, "net_pnl": 0.0,
            "pnl_pct": 0.0, "hold_days": 20.0,
            "charges": 0.0, "slippage": 0.0,
        })
        br.compute()
        s = br.to_dict()["summary"]
        self.assertAlmostEqual(s["time_in_market"], 0.4, places=4)


# ── P2.1: MTM update handles close_price = 0 correctly ───────────────────

class TestMTMZeroClose(unittest.TestCase):
    """Phase 2.1: pre-fix `if close_price:` skipped MTM updates on close=0,
    which silently preserved the last non-zero price. A stock that actually
    went to 0 (delisting, corporate action) would show no wipeout in the
    equity curve. Post-fix uses `is not None`, so 0.0 updates MTM."""

    def test_zero_close_zeroes_position_mtm(self):
        start = 1577836800
        end = start + 5 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:Z",
            "entry_epoch": start,
            "exit_epoch": end,
            "entry_price": 100.0,
            "exit_price": 0.0,
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 6)]
        stats = {e: {"NSE:Z": {"close": 100.0, "avg_txn": 1_000_000}}
                 for e in mtm_epochs[:3]}
        # Days 3-5: close is 0 (delisted stub)
        for e in mtm_epochs[3:]:
            stats[e] = {"NSE:Z": {"close": 0.0, "avg_txn": 1_000_000}}

        context = {"start_epoch": start, "end_epoch": end,
                   "start_margin": 1_000_000, "slippage_rate": 0.0005}
        sim_config = {"max_positions": 5, "max_positions_per_instrument": 1}

        day_wise_log, _, _, _, _ = process(context, df_orders, stats, {},
                                           sim_config, "t")
        # Last day's invested_value must reflect close=0, not the stale 100.
        last_day = day_wise_log[-1]
        self.assertEqual(last_day["invested_value"], 0.0,
            "close=0 must zero the MTM, not preserve stale last_close_price")


# ── P2.2: missing avg_txn under percentage_of_instrument_avg_txn cap ─────

class TestMissingAvgTxnPolicy(unittest.TestCase):
    """Phase 2.2: pre-fix silently skipped the cap when avg_txn was absent,
    letting the order through uncapped. Post-fix: default "no_cap" policy
    preserves legacy behavior for reproducibility; opt-in "skip" policy
    refuses the order. Either way, the event is recorded in
    snapshot["missing_avg_txn_events"]."""

    def _ctx(self, start, end, **overrides):
        c = {"start_epoch": start, "end_epoch": end,
             "start_margin": 1_000_000, "slippage_rate": 0.0005}
        c.update(overrides)
        return c

    def _df(self, start):
        # One entry order on day 1.
        return pl.DataFrame([{
            "instrument": "NSE:X",
            "entry_epoch": start + SECONDS_IN_ONE_DAY,
            "exit_epoch": start + 5 * SECONDS_IN_ONE_DAY,
            "entry_price": 100.0,
            "exit_price": 105.0,
        }])

    def _stats_without_avg_txn(self, start):
        # close populated for MTM but no avg_txn key on day 1 (entry day)
        epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 6)]
        stats = {e: {"NSE:X": {"close": 100.0, "avg_txn": 1_000_000}} for e in epochs}
        entry_day = start + SECONDS_IN_ONE_DAY
        stats[entry_day]["NSE:X"] = {"close": 100.0}  # no avg_txn
        return stats

    def test_default_no_cap_preserves_legacy_behavior_and_logs(self):
        """Default policy is "no_cap" — pre-fix behavior. Order is placed
        without the cap (preserves reproducibility of historical results),
        but the event is now logged so the silent fallback is auditable."""
        start = 1577836800
        end = start + 5 * SECONDS_IN_ONE_DAY
        sim_config = {
            "max_positions": 5, "max_positions_per_instrument": 1,
            "max_order_value": {"type": "percentage_of_instrument_avg_txn", "value": 1.0},
        }
        ctx = self._ctx(start, end)  # no policy override -> default "no_cap"
        _, _, snap, _, trade_log = process(
            ctx, self._df(start), self._stats_without_avg_txn(start),
            {}, sim_config, "t"
        )
        # Legacy behavior: order IS placed despite missing avg_txn.
        self.assertEqual(len(trade_log), 1)
        # Event is logged so the silent fallback is now visible.
        self.assertIn("missing_avg_txn_events", snap)
        self.assertEqual(len(snap["missing_avg_txn_events"]), 1)
        self.assertEqual(snap["missing_avg_txn_events"][0]["policy"], "no_cap")

    def test_skip_policy_refuses_order_and_logs(self):
        """Opt-in "skip" policy: refuse the order when avg_txn is missing,
        honoring the user's cap strictly."""
        start = 1577836800
        end = start + 5 * SECONDS_IN_ONE_DAY
        sim_config = {
            "max_positions": 5, "max_positions_per_instrument": 1,
            "max_order_value": {"type": "percentage_of_instrument_avg_txn", "value": 1.0},
        }
        ctx = self._ctx(start, end, missing_avg_txn_policy="skip")
        _, _, snap, _, trade_log = process(
            ctx, self._df(start), self._stats_without_avg_txn(start),
            {}, sim_config, "t"
        )
        self.assertEqual(len(trade_log), 0)
        self.assertEqual(snap["missing_avg_txn_events"][0]["policy"], "skip")


# ── P2.4: payout catch-up when resumed past multiple intervals ───────────

class TestPayoutCatchUp(unittest.TestCase):
    """Phase 2.4: pre-fix ran one payout per iteration and advanced
    next_payout_epoch by a single interval, silently skipping payouts when
    resuming from a snapshot past multiple intervals. Post-fix loops until
    next_payout_epoch is in the future."""

    def test_resume_past_three_fixed_payouts(self):
        start = 1577836800
        # Advance 35 days with a 10-day payout interval and pre-populated
        # next_payout_epoch from the "past" to simulate a resume scenario.
        end = start + 35 * SECONDS_IN_ONE_DAY
        # No orders — pure payout path test.
        df_orders = pl.DataFrame(schema={
            "instrument": pl.String, "entry_epoch": pl.Int64,
            "exit_epoch": pl.Int64, "entry_price": pl.Float64,
            "exit_price": pl.Float64,
        })
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 36)]
        stats = {e: {} for e in mtm_epochs}

        # Snapshot claims next_payout was due at start - 5 days.
        # With interval=10d, 4 payouts are due over [start-5, start, +5, +15, +25, +35].
        # Actually: start-5 (due), start+5 (due at simulation_date=start... no)
        # Let me think: simulation_date_epoch advances from start -> end.
        # At simulation_date=start, next_payout_epoch = start - 5d -> payout runs,
        # then advances to start + 5d. start >= start+5d? no. Stop. 1 payout.
        # At simulation_date=start+5d: next_payout=start+5d -> run, advance to start+15d. 1 more.
        # Etc. So across the 35-day sim we'd see payouts at start, +5, +15, +25, +35 = 5 payouts.
        # (pre-fix would also give 5 because the single-advance still catches up
        # across successive iterations — the only case where the old code
        # actually *skipped* payouts is when a SINGLE iteration covers multiple
        # intervals, which is what "resume" mimics.)

        # Better test: start with next_payout_epoch = start - 25d. First
        # iteration at simulation_date=start covers 3 intervals (25, 15, 5).
        # Pre-fix: 1 payout of $100. Post-fix: 3 payouts totaling $300.
        snapshot = {
            "margin_available": 1_000_000.0,
            "current_position_value": 0.0,
            "simulation_date": start,
            "current_positions_count": 0,
            "max_account_value": 1_000_000.0,
            "current_positions": {},
            "next_payout_epoch": start - 25 * SECONDS_IN_ONE_DAY,
        }
        sim_config = {
            "max_positions": 5, "max_positions_per_instrument": 1,
            "pay_out": {
                "type": "fixed", "value": 100.0,
                "withdrawal_lockup_days": 0, "payout_interval_days": 10,
            },
        }
        context = {"start_epoch": start, "end_epoch": end,
                   "start_margin": 1_000_000, "slippage_rate": 0.0005}

        _, _, snap, _, _ = process(context, df_orders, stats, snapshot,
                                   sim_config, "t")
        # Fixed payouts: expected = (25/10 catch-up = 3) + regular over 35 days.
        # Regular: at sim_date = start+5, +15, +25, +35 = 4 more payouts.
        # Total: 3 + 4 = 7 * $100 = $700 withdrawn.
        expected_withdrawn = 700.0
        expected_margin = 1_000_000 - expected_withdrawn
        self.assertAlmostEqual(snap["margin_available"], expected_margin, places=2)


if __name__ == "__main__":
    unittest.main()

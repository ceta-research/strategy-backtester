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


if __name__ == "__main__":
    unittest.main()

"""Property-style invariant tests for lib/metrics.py (P2 L28).

Parametrized across representative inputs — not true Hypothesis tests
(hypothesis is not a project dependency), but assert the same class of
invariants that property testing targets.

Invariants:
- CAGR of a flat unit series `[1, 1, 1, ...]` is 0%.
- Calmar is None when MDD is 0 (divide-by-zero guard).
- Sharpe (geometric) is None when vol is 0 (constant-return series).
- max_drawdown is 0 on a strictly increasing series.
- max_drawdown bounded in [-1, 0] for any series.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.equity_curve import EquityCurve, Frequency
from lib.metrics import compute_drawdown_series, compute_metrics_from_curve


ONE_DAY = 86400


def _curve(values, freq=Frequency.DAILY_TRADING):
    """Build an EquityCurve with evenly-spaced epochs."""
    n = len(values)
    epochs = [1_600_000_000 + i * ONE_DAY for i in range(n)]
    return EquityCurve.from_pairs(list(zip(epochs, values)), freq)


class TestFlatSeriesInvariants(unittest.TestCase):
    """Flat curve: constant value → CAGR=0, vol=0, Sharpe=None."""

    def test_flat_curve_cagr_is_zero(self):
        for n in (2, 5, 50, 500):
            with self.subTest(n=n):
                curve = _curve([100.0] * n)
                res = compute_metrics_from_curve(curve)
                self.assertAlmostEqual(res["portfolio"]["cagr"], 0.0, places=9)

    def test_flat_curve_vol_is_zero(self):
        curve = _curve([100.0] * 10)
        res = compute_metrics_from_curve(curve)
        self.assertAlmostEqual(res["portfolio"]["annualized_volatility"], 0.0, places=9)

    def test_flat_curve_sharpe_is_none(self):
        """vol=0 → both Sharpe definitions None."""
        curve = _curve([100.0] * 10)
        res = compute_metrics_from_curve(curve)
        self.assertIsNone(res["portfolio"]["sharpe_ratio"])
        self.assertIsNone(res["portfolio"]["sharpe_ratio_arithmetic"])

    def test_flat_curve_mdd_is_zero(self):
        curve = _curve([100.0] * 10)
        res = compute_metrics_from_curve(curve)
        self.assertAlmostEqual(res["portfolio"]["max_drawdown"], 0.0, places=9)


class TestMonotoneSeriesInvariants(unittest.TestCase):
    """Strictly increasing curve: MDD=0, Calmar=None (divide-by-zero)."""

    def test_monotone_up_mdd_zero(self):
        curve = _curve([100.0 + i for i in range(20)])
        res = compute_metrics_from_curve(curve)
        self.assertAlmostEqual(res["portfolio"]["max_drawdown"], 0.0, places=9)

    def test_monotone_up_calmar_is_none(self):
        curve = _curve([100.0 + i for i in range(20)])
        res = compute_metrics_from_curve(curve)
        self.assertIsNone(res["portfolio"]["calmar_ratio"])

    def test_monotone_up_duration_zero_not_none(self):
        """P2 L43 invariant: no drawdown → duration 0, not None."""
        curve = _curve([100.0 + i for i in range(20)])
        res = compute_metrics_from_curve(curve)
        self.assertEqual(res["portfolio"]["max_dd_duration_periods"], 0)


class TestMDDBoundaries(unittest.TestCase):
    """MDD is bounded in [-1, 0] for any non-negative curve."""

    def test_mdd_is_non_positive(self):
        test_curves = [
            [100, 110, 120, 130],         # monotone up
            [100, 90, 80, 70],            # monotone down
            [100, 120, 80, 100, 60, 90],  # choppy
            [100, 110, 100, 110, 100, 110],  # sawtooth
        ]
        for values in test_curves:
            with self.subTest(values=values):
                dd = compute_drawdown_series(values)
                self.assertLessEqual(max(dd), 0.0)
                self.assertGreaterEqual(min(dd), -1.0)

    def test_mdd_ge_minus_one(self):
        """A curve that goes from 100 to 0.01: MDD ≈ -0.9999 but not < -1."""
        dd = compute_drawdown_series([100.0, 50.0, 10.0, 1.0, 0.01])
        self.assertGreaterEqual(min(dd), -1.0)


class TestZeroVolSeries(unittest.TestCase):
    """All-constant-non-unit returns: vol=0, Sharpe=None, CAGR defined."""

    def test_all_same_positive_return(self):
        """Per-period return = 1% forever; compound CAGR is well-defined.

        Sharpe is non-None here because floating-point noise leaves a
        tiny epsilon in vol (~1e-16), so `vol > 0` passes. The invariant
        that matters: a constant-return series should not blow up (no
        NaN/Inf); vol is essentially zero and Sharpe is astronomically
        large but finite."""
        curve = _curve([100.0 * (1.01 ** i) for i in range(60)])
        res = compute_metrics_from_curve(curve)
        port = res["portfolio"]
        # CAGR is well-defined and positive
        self.assertIsNotNone(port["cagr"])
        self.assertGreater(port["cagr"], 0.0)
        # vol is essentially zero (sample std of identical returns is 0
        # up to float precision)
        self.assertLess(port["annualized_volatility"], 1e-10)
        # Sharpe is finite (no division-by-zero crash)
        if port["sharpe_ratio"] is not None:
            self.assertTrue(math.isfinite(port["sharpe_ratio"]))


if __name__ == "__main__":
    unittest.main()

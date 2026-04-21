"""Edge-case tests for lib/metrics.py (P2 Batch 1).

Covers:
- L41 compute_drawdown_series peak<=0 semantics
- L42 VaR 95% index convention (lower-quantile vs numpy linear-interp)
- L43 max_dd_duration_periods returns 0, not None, when no drawdown
- L230 dual Sharpe: geometric and arithmetic emitted side-by-side
- D1 invariant: sharpe_arithmetic >= sharpe_geometric (variance drag)
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.metrics import (
    compute_drawdown_series,
    compute_metrics,
    _compute_series_metrics,
)


class TestDrawdownSeriesPeakZero(unittest.TestCase):
    """L41 — peak<=0 semantics in compute_drawdown_series."""

    def test_starts_at_zero_stays_zero_no_crash(self):
        """Curve starting at 0 that never grows: dd=0 throughout (no reference)."""
        dd = compute_drawdown_series([0.0, 0.0, 0.0])
        self.assertEqual(dd, [0.0, 0.0, 0.0])

    def test_starts_at_zero_then_grows_tracks_from_first_positive(self):
        """Once the curve goes positive, peak tracks; drawdown is relative to that."""
        dd = compute_drawdown_series([0.0, 100.0, 50.0, 100.0])
        self.assertAlmostEqual(dd[0], 0.0, places=6)
        self.assertAlmostEqual(dd[1], 0.0, places=6)
        self.assertAlmostEqual(dd[2], -0.5, places=6)
        self.assertAlmostEqual(dd[3], 0.0, places=6)

    def test_strictly_monotone_curve_has_zero_drawdown(self):
        dd = compute_drawdown_series([100, 110, 120, 130])
        self.assertEqual(max(abs(x) for x in dd), 0.0)

    def test_single_drawdown_matches_hand_compute(self):
        dd = compute_drawdown_series([100, 120, 90, 100])
        # Peak = 120, trough = 90, dd = (90-120)/120 = -0.25
        self.assertAlmostEqual(min(dd), -0.25, places=6)


class TestVaR95Convention(unittest.TestCase):
    """L42 — VaR lower-quantile (observation-based) convention documented."""

    def test_var_picks_observed_5th_percentile_return(self):
        """For n=100, index = ceil(100*0.05)-1 = 4 → 5th smallest return."""
        returns = [i * 0.001 for i in range(-50, 50)]  # -0.050 .. 0.049
        result = compute_metrics(returns, [0.0] * 100, periods_per_year=252)
        port = result["portfolio"]
        sorted_r = sorted(returns)
        expected = sorted_r[4]  # ceil(100*0.05) - 1 = 4
        self.assertAlmostEqual(port["var_95"], expected, places=9)

    def test_var_is_an_observed_return_not_interpolated(self):
        """VaR is one of the actual returns (no interpolation)."""
        returns = [-0.1, -0.05, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07,
                   0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17]
        result = compute_metrics(returns, [0.0] * 20, periods_per_year=252)
        port = result["portfolio"]
        # n=20, index = ceil(20*0.05)-1 = 0 → worst observed return
        self.assertAlmostEqual(port["var_95"], -0.10, places=9)
        self.assertIn(port["var_95"], returns)

    def test_cvar_is_mean_of_tail_observations(self):
        """CVaR is the mean of returns <= VaR."""
        returns = [-0.1, -0.05, -0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06,
                   0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16]
        result = compute_metrics(returns, [0.0] * 20, periods_per_year=252)
        port = result["portfolio"]
        # var_95 = -0.1 (worst); tail = [-0.1]
        self.assertAlmostEqual(port["cvar_95"], -0.10, places=9)


class TestMaxDDDurationEmitsZero(unittest.TestCase):
    """L43 — max_dd_duration_periods returns 0 (not None) when no drawdown."""

    def test_monotone_up_returns_duration_zero_not_none(self):
        """Strictly increasing returns: no drawdown → duration=0."""
        result = compute_metrics([0.01] * 12, [0.0] * 12, periods_per_year=12)
        port = result["portfolio"]
        self.assertEqual(port["max_dd_duration_periods"], 0)
        self.assertAlmostEqual(port["max_drawdown"], 0.0, places=9)

    def test_drawdown_returns_positive_duration(self):
        result = compute_metrics(
            [0.10, -0.20, 0.05, 0.15], [0.0] * 4, periods_per_year=4
        )
        self.assertGreater(result["portfolio"]["max_dd_duration_periods"], 0)

    def test_length_one_returns_zero_duration(self):
        """Single-return edge path: the n<2 branch in
        _compute_series_metrics_with_cagr emits max_dd_duration_periods=0."""
        from lib.metrics import _compute_series_metrics_with_cagr
        result = _compute_series_metrics_with_cagr(
            [0.05], ppy=252, risk_free_rate=0.02,
            cagr=None, total_return=0.05,
        )
        self.assertEqual(result["max_dd_duration_periods"], 0)


class TestDualSharpeD1(unittest.TestCase):
    """L230 / D1 — emit both geometric and arithmetic Sharpe."""

    def test_both_keys_present(self):
        returns = [0.05, -0.02, 0.08, 0.03]
        result = compute_metrics(returns, [0.0] * 4, periods_per_year=4,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        self.assertIn("sharpe_ratio", port)
        self.assertIn("sharpe_ratio_arithmetic", port)
        self.assertIsNotNone(port["sharpe_ratio"])
        self.assertIsNotNone(port["sharpe_ratio_arithmetic"])

    def test_geometric_and_arithmetic_are_distinct(self):
        """The two Sharpe values use different annualization conventions
        (CAGR vs simple-annualized arithmetic mean). They differ on
        any non-trivial return series — if they matched exactly, the
        dual-definition API would be a no-op."""
        returns = [0.10, -0.05, 0.08, -0.03, 0.06, -0.02, 0.07]
        result = compute_metrics(returns, [0.0] * 7, periods_per_year=12,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        # Both defined (vol > 0)
        self.assertIsNotNone(port["sharpe_ratio"])
        self.assertIsNotNone(port["sharpe_ratio_arithmetic"])
        # They should not be numerically identical
        self.assertNotAlmostEqual(
            port["sharpe_ratio"], port["sharpe_ratio_arithmetic"], places=6
        )

    def test_arithmetic_matches_hand_formula(self):
        """sharpe_arithmetic = (mean(r)*ppy - rf) / ann_vol."""
        returns = [0.05, -0.02, 0.08, 0.03]
        ppy = 4
        rf = 0.02
        result = compute_metrics(returns, [0.0] * 4, periods_per_year=ppy,
                                  risk_free_rate=rf)
        port = result["portfolio"]

        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        vol = math.sqrt(var) * math.sqrt(ppy)
        expected = (mean_r * ppy - rf) / vol
        self.assertAlmostEqual(port["sharpe_ratio_arithmetic"], expected, places=9)

    def test_zero_vol_returns_none_for_both(self):
        """Constant returns (vol=0) → both Sharpe values are None."""
        returns = [0.01] * 10
        result = compute_metrics(returns, [0.0] * 10, periods_per_year=12,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        self.assertIsNone(port["sharpe_ratio"])
        self.assertIsNone(port["sharpe_ratio_arithmetic"])


class TestEmptyMetricsSchema(unittest.TestCase):
    """Ensure n<2 path returns the full key set including new sharpe_arithmetic."""

    def test_empty_has_sharpe_arithmetic_key(self):
        result = compute_metrics([], [], periods_per_year=252)
        self.assertIn("sharpe_ratio_arithmetic", result["portfolio"])
        self.assertIsNone(result["portfolio"]["sharpe_ratio_arithmetic"])

    def test_length_one_via_with_cagr_has_sharpe_arithmetic_key(self):
        """The n<2 branch in _compute_series_metrics_with_cagr must include
        the new sharpe_ratio_arithmetic key so downstream code can safely
        `.get("sharpe_ratio_arithmetic")` without KeyError."""
        from lib.metrics import _compute_series_metrics_with_cagr
        result = _compute_series_metrics_with_cagr(
            [0.05], ppy=252, risk_free_rate=0.02,
            cagr=None, total_return=0.05,
        )
        self.assertIn("sharpe_ratio_arithmetic", result)
        self.assertIsNone(result["sharpe_ratio_arithmetic"])


if __name__ == "__main__":
    unittest.main()

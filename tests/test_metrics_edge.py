"""Edge-case tests for lib/metrics.py."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.metrics import compute_drawdown_series, compute_metrics


class TestDrawdownSeriesPeakZero(unittest.TestCase):

    def test_starts_at_zero_stays_zero_no_crash(self):
        dd = compute_drawdown_series([0.0, 0.0, 0.0])
        self.assertEqual(dd, [0.0, 0.0, 0.0])

    def test_starts_at_zero_then_grows_tracks_from_first_positive(self):
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
        self.assertAlmostEqual(min(dd), -0.25, places=6)


class TestVaR95Convention(unittest.TestCase):

    def test_var_picks_observed_5th_percentile_return(self):
        returns = [i * 0.001 for i in range(-50, 50)]
        result = compute_metrics(returns, [0.0] * 100, periods_per_year=252)
        port = result["portfolio"]
        expected = sorted(returns)[4]  # ceil(100 * 0.05) - 1 = 4
        self.assertAlmostEqual(port["var_95"], expected, places=9)

    def test_var_is_an_observed_return_not_interpolated(self):
        returns = [-0.1, -0.05, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07,
                   0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17]
        result = compute_metrics(returns, [0.0] * 20, periods_per_year=252)
        port = result["portfolio"]
        self.assertAlmostEqual(port["var_95"], -0.10, places=9)
        self.assertIn(port["var_95"], returns)

    def test_cvar_is_mean_of_tail_observations(self):
        returns = [-0.1, -0.05, -0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06,
                   0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16]
        result = compute_metrics(returns, [0.0] * 20, periods_per_year=252)
        self.assertAlmostEqual(result["portfolio"]["cvar_95"], -0.10, places=9)


class TestMaxDDDurationEmitsZero(unittest.TestCase):

    def test_monotone_up_returns_duration_zero_not_none(self):
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
        from lib.metrics import _compute_series_metrics_with_cagr
        result = _compute_series_metrics_with_cagr(
            [0.05], ppy=252, risk_free_rate=0.02,
            cagr=None, total_return=0.05,
        )
        self.assertEqual(result["max_dd_duration_periods"], 0)


class TestDualSharpe(unittest.TestCase):

    def test_both_keys_present(self):
        returns = [0.05, -0.02, 0.08, 0.03]
        result = compute_metrics(returns, [0.0] * 4, periods_per_year=4,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        self.assertIsNotNone(port["sharpe_ratio"])
        self.assertIsNotNone(port["sharpe_ratio_arithmetic"])

    def test_geometric_and_arithmetic_are_distinct(self):
        returns = [0.10, -0.05, 0.08, -0.03, 0.06, -0.02, 0.07]
        result = compute_metrics(returns, [0.0] * 7, periods_per_year=12,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        self.assertNotAlmostEqual(
            port["sharpe_ratio"], port["sharpe_ratio_arithmetic"], places=6
        )

    def test_arithmetic_matches_hand_formula(self):
        returns = [0.05, -0.02, 0.08, 0.03]
        ppy, rf = 4, 0.02
        result = compute_metrics(returns, [0.0] * 4, periods_per_year=ppy,
                                  risk_free_rate=rf)
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        vol = math.sqrt(var) * math.sqrt(ppy)
        expected = (mean_r * ppy - rf) / vol
        self.assertAlmostEqual(
            result["portfolio"]["sharpe_ratio_arithmetic"], expected, places=9
        )

    def test_zero_vol_returns_none_for_both(self):
        result = compute_metrics([0.01] * 10, [0.0] * 10, periods_per_year=12,
                                  risk_free_rate=0.02)
        port = result["portfolio"]
        self.assertIsNone(port["sharpe_ratio"])
        self.assertIsNone(port["sharpe_ratio_arithmetic"])


class TestEmptyMetricsSchema(unittest.TestCase):

    def test_empty_has_sharpe_arithmetic_key(self):
        result = compute_metrics([], [], periods_per_year=252)
        self.assertIn("sharpe_ratio_arithmetic", result["portfolio"])
        self.assertIsNone(result["portfolio"]["sharpe_ratio_arithmetic"])

    def test_length_one_via_with_cagr_has_sharpe_arithmetic_key(self):
        from lib.metrics import _compute_series_metrics_with_cagr
        result = _compute_series_metrics_with_cagr(
            [0.05], ppy=252, risk_free_rate=0.02,
            cagr=None, total_return=0.05,
        )
        self.assertIn("sharpe_ratio_arithmetic", result)
        self.assertIsNone(result["sharpe_ratio_arithmetic"])


if __name__ == "__main__":
    unittest.main()

"""Tests for lib/metrics.py"""

import os
import sys
import math
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.metrics import compute_metrics, _compute_series_metrics, _empty_metrics


class TestComputeMetrics(unittest.TestCase):

    def test_known_values_cagr(self):
        """Hand-computed CAGR from known returns."""
        # 4 quarterly returns: +5%, -2%, +8%, +3%
        # Cumulative: 1.05 * 0.98 * 1.08 * 1.03 = 1.14467...
        # CAGR (annualized): cumulative^(1/years) - 1
        # years = 4 / 4 = 1
        # CAGR = 1.14467 - 1 = 0.14467 (14.47%)
        returns = [0.05, -0.02, 0.08, 0.03]
        bench = [0.01, 0.01, 0.01, 0.01]

        result = compute_metrics(returns, bench, periods_per_year=4, risk_free_rate=0.02)
        port = result["portfolio"]

        cumulative = 1.05 * 0.98 * 1.08 * 1.03
        expected_cagr = cumulative ** (1.0 / 1.0) - 1  # 1 year

        self.assertAlmostEqual(port["cagr"], expected_cagr, places=6)
        self.assertAlmostEqual(port["total_return"], cumulative - 1, places=6)

    def test_drawdown_calculation(self):
        """Peak-to-trough from known sequence."""
        # Returns: +10%, -20%, +5%
        # Cumulative: 1.10, 0.88, 0.924
        # Peak: 1.10 after period 1
        # Trough: 0.88 after period 2
        # Max DD: (0.88 - 1.10) / 1.10 = -0.2 = -20%
        returns = [0.10, -0.20, 0.05]
        bench = [0.01, -0.01, 0.01]

        result = compute_metrics(returns, bench, periods_per_year=12, risk_free_rate=0.02)
        port = result["portfolio"]

        expected_dd = (0.88 - 1.10) / 1.10
        self.assertAlmostEqual(port["max_drawdown"], expected_dd, places=6)

    def test_calmar_is_cagr_over_maxdd(self):
        """Calmar = CAGR / |max_dd|."""
        returns = [0.10, -0.20, 0.05, 0.15]
        bench = [0.01, -0.01, 0.01, 0.01]

        result = compute_metrics(returns, bench, periods_per_year=4, risk_free_rate=0.02)
        port = result["portfolio"]

        if port["max_drawdown"] != 0 and port["calmar_ratio"] is not None:
            expected_calmar = port["cagr"] / abs(port["max_drawdown"])
            self.assertAlmostEqual(port["calmar_ratio"], expected_calmar, places=6)

    def test_sharpe_positive_excess(self):
        """Positive excess return -> positive Sharpe (when vol > 0)."""
        # All positive returns well above risk-free
        returns = [0.05, 0.04, 0.06, 0.05, 0.03, 0.07]
        bench = [0.01] * 6

        result = compute_metrics(returns, bench, periods_per_year=12, risk_free_rate=0.02)
        port = result["portfolio"]

        # CAGR should be well above 2% -> positive Sharpe
        self.assertGreater(port["cagr"], 0.02)
        self.assertIsNotNone(port["sharpe_ratio"])
        self.assertGreater(port["sharpe_ratio"], 0)

    def test_empty_returns(self):
        """[] -> all metrics None."""
        result = compute_metrics([], [], periods_per_year=252)
        port = result["portfolio"]
        self.assertIsNone(port["cagr"])
        self.assertIsNone(port["max_drawdown"])
        self.assertIsNone(port["sharpe_ratio"])
        self.assertIsNone(port["calmar_ratio"])

    def test_single_period(self):
        """[0.05] -> all metrics None (need >= 2 periods)."""
        result = compute_metrics([0.05], [0.01], periods_per_year=252)
        port = result["portfolio"]
        self.assertIsNone(port["cagr"])


class TestSeriesMetrics(unittest.TestCase):

    def test_all_positive_no_drawdown(self):
        """Monotonically positive returns -> max_dd = 0."""
        returns = [0.01, 0.02, 0.01, 0.03]
        metrics = _compute_series_metrics(returns, 4, 0.02)
        self.assertEqual(metrics["max_drawdown"], 0)
        self.assertIsNone(metrics["calmar_ratio"])  # 0 drawdown -> calmar undefined

    def test_all_negative(self):
        returns = [-0.05, -0.03, -0.04]
        metrics = _compute_series_metrics(returns, 12, 0.02)
        self.assertLess(metrics["cagr"], 0)
        self.assertLess(metrics["max_drawdown"], 0)

    def test_max_consecutive_losses(self):
        returns = [0.01, -0.01, -0.02, -0.03, 0.05, -0.01]
        metrics = _compute_series_metrics(returns, 12, 0.02)
        self.assertEqual(metrics["max_consecutive_losses"], 3)

    def test_var_95(self):
        """VaR should be the 5th percentile return."""
        returns = list(range(-10, 90))  # -10 to 89 (100 values)
        returns = [r / 100 for r in returns]
        metrics = _compute_series_metrics(returns, 252, 0.02)
        # 5th percentile of -0.10 to 0.89: index 4 -> -0.06
        self.assertAlmostEqual(metrics["var_95"], -0.06, places=2)


class TestSortinoDenominator(unittest.TestCase):
    """Phase 1.1: downside variance must use (n-1) sample estimator to match variance."""

    def test_sortino_uses_sample_downside_variance(self):
        # Hand-compute expected downside_dev with (n-1).
        # Returns: +3%, -4%, +2%, -1%, +5% (monthly, ppy=12)
        # rf_period = 0.02 / 12 ≈ 0.001667
        # downsides (r < rf_period): -4% (-0.04 - 0.001667)^2, -1% (-0.01 - 0.001667)^2
        # wait: only diffs where r - rf_period < 0.
        # r=0.03 → diff=0.028 → 0
        # r=-0.04 → diff=-0.0417 → squared=0.001736
        # r=0.02 → diff=0.0183 → 0
        # r=-0.01 → diff=-0.0117 → squared=0.000136
        # r=0.05 → diff=0.0483 → 0
        # downside_var = (0.001736 + 0 + 0.000136 + 0 + 0) / (n-1=4) = 0.000468
        # downside_dev_annualized = sqrt(0.000468) * sqrt(12) ≈ 0.0749
        returns = [0.03, -0.04, 0.02, -0.01, 0.05]
        metrics = _compute_series_metrics(returns, 12, 0.02)
        # Sortino must be defined and reasonable (not divided-by-tiny).
        self.assertIsNotNone(metrics["sortino_ratio"])
        # Also: manually derive and compare.
        rf_period = 0.02 / 12
        sq = [(r - rf_period) ** 2 if r - rf_period < 0 else 0.0 for r in returns]
        ddev = math.sqrt(sum(sq) / (len(returns) - 1)) * math.sqrt(12)
        expected_sortino = (metrics["cagr"] - 0.02) / ddev
        self.assertAlmostEqual(metrics["sortino_ratio"], expected_sortino, places=6)


class TestEmptyMetrics(unittest.TestCase):

    def test_structure(self):
        m = _empty_metrics()
        self.assertIn("portfolio", m)
        self.assertIn("benchmark", m)
        self.assertIn("comparison", m)
        self.assertIsNone(m["portfolio"]["cagr"])
        self.assertIsNone(m["comparison"]["excess_cagr"])


if __name__ == "__main__":
    unittest.main()

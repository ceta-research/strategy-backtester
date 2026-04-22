"""Cross-check skewness and kurtosis formulas against scipy.stats."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.equity_curve import EquityCurve, Frequency
from lib.metrics import compute_metrics_from_curve

ONE_DAY = 86400


def _curve(values, freq=Frequency.DAILY_TRADING):
    epochs = [1_600_000_000 + i * ONE_DAY for i in range(len(values))]
    return EquityCurve.from_pairs(list(zip(epochs, values)), freq)


class TestSkewKurtCrossCheck(unittest.TestCase):
    """Verify our skewness/kurtosis match scipy.stats on known data."""

    def setUp(self):
        try:
            from scipy.stats import skew, kurtosis
            self.skew = skew
            self.kurtosis = kurtosis
        except ImportError:
            self.skipTest("scipy not installed")

    def _returns(self, values):
        return [values[i] / values[i - 1] - 1 for i in range(1, len(values))]

    def test_right_skewed_series(self):
        values = [100, 101, 103, 102, 108, 107, 115, 114, 120, 125, 130]
        curve = _curve(values)
        res = compute_metrics_from_curve(curve)
        rets = self._returns(values)

        expected_skew = self.skew(rets, bias=False)
        expected_kurt = self.kurtosis(rets, bias=False)

        self.assertAlmostEqual(res["portfolio"]["skewness"], float(expected_skew), places=6)
        self.assertAlmostEqual(res["portfolio"]["kurtosis"], float(expected_kurt), places=6)

    def test_left_skewed_series(self):
        values = [100, 99, 95, 97, 88, 90, 82, 84, 78, 75, 70]
        curve = _curve(values)
        res = compute_metrics_from_curve(curve)
        rets = self._returns(values)

        expected_skew = self.skew(rets, bias=False)
        expected_kurt = self.kurtosis(rets, bias=False)

        self.assertAlmostEqual(res["portfolio"]["skewness"], float(expected_skew), places=6)
        self.assertAlmostEqual(res["portfolio"]["kurtosis"], float(expected_kurt), places=6)

    def test_symmetric_series(self):
        values = [100, 110, 100, 110, 100, 110, 100, 110, 100, 110, 100]
        curve = _curve(values)
        res = compute_metrics_from_curve(curve)
        rets = self._returns(values)

        expected_skew = self.skew(rets, bias=False)
        expected_kurt = self.kurtosis(rets, bias=False)

        self.assertAlmostEqual(res["portfolio"]["skewness"], float(expected_skew), places=6)
        self.assertAlmostEqual(res["portfolio"]["kurtosis"], float(expected_kurt), places=6)

    def test_too_few_returns_gives_none(self):
        """n=2 returns → n<3 → skewness=None; n<4 → kurtosis=None."""
        curve = _curve([100, 110, 120])
        res = compute_metrics_from_curve(curve)
        self.assertIsNone(res["portfolio"]["kurtosis"])


if __name__ == "__main__":
    unittest.main()

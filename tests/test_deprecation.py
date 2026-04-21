"""Tests for intraday_simulator v1 deprecation."""

import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIntradayV1Deprecation(unittest.TestCase):

    def test_emits_deprecation_warning(self):
        import engine.intraday_simulator as v1
        config = {
            "initial_capital": 100_000,
            "max_positions": 1,
            "order_value": 10_000,
            "exchange": "NSE",
        }
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v1.simulate_intraday([], config)
            dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            self.assertTrue(dep_warnings)
            self.assertIn("v2", str(dep_warnings[0].message))


if __name__ == "__main__":
    unittest.main()

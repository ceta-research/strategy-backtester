"""Tests for intraday_simulator v1 deprecation (P2 D2 / L345).

One-time DeprecationWarning fires on first simulate_intraday() call.
Subsequent calls do not re-warn (avoids test-suite flood).
"""

import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIntradayV1Deprecation(unittest.TestCase):

    def setUp(self):
        """Reset the module-level warn-once flag before each test."""
        import engine.intraday_simulator as v1
        v1._DEPRECATION_WARNED = False

    def test_first_call_emits_deprecation_warning(self):
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
            self.assertTrue(dep_warnings, "First call must emit DeprecationWarning")
            self.assertIn("intraday_simulator", str(dep_warnings[0].message))
            self.assertIn("v2", str(dep_warnings[0].message))

    def test_second_call_does_not_re_warn(self):
        """One-time flag prevents test-suite flood."""
        import engine.intraday_simulator as v1
        config = {
            "initial_capital": 100_000,
            "max_positions": 1,
            "order_value": 10_000,
            "exchange": "NSE",
        }
        # First call consumes the warning
        v1.simulate_intraday([], config)
        # Second call should not warn
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v1.simulate_intraday([], config)
            dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            self.assertFalse(dep_warnings,
                             "Subsequent calls should not re-emit DeprecationWarning")


if __name__ == "__main__":
    unittest.main()

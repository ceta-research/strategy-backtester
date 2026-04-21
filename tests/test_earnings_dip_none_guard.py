"""Smoke test for engine.signals.earnings_dip after None-guard edit."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEarningsDipImport(unittest.TestCase):
    def test_module_imports_cleanly(self):
        import engine.signals.earnings_dip  # noqa: F401


if __name__ == "__main__":
    unittest.main()

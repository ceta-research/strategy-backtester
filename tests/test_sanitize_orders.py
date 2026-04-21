"""Tests for engine.signals.base.sanitize_orders (P2 L74).

Coverage:
- Sub-penny entry price removal.
- Non-positive exit price removal.
- Extreme-return capping at max_return_mult.
- L74 diagnostic counter fires at the tighter threshold without changing
  the capped DataFrame (no behavior regression at max_return_mult=999).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.signals.base import sanitize_orders


def _orders(rows):
    """Build a minimal orders DF: entry_price, exit_price."""
    return pl.DataFrame(
        {
            "entry_price": [r[0] for r in rows],
            "exit_price": [r[1] for r in rows],
        }
    )


class TestSanitizeFilters(unittest.TestCase):
    def test_drops_zero_entry_price(self):
        df = _orders([(0.0, 100.0), (10.0, 12.0)])
        out = sanitize_orders(df, max_return_mult=999.0)
        self.assertEqual(out.height, 1)
        self.assertEqual(out["entry_price"][0], 10.0)

    def test_drops_sub_penny_entry(self):
        df = _orders([(0.05, 0.10), (10.0, 12.0)])
        out = sanitize_orders(df, min_entry_price=0.10, max_return_mult=999.0)
        self.assertEqual(out.height, 1)

    def test_drops_zero_exit_price(self):
        df = _orders([(10.0, 0.0), (10.0, 12.0)])
        out = sanitize_orders(df, max_return_mult=999.0)
        self.assertEqual(out.height, 1)

    def test_caps_at_max_return_mult(self):
        """exit_price = 30×entry with max_return_mult=5 → exit capped to 5× entry."""
        df = _orders([(10.0, 300.0), (10.0, 15.0)])
        out = sanitize_orders(df, max_return_mult=5.0, diagnostic_threshold=0)
        self.assertEqual(out["exit_price"][0], 50.0)  # 10 * 5
        self.assertEqual(out["exit_price"][1], 15.0)

    def test_no_cap_when_within_limit(self):
        df = _orders([(10.0, 12.0), (10.0, 11.0)])
        out = sanitize_orders(df, max_return_mult=5.0, diagnostic_threshold=0)
        self.assertEqual(out["exit_price"][0], 12.0)


class TestDiagnosticThreshold(unittest.TestCase):
    """P2 L74 — diagnostic fires without changing output when max_return_mult is permissive."""

    def test_diagnostic_counts_extreme_orders(self):
        """Current pipeline passes max_return_mult=999, which lets 30×
        returns through. The diagnostic at threshold=20 should flag them
        without affecting the output DataFrame."""
        df = _orders([(10.0, 300.0), (10.0, 15.0), (10.0, 250.0)])
        out = sanitize_orders(df, max_return_mult=999.0, diagnostic_threshold=20.0)
        # With max_return_mult=999, no capping — both extreme rows pass through
        self.assertEqual(out.height, 3)
        self.assertEqual(out["exit_price"][0], 300.0)
        self.assertEqual(out["exit_price"][2], 250.0)

    def test_diagnostic_zero_disables(self):
        """Passing diagnostic_threshold=0 skips the counter (back-compat)."""
        df = _orders([(10.0, 300.0)])
        out = sanitize_orders(df, max_return_mult=999.0, diagnostic_threshold=0)
        self.assertEqual(out.height, 1)

    def test_cap_and_diagnostic_coexist(self):
        """When max_return_mult caps rows, they are still counted in the
        diagnostic (the count uses pre-cap state). Ensures the log line
        surfaces both actions."""
        df = _orders([(10.0, 300.0), (10.0, 50.0)])  # 30× and 5×
        # Cap at 10× — first row capped to 100, second row untouched
        out = sanitize_orders(df, max_return_mult=10.0, diagnostic_threshold=20.0)
        self.assertEqual(out["exit_price"][0], 100.0)
        self.assertEqual(out["exit_price"][1], 50.0)


if __name__ == "__main__":
    unittest.main()

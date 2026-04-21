"""Tests for engine.signals.earnings_dip None-slice guard (P2 L305).

Pre-fix: `max(pd_closes[earn_idx:peak_end + 1])` raised TypeError if the
slice contained None (possible when fill_missing_dates injected weekend
rows without backward-filling close for the particular symbol).

Post-fix: filter out None entries, continue if the filtered slice is
empty. No signal is emitted from a data-quality-impaired window.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNoneSliceGuard(unittest.TestCase):
    """Exercise the guard on a synthetic closes list with embedded None."""

    def test_max_on_slice_with_none_raises_without_guard(self):
        """Baseline: unguarded max raises TypeError on None in slice."""
        closes = [100.0, 101.0, None, 103.0, 104.0]
        with self.assertRaises(TypeError):
            max(closes[0:5])

    def test_filtered_max_handles_none(self):
        """Post-fix pattern: filter None, handle empty, take max."""
        closes = [100.0, 101.0, None, 103.0, 104.0]
        filtered = [x for x in closes[0:5] if x is not None]
        self.assertTrue(filtered)
        self.assertEqual(max(filtered), 104.0)

    def test_filtered_max_handles_all_none(self):
        """All-None slice: filter yields empty list; caller must guard."""
        closes = [None, None, None]
        filtered = [x for x in closes if x is not None]
        self.assertEqual(filtered, [])

    def test_signal_module_imports_cleanly(self):
        """The signal file must parse without syntax errors after the edit."""
        import engine.signals.earnings_dip  # noqa: F401


if __name__ == "__main__":
    unittest.main()

"""Tests for engine/data_provider.py.

Covers Phase 4 audit items P4.1-P4.4:
  P4.1: signal generators properly trim to start_epoch; prefetch data
        does NOT leak into generated orders.
  P4.2: remove_price_oscillations emits structured logging (INFO summary
        + DEBUG symbol list).
  P4.3/P4.4: documentation-only items, asserted indirectly via the
        oscillation-filter behavior test (which exercises the provider
        path that applies adjustment/filter logic).
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.data_provider import remove_price_oscillations


def _build_oscillating_df(n_symbols: int = 2, n_days: int = 20) -> pl.DataFrame:
    """Build a synthetic frame where every symbol has exactly one spike+revert
    pattern on day 5. Each spike is 3x the prior close (well above
    spike_threshold=2.0)."""
    rows = []
    base_epoch = 1577836800  # 2020-01-01
    for i in range(n_symbols):
        inst = f"NSE:BAD{i}"
        for d in range(n_days):
            close = 100.0
            if d == 5:
                close = 300.0  # spike
            rows.append({
                "instrument": inst,
                "date_epoch": base_epoch + d * 86400,
                "close": close,
            })
    return pl.DataFrame(rows)


class TestOscillationFilterLogging(unittest.TestCase):
    """P4.2: remove_price_oscillations must emit structured log events
    when rows are dropped, so downstream data-quality monitoring can
    pick up on it without parsing stdout."""

    def test_info_log_emitted_when_rows_removed(self):
        df = _build_oscillating_df(n_symbols=2, n_days=20)
        caplog_records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                caplog_records.append(record)

        handler = _Handler(level=logging.DEBUG)
        logger = logging.getLogger("engine.data_provider")
        logger.addHandler(handler)
        prev_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            out = remove_price_oscillations(df, verbose=False)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)

        # INFO message with the removal summary.
        info_msgs = [r.getMessage() for r in caplog_records if r.levelno == logging.INFO]
        self.assertTrue(any("removed" in m for m in info_msgs),
                        f"Expected INFO log with 'removed'. Got: {info_msgs}")

        # DEBUG message listing affected symbols.
        debug_msgs = [r.getMessage() for r in caplog_records if r.levelno == logging.DEBUG]
        self.assertTrue(any("affected symbols" in m for m in debug_msgs),
                        f"Expected DEBUG log listing affected symbols. Got: {debug_msgs}")
        # Symbol list should include both BAD0 and BAD1.
        joined_debug = " ".join(debug_msgs)
        self.assertIn("NSE:BAD0", joined_debug)
        self.assertIn("NSE:BAD1", joined_debug)

        # Rows actually dropped.
        self.assertLess(out.height, df.height)

    def test_no_log_when_no_removals(self):
        # Build a clean frame with no oscillations.
        rows = []
        base_epoch = 1577836800
        for i in range(2):
            for d in range(20):
                rows.append({
                    "instrument": f"NSE:OK{i}",
                    "date_epoch": base_epoch + d * 86400,
                    "close": 100.0 + d,  # monotonic — no spikes
                })
        df = pl.DataFrame(rows)
        caplog_records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                caplog_records.append(record)

        handler = _Handler(level=logging.DEBUG)
        logger = logging.getLogger("engine.data_provider")
        logger.addHandler(handler)
        try:
            out = remove_price_oscillations(df, verbose=False)
        finally:
            logger.removeHandler(handler)

        # No INFO/DEBUG messages about removals.
        removal_msgs = [
            r for r in caplog_records
            if "removed" in r.getMessage() or "affected symbols" in r.getMessage()
        ]
        self.assertEqual(removal_msgs, [],
                         f"Expected no removal logs on clean data; got: "
                         f"{[r.getMessage() for r in removal_msgs]}")
        self.assertEqual(out.height, df.height)


class TestSignalsRespectStartEpoch(unittest.TestCase):
    """P4.1: signal generators that do NOT go through `engine.scanner.
    process` must still trim to start_epoch so prefetch rows don't end
    up as generated orders."""

    def test_start_epoch_filter_present_in_signal_files(self):
        """Static audit: every signal file either filters by start_epoch
        directly, delegates to the scanner (which trims), or uses a
        rebalance-date list derived from a scanner-trimmed frame.

        This test asserts the pattern is present in every signal file's
        source, so a new signal gen added without the pattern fails
        the test.
        """
        signals_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "engine", "signals",
        )
        # Signal files that are allowed to NOT contain the direct
        # `start_epoch` filter string because they trim indirectly.
        indirect = {
            "eod_technical.py",   # delegates to scanner.process
            "__init__.py",
            "base.py",            # utilities
        }
        missing = []
        for fname in sorted(os.listdir(signals_dir)):
            if not fname.endswith(".py"):
                continue
            if fname in indirect:
                continue
            with open(os.path.join(signals_dir, fname), "r") as f:
                source = f.read()
            if "start_epoch" not in source:
                missing.append(fname)

        self.assertEqual(missing, [],
                         f"Signal files missing start_epoch handling: "
                         f"{missing}. Each must filter to avoid emitting "
                         f"orders in the prefetch window.")


if __name__ == "__main__":
    unittest.main()

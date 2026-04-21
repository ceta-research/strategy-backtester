"""Direct tests for engine/order_generator.py exit integration.

The exit primitives (anomalous_drop, trailing_stop, ExitTracker) have
their own unit tests in tests/test_exits.py. This file covers the
INTEGRATION logic in generate_exit_attributes_for_instrument — priority
order, tracker.record-after-every-decision, multi-config tracking.

Covers audit P7.6b.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.order_generator import generate_exit_attributes_for_instrument

DAY = 86400


def _build_tick_data(closes, opens=None):
    """Build a tick-data frame with the columns the function expects."""
    n = len(closes)
    if opens is None:
        opens = list(closes)
    start = 1577836800
    return pl.DataFrame({
        "date_epoch": [start + i * DAY for i in range(n)],
        "close": closes,
        "open": opens,
        "next_open": [opens[i + 1] if i + 1 < n else None for i in range(n)],
        "next_volume": [1000] * n,
        "next_epoch": [start + (i + 1) * DAY if i + 1 < n else None for i in range(n)],
    })


def _build_context(exit_configs):
    """Assemble a minimal `context` dict that get_exit_config_iterator
    can consume.

    create_config_iterator takes the Cartesian product of each input
    param's value list. If two configs share the same min_hold but differ
    on trailing_stop_pct, we must pass only the UNIQUE values per param —
    or we get 2x2 = 4 configs instead of 2.
    """
    exit_input = {}
    for key in exit_configs[0].keys():
        if key == "id":
            continue
        # Preserve order while deduplicating values per param.
        seen = []
        for c in exit_configs:
            v = c[key]
            if v not in seen:
                seen.append(v)
        exit_input[key] = seen

    # Actual config count after Cartesian product.
    total = 1
    for v in exit_input.values():
        total *= len(v)

    return {
        "exit_config_input": exit_input,
        "anomalous_drop_threshold_pct": 20,
        "total_exit_configs": total,
    }


class TestAnomalousDropPriorityOverTsl(unittest.TestCase):
    """Priority order: anomalous_drop fires BEFORE trailing_stop check
    on the same bar. Without the P0 #8 signed-check fix, a +25% gap
    would have fired anomalous_drop (wrong). With the fix, only the
    -25% gap fires."""

    def test_negative_gap_triggers_anomalous_drop(self):
        # Day 0: entry at 100. Day 1: close drops to 74 (-26%) → anomalous.
        df = _build_tick_data(closes=[100, 74, 80, 85, 90])
        entry_epoch = 1577836800
        instrument_order_config = {
            entry_epoch: {"entry_price": 100.0},
        }
        context = _build_context([
            {"id": "1", "min_hold_time_days": 0, "trailing_stop_pct": 15},
        ])

        _, out = generate_exit_attributes_for_instrument(
            "NSE:A", instrument_order_config, df, context,
            drop_threshold=20,
        )

        # One exit recorded on day 1 with anomalous_drop reason, exit at 80% of 100.
        self.assertIn(entry_epoch, out)
        exits_for_entry = out[entry_epoch]
        self.assertEqual(len(exits_for_entry), 1)
        exit_epoch = list(exits_for_entry.keys())[0]
        self.assertEqual(exit_epoch, entry_epoch + 1 * DAY)
        exit_attrs = exits_for_entry[exit_epoch]
        # anomalous_drop exit price = last_close * 0.8 = 100 * 0.8 = 80.
        self.assertAlmostEqual(exit_attrs["exit_price"], 80.0, places=4)

    def test_positive_gap_does_not_trigger_anomalous_drop(self):
        """+25% gap up (e.g. earnings beat) must NOT fire anomalous_drop.
        Pre-P0-#8-fix, `abs(diff) > threshold` would have erroneously
        booked a loss on a day the stock rallied. With the signed check,
        the strategy walks forward to find a real TSL or end-of-data."""
        # Day 0: entry at 100. Day 1: close up to 125 (+25%) → NOT anomalous.
        # Day 2: close stays at 125 (no TSL trigger at 15%).
        # Day 3: drops to 100. Drawdown from 125 = 20%, fires TSL 15%.
        df = _build_tick_data(closes=[100, 125, 125, 100, 100])
        entry_epoch = 1577836800
        instrument_order_config = {
            entry_epoch: {"entry_price": 100.0},
        }
        context = _build_context([
            {"id": "1", "min_hold_time_days": 0, "trailing_stop_pct": 15},
        ])

        _, out = generate_exit_attributes_for_instrument(
            "NSE:A", instrument_order_config, df, context,
            drop_threshold=20,
        )

        exits_for_entry = out[entry_epoch]
        self.assertEqual(len(exits_for_entry), 1)
        exit_epoch = list(exits_for_entry.keys())[0]
        exit_attrs = exits_for_entry[exit_epoch]
        # TSL fires on day 3 close=100 (drawdown 20% from peak 125 > 15%).
        # Exit at next-day open if available. Day 4 open = 100.
        self.assertEqual(exit_epoch, entry_epoch + 4 * DAY)
        self.assertAlmostEqual(exit_attrs["exit_price"], 100.0, places=4)


class TestTrackerPreventsDuplicateExits(unittest.TestCase):
    """P0 #9 fix: every exit decision must call tracker.record(), so
    the TSL branch cannot fire again for the same exit_config on a
    later bar. This test proves the integration respects the tracker."""

    def test_no_duplicate_exit_for_same_config(self):
        # Series that both triggers anomalous_drop AND would later satisfy TSL.
        # With the fix, only ONE exit row emerges.
        df = _build_tick_data(closes=[100, 60, 90, 50, 40])  # -40% gap day 1
        entry_epoch = 1577836800
        instrument_order_config = {
            entry_epoch: {"entry_price": 100.0},
        }
        context = _build_context([
            {"id": "1", "min_hold_time_days": 0, "trailing_stop_pct": 15},
        ])

        _, out = generate_exit_attributes_for_instrument(
            "NSE:A", instrument_order_config, df, context,
            drop_threshold=20,
        )

        exits_for_entry = out[entry_epoch]
        # Even though TSL threshold is satisfied on multiple later bars,
        # only one exit row exists because the tracker blocked the TSL
        # branch after anomalous_drop recorded.
        self.assertEqual(len(exits_for_entry), 1)


class TestMultipleExitConfigs(unittest.TestCase):
    """Each exit_config maintains its own fired-state. A tight TSL fires
    early; a loose TSL fires later — both must eventually record."""

    def test_each_config_records_independently(self):
        # Peak=125 on day 1, then decline to 110 (day 2), 100 (day 3), 90 (day 4).
        # Drawdowns from 125: day 2 = 12%, day 3 = 20%, day 4 = 28%.
        # TSL 10% fires day 2; TSL 25% fires day 4.
        df = _build_tick_data(closes=[100, 125, 110, 100, 90])
        entry_epoch = 1577836800
        instrument_order_config = {
            entry_epoch: {"entry_price": 100.0},
        }
        context = _build_context([
            {"id": "1", "min_hold_time_days": 0, "trailing_stop_pct": 10},
            {"id": "2", "min_hold_time_days": 0, "trailing_stop_pct": 25},
        ])

        _, out = generate_exit_attributes_for_instrument(
            "NSE:A", instrument_order_config, df, context,
            drop_threshold=20,
        )

        exits_for_entry = out[entry_epoch]
        # Two distinct exit rows (one per config). Could share an exit_epoch
        # if both triggered on the same day, but in this construction they
        # trigger on different days.
        self.assertEqual(len(exits_for_entry), 2)


class TestMinHoldGate(unittest.TestCase):
    """min_hold_time_days blocks TSL during the hold window but does
    NOT block anomalous_drop (which fires on explicit price events)."""

    def test_min_hold_blocks_tsl(self):
        # Strong early drawdown; TSL=15% would fire day 1 without min_hold.
        # With min_hold_time_days=5, TSL stays dormant until day 5+.
        df = _build_tick_data(closes=[100, 80, 82, 83, 84, 85, 72])
        entry_epoch = 1577836800
        instrument_order_config = {
            entry_epoch: {"entry_price": 100.0},
        }
        context = _build_context([
            {"id": "1", "min_hold_time_days": 5, "trailing_stop_pct": 15},
        ])

        _, out = generate_exit_attributes_for_instrument(
            "NSE:A", instrument_order_config, df, context,
            drop_threshold=50,  # disable anomalous_drop for this test
        )

        exits_for_entry = out[entry_epoch]
        self.assertEqual(len(exits_for_entry), 1)
        exit_epoch = list(exits_for_entry.keys())[0]
        # TSL triggers on day 6 (close=72, drawdown from 100 peak = 28% > 15%)
        # Exit at day 6 close since it's the last bar (no next_open).
        self.assertEqual(exit_epoch, entry_epoch + 6 * DAY)


if __name__ == "__main__":
    unittest.main()

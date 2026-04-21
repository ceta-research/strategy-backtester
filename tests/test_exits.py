"""Tests for engine.exits primitives.

Locks in the fixes for audit P0s #8, #9, #10:

  P0 #8 — anomalous_drop must be SIGNED: positive gaps do NOT fire.
  P0 #9 — ExitTracker.record() is the only way to emit; all decisions
          go through it, so a subsequent exit check cannot fire for the
          same exit_config.
  P0 #10 — walk_forward_exit's `require_peak_recovery` is keyword-only
          and mandatory. Missing it raises.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exits import (
    anomalous_drop, end_of_data, trailing_stop, below_min_hold,
    max_hold_reached, ExitTracker, ExitDecision,
)


# ── P0 #8: anomalous_drop is SIGNED ──────────────────────────────────────

class TestAnomalousDropSigned(unittest.TestCase):

    def test_negative_gap_fires(self):
        # -25% drop (last=100, close=75), threshold=20 -> fire
        d = anomalous_drop(close_price=75.0, last_close=100.0,
                           drop_threshold_pct=20.0, this_epoch=1000)
        self.assertIsNotNone(d)
        self.assertEqual(d.reason, "anomalous_drop")
        self.assertAlmostEqual(d.exit_price, 80.0)  # 80% of 100

    def test_positive_gap_does_not_fire(self):
        """THE P0 #8 lock-in: +25% gap-up must NOT book a loss."""
        d = anomalous_drop(close_price=125.0, last_close=100.0,
                           drop_threshold_pct=20.0, this_epoch=1000)
        self.assertIsNone(d, "Positive gap must not trigger anomalous drop")

    def test_small_negative_move_does_not_fire(self):
        d = anomalous_drop(close_price=90.0, last_close=100.0,
                           drop_threshold_pct=20.0, this_epoch=1000)
        self.assertIsNone(d)

    def test_zero_last_close_guard(self):
        d = anomalous_drop(close_price=50.0, last_close=0.0,
                           drop_threshold_pct=20.0, this_epoch=1000)
        self.assertIsNone(d)

    def test_none_last_close_guard(self):
        d = anomalous_drop(close_price=50.0, last_close=None,
                           drop_threshold_pct=20.0, this_epoch=1000)
        self.assertIsNone(d)


# ── P0 #9: ExitTracker always records ────────────────────────────────────

class TestExitTracker(unittest.TestCase):

    def test_record_marks_fired(self):
        t = ExitTracker()
        self.assertFalse(t.has_fired(1))
        t.record(1)
        self.assertTrue(t.has_fired(1))

    def test_distinct_configs_tracked_separately(self):
        t = ExitTracker()
        t.record(1)
        self.assertTrue(t.has_fired(1))
        self.assertFalse(t.has_fired(2))

    def test_all_fired_detection(self):
        t = ExitTracker()
        t.record(1)
        t.record(2)
        self.assertTrue(t.all_fired(2))
        self.assertFalse(t.all_fired(3))


# ── P0 #10: walk_forward_exit require_peak_recovery is required ─────────

class TestWalkForwardExitRequiresExplicitGate(unittest.TestCase):
    """The old default was True; silent inheritance hid the breakout P0."""

    def test_missing_kwarg_raises(self):
        from engine.signals.base import walk_forward_exit
        with self.assertRaises(TypeError):
            walk_forward_exit(
                [1000, 2000], [100.0, 101.0], 0, 1000, 100.0, 100.0, 0.05, 10,
                # require_peak_recovery intentionally missing (keyword-only)
            )

    def test_percent_value_for_fraction_raises(self):
        """WFE-1 guard (code review 2026-04-21): passing a percent value
        like 5.0 for trailing_stop_fraction must raise, not silently
        disable TSL by computing `trail_high * (1 - 5.0)`."""
        from engine.signals.base import walk_forward_exit
        with self.assertRaises(ValueError) as cm:
            walk_forward_exit(
                [1000, 2000], [100.0, 101.0], 0, 1000, 100.0, 100.0,
                5.0,  # percent masquerading as fraction
                10, require_peak_recovery=False,
            )
        self.assertIn("trailing_stop_fraction", str(cm.exception))

    def test_explicit_false_breakout_semantics(self):
        """With require_peak_recovery=False, TSL activates immediately.
        Entry at 100, rally to 105, drop to 92 = 12% drawdown > 5% TSL -> fires."""
        from engine.signals.base import walk_forward_exit
        epochs = [1000, 2000, 3000]
        closes = [100.0, 105.0, 92.0]
        opens = [100.0, 105.0, 92.0]
        exit_epoch, exit_price = walk_forward_exit(
            epochs, closes, 0, 1000, 100.0, 100.0, 0.05, 0,
            opens=opens,
            require_peak_recovery=False,
        )
        self.assertIsNotNone(exit_epoch)

    def test_explicit_true_dip_buy_fires_after_recovery(self):
        """With require_peak_recovery=True: entry below peak, price
        recovers to peak, then drops — TSL activates only post-recovery."""
        from engine.signals.base import walk_forward_exit
        epochs = [1000, 2000, 3000, 4000, 5000]
        # recover to peak at day 3, then drop 10% -> TSL fires at day 4
        closes = [90.0, 95.0, 100.0, 88.0, 88.0]
        exit_epoch, exit_price = walk_forward_exit(
            epochs, closes, 0, 1000, 90.0, 100.0, 0.05, 0,
            require_peak_recovery=True,
        )
        self.assertEqual(exit_epoch, 4000)

    def test_explicit_true_never_recovers_falls_back_to_end(self):
        """If price never recovers to peak and no other exit triggers,
        walk_forward_exit returns the last bar as a safe default."""
        from engine.signals.base import walk_forward_exit
        epochs = [1000, 2000, 3000, 4000]
        closes = [90.0, 85.0, 80.0, 80.0]  # never touches peak=100
        exit_epoch, _ = walk_forward_exit(
            epochs, closes, 0, 1000, 90.0, 100.0, 0.05, 0,
            require_peak_recovery=True,
        )
        # Fallback: last bar
        self.assertEqual(exit_epoch, 4000)


# ── trailing_stop primitive ──────────────────────────────────────────────

class TestTrailingStop(unittest.TestCase):

    # Note: engine.exits.trailing_stop takes threshold as PERCENT (e.g. 5.0
    # for 5%), matching the call site in order_generator.py which uses
    # exit_config["trailing_stop_pct"] directly.

    def test_no_drawdown_no_exit(self):
        d = trailing_stop(close_price=100.0, max_price_since_entry=100.0,
                          trailing_stop_pct=5.0, next_epoch=2000,
                          next_open=100.0, this_epoch=1000)
        self.assertIsNone(d)

    def test_drawdown_exceeds_threshold_fires(self):
        d = trailing_stop(close_price=94.0, max_price_since_entry=100.0,
                          trailing_stop_pct=5.0, next_epoch=2000,
                          next_open=95.0, this_epoch=1000)
        self.assertIsNotNone(d)
        self.assertEqual(d.exit_epoch, 2000)
        self.assertEqual(d.exit_price, 95.0)

    def test_falls_back_to_close_when_no_next_open(self):
        d = trailing_stop(close_price=94.0, max_price_since_entry=100.0,
                          trailing_stop_pct=5.0, next_epoch=2000,
                          next_open=None, this_epoch=1000)
        self.assertIsNotNone(d)
        self.assertEqual(d.exit_epoch, 1000)
        self.assertEqual(d.exit_price, 94.0)

    def test_zero_threshold_disabled(self):
        d = trailing_stop(close_price=50.0, max_price_since_entry=100.0,
                          trailing_stop_pct=0.0, next_epoch=2000,
                          next_open=55.0, this_epoch=1000)
        self.assertIsNone(d)


if __name__ == "__main__":
    unittest.main()

"""Direct tests for engine.signals.base.walk_forward_exit.

This walker is called by 20+ signal generators as the central
trailing-stop-loss exit engine. Pre-Phase-7 there were no direct tests
— it was exercised only indirectly via test_pipeline.py.

Each test builds a synthetic price series with a known expected exit
and asserts (exit_epoch, exit_price) bit-exactly.

Covers audit P7.2. Footgun guards (WFE-1) and peak-recovery semantics
(P0 #10) are pinned so silent regressions fail the test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.signals.base import walk_forward_exit

DAY = 86400


def _epochs(n, start=1577836800):
    """Return n consecutive daily epochs starting 2020-01-01."""
    return [start + i * DAY for i in range(n)]


class TestPeakRecoveryModeTslZero(unittest.TestCase):
    """trailing_stop_fraction=0 means 'no TSL, exit on peak recovery'.
    This is the dip-buy-without-TSL variant."""

    def test_exit_when_close_reaches_peak(self):
        # entry=90, peak=100. Series recovers through peak on day 3.
        epochs = _epochs(5)
        closes = [90, 92, 95, 100, 105]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=90, peak_price=100,
            trailing_stop_fraction=0.0, max_hold_days=0,
            require_peak_recovery=True,
        )
        # Exit on day 3 (close=100); no opens provided so exit at close.
        self.assertEqual(result, (epochs[3], 100))

    def test_max_hold_terminates_before_peak(self):
        # Peak=200 never reached; max_hold=3 days kicks in.
        epochs = _epochs(10)
        closes = [90] * 10
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=90, peak_price=200,
            trailing_stop_fraction=0.0, max_hold_days=3,
            require_peak_recovery=True,
        )
        # hold_days >= 3 first becomes true on index 3 (epoch[0]+3*DAY).
        self.assertEqual(result, (epochs[3], 90))


class TestTslBreakoutMode(unittest.TestCase):
    """require_peak_recovery=False: TSL active from entry. Used by
    breakout / momentum strategies where entry IS at the peak."""

    def test_tsl_fires_exactly_at_threshold(self):
        # entry=100, trail_high tracks max close since entry.
        # Day 0: c=100, trail_high=100
        # Day 1: c=110, trail_high=110
        # Day 2: c=104.5 — drawdown from 110 is 5%, threshold 5% => fires.
        epochs = _epochs(5)
        closes = [100, 110, 104.5, 103, 102]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
        )
        # `c <= trail_high * (1 - frac)` = 104.5 <= 110 * 0.95 = 104.5 → fires.
        self.assertEqual(result, (epochs[2], 104.5))

    def test_tsl_does_not_fire_above_threshold(self):
        # trail_high=110, c=105: 105 > 104.5 → no fire, walks on.
        epochs = _epochs(5)
        closes = [100, 110, 105, 106, 107]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
        )
        # Reaches end of data; exit at last bar close.
        self.assertEqual(result, (epochs[-1], 107))


class TestTslPeakRecoveryGate(unittest.TestCase):
    """require_peak_recovery=True: TSL activates only after close reaches
    peak_price. Correct for dip-buy strategies."""

    def test_tsl_dormant_below_peak(self):
        # entry=90, peak=100. Prices dip further; TSL must NOT fire.
        epochs = _epochs(5)
        closes = [90, 85, 80, 75, 70]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=90, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=True,
        )
        # Peak never reached → no TSL → exit at last bar close.
        self.assertEqual(result, (epochs[-1], 70))

    def test_tsl_activates_after_peak_then_fires(self):
        # entry=90, peak=100.
        # Day 0: c=90  trail_high=90
        # Day 1: c=100 trail_high=100, peak reached
        # Day 2: c=110 trail_high=110, peak already reached
        # Day 3: c=104.5 — drawdown from 110 = 5%; TSL 5% threshold → fires.
        epochs = _epochs(5)
        closes = [90, 100, 110, 104.5, 103]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=90, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=True,
        )
        self.assertEqual(result, (epochs[3], 104.5))


class TestNextDayOpenExit(unittest.TestCase):
    """When opens are provided, TSL exits at the NEXT DAY'S open."""

    def test_exit_at_next_day_open_when_available(self):
        epochs = _epochs(5)
        closes = [100, 110, 104.5, 103, 102]
        opens = [99, 109, 103.5, 103.5, 101]  # next-day open for j=2 is opens[3]=103.5
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
            opens=opens,
        )
        # TSL triggers on j=2; _exit_at(j) returns (epochs[j+1], opens[j+1]).
        self.assertEqual(result, (epochs[3], 103.5))

    def test_falls_back_to_close_on_last_bar(self):
        # TSL fires on the LAST bar — no next-day open available.
        epochs = _epochs(3)
        closes = [100, 110, 104.5]
        opens = [99, 109, 103.5]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
            opens=opens,
        )
        # j=2 is last index; no opens[3]; exit at close[2].
        self.assertEqual(result, (epochs[2], 104.5))

    def test_falls_back_to_close_on_zero_next_open(self):
        # next_open is None/zero → fall through to close.
        epochs = _epochs(5)
        closes = [100, 110, 104.5, 103, 102]
        opens = [99, 109, 103.5, 0, 101]  # opens[3] = 0 (bad data)
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
            opens=opens,
        )
        # opens[3] = 0 → _exit_at falls back to (epochs[2], closes[2]).
        self.assertEqual(result, (epochs[2], 104.5))


class TestMaxHoldDays(unittest.TestCase):
    """max_hold_days=N forces exit on day where hold_days >= N."""

    def test_max_hold_wins_over_tsl(self):
        # TSL would not fire; max_hold=2 terminates.
        epochs = _epochs(10)
        closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.10, max_hold_days=2,
            require_peak_recovery=False,
        )
        # hold_days = 2 on j=2.
        self.assertEqual(result, (epochs[2], 102))

    def test_tsl_before_max_hold(self):
        # TSL fires earlier than max_hold.
        epochs = _epochs(20)
        closes = [100, 120, 108] + [100] * 17  # TSL 10% of 120 = 108
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.10, max_hold_days=10,
            require_peak_recovery=False,
        )
        # TSL fires on j=2 (c=108 <= 120*0.9=108). Exit before max_hold.
        self.assertEqual(result, (epochs[2], 108))


class TestNullCloseHandling(unittest.TestCase):
    """None close values are skipped (treat as missing data)."""

    def test_none_close_skipped(self):
        epochs = _epochs(5)
        closes = [100, None, 110, 104.5, 103]  # None on day 1
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=False,
        )
        # Day 1 skipped; day 2 updates trail_high=110; day 3 c=104.5 fires TSL.
        self.assertEqual(result, (epochs[3], 104.5))


class TestWFE1Guard(unittest.TestCase):
    """Runtime guard against passing a percent instead of a fraction."""

    def test_fraction_above_one_raises(self):
        epochs = _epochs(5)
        closes = [100, 105, 110, 95, 90]
        with self.assertRaises(ValueError) as ctx:
            walk_forward_exit(
                epochs, closes, start_idx=0,
                entry_epoch=epochs[0], entry_price=100, peak_price=100,
                trailing_stop_fraction=5.0,  # WFE-1 footgun: meant 5%
                max_hold_days=0,
                require_peak_recovery=False,
            )
        self.assertIn("trailing_stop_fraction", str(ctx.exception))
        self.assertIn("0, 1", str(ctx.exception))

    def test_negative_fraction_raises(self):
        epochs = _epochs(3)
        closes = [100, 101, 102]
        with self.assertRaises(ValueError):
            walk_forward_exit(
                epochs, closes, start_idx=0,
                entry_epoch=epochs[0], entry_price=100, peak_price=100,
                trailing_stop_fraction=-0.05,
                max_hold_days=0,
                require_peak_recovery=False,
            )


class TestEndOfDataFallback(unittest.TestCase):
    """With no trigger firing, exit at last available bar."""

    def test_exit_at_last_bar_close(self):
        epochs = _epochs(5)
        closes = [100, 102, 104, 106, 108]  # monotonic up; no TSL fire
        result = walk_forward_exit(
            epochs, closes, start_idx=0,
            entry_epoch=epochs[0], entry_price=100, peak_price=200,
            trailing_stop_fraction=0.05, max_hold_days=0,
            require_peak_recovery=True,  # peak=200 never reached
        )
        self.assertEqual(result, (epochs[-1], 108))

    def test_start_past_end_returns_none(self):
        epochs = _epochs(3)
        closes = [100, 101, 102]
        # start_idx >= len(epochs) should return (None, None).
        result = walk_forward_exit(
            epochs, closes, start_idx=5,
            entry_epoch=epochs[0], entry_price=100, peak_price=100,
            trailing_stop_fraction=0.0, max_hold_days=0,
            require_peak_recovery=False,
        )
        self.assertEqual(result, (None, None))


if __name__ == "__main__":
    unittest.main()

"""Regression tests for Layer 3: simulator end_epoch handling.

Audit P0 #6: pre-fix, simulator.process() derived end_epoch from
`df_orders["entry_epoch"].max()` when orders existed, producing three
silent bugs:

  1. Positions with exit_epoch > max(entry_epoch) were never exited;
     their trade row was never emitted in trade_log.
  2. MTM updates stopped at max(entry_epoch), so late positions missed
     drawdown evaluation.
  3. The loop used `>= end_epoch: break`, so end_epoch itself was excluded.

Post-fix, end_epoch comes from context and defaults to a close_at_mtm
policy: any still-open position gets a synthetic exit at its last known
close price, recorded with exit_reason="end_of_sim".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.simulator import process
from engine.constants import SECONDS_IN_ONE_DAY


def _make_trivial_context(start_epoch, end_epoch, start_margin=1_000_000):
    return {
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "start_margin": start_margin,
        "slippage_rate": 0.0005,
    }


def _make_sim_config():
    return {
        "max_positions": 5,
        "max_positions_per_instrument": 1,
    }


def _make_stats(epochs, instrument, close_price):
    """Build epoch_wise_instrument_stats for a single instrument across epochs."""
    return {e: {instrument: {"close": close_price, "avg_txn": 1_000_000}} for e in epochs}


class TestEndEpochAuthoritative(unittest.TestCase):
    """end_epoch comes from context, not from df_orders."""

    def test_position_opened_before_last_day_gets_exited_at_end(self):
        """A position with exit_epoch BEYOND the simulation window must be
        force-closed at end_epoch and appear in trade_log."""
        start = 1577836800  # 2020-01-01
        end = start + 30 * SECONDS_IN_ONE_DAY
        # Entry on day 5, natural exit scheduled for day 100 (beyond end_epoch)
        entry_epoch = start + 5 * SECONDS_IN_ONE_DAY
        natural_exit_epoch = start + 100 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": entry_epoch,
            "exit_epoch": natural_exit_epoch,
            "entry_price": 100.0,
            "exit_price": 110.0,
        }])

        # MTM data from entry day through end of simulation
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = _make_stats(mtm_epochs, "NSE:TEST", close_price=105.0)

        context = _make_trivial_context(start, end)
        sim_config = _make_sim_config()

        day_log, order_ids, snapshot, _, trade_log = process(
            context, df_orders, stats, {}, sim_config, "test")

        # Position must have been exited — not silently abandoned.
        self.assertEqual(len(trade_log), 1,
                         f"Expected 1 trade in trade_log, got {len(trade_log)}")
        trade = trade_log[0]
        self.assertEqual(trade.get("exit_reason"), "end_of_sim")
        # Exit should be at the last MTM close (105.0), not the planned 110.0
        self.assertAlmostEqual(trade["exit_price"], 105.0)
        self.assertEqual(trade["exit_epoch"], end)

    def test_no_open_positions_at_end(self):
        """After end-of-sim policy, snapshot must have no open positions."""
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        entry_epoch = start + 5 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": entry_epoch,
            "exit_epoch": start + 100 * SECONDS_IN_ONE_DAY,
            "entry_price": 100.0,
            "exit_price": 110.0,
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = _make_stats(mtm_epochs, "NSE:TEST", close_price=105.0)

        _, _, snapshot, _, _ = process(
            _make_trivial_context(start, end), df_orders, stats, {},
            _make_sim_config(), "test")

        self.assertEqual(len(snapshot["current_positions"]), 0,
                         "All positions must be closed by end_of_sim policy")
        self.assertEqual(snapshot["current_positions_count"], 0)

    def test_end_epoch_itself_is_processed(self):
        """Loop uses `> end_epoch`, so the last day IS included in MTM."""
        start = 1577836800
        end = start + 10 * SECONDS_IN_ONE_DAY
        entry_epoch = start + 2 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": entry_epoch,
            "exit_epoch": start + 5 * SECONDS_IN_ONE_DAY,
            "entry_price": 100.0,
            "exit_price": 110.0,
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 11)]
        stats = _make_stats(mtm_epochs, "NSE:TEST", close_price=105.0)

        day_log, _, _, _, _ = process(
            _make_trivial_context(start, end), df_orders, stats, {},
            _make_sim_config(), "test")

        # day_log must include an entry for end_epoch itself.
        last_entry = day_log[-1]
        self.assertEqual(last_entry["log_date_epoch"], end,
                         f"Last MTM log should be at end_epoch={end}, got {last_entry['log_date_epoch']}")

    def test_no_orders_still_uses_context_end_epoch(self):
        """Even with empty df_orders, simulation window is authoritative."""
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        df_orders = pl.DataFrame(schema={
            "instrument": pl.Utf8, "entry_epoch": pl.Int64,
            "exit_epoch": pl.Int64, "entry_price": pl.Float64,
            "exit_price": pl.Float64,
        })

        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = _make_stats(mtm_epochs, "NSE:TEST", close_price=100.0)

        day_log, _, snapshot, _, trade_log = process(
            _make_trivial_context(start, end), df_orders, stats, {},
            _make_sim_config(), "test")

        self.assertEqual(snapshot["simulation_date"], end)
        self.assertEqual(len(trade_log), 0)


if __name__ == "__main__":
    unittest.main()

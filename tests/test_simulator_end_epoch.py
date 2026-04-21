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
from engine.order_key import OrderKey


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

    def test_unknown_end_of_sim_policy_raises(self):
        """Code-review finding SIM-2: unknown end_of_sim_policy strings must
        raise, not silently skip the force-close block."""
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": start + 5 * SECONDS_IN_ONE_DAY,
            "exit_epoch": start + 100 * SECONDS_IN_ONE_DAY,
            "entry_price": 100.0, "exit_price": 110.0,
        }])
        mtm_epochs = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = _make_stats(mtm_epochs, "NSE:TEST", close_price=105.0)

        context = _make_trivial_context(start, end)
        context["end_of_sim_policy"] = "abandon"  # unsupported

        with self.assertRaises(ValueError) as cm:
            process(context, df_orders, stats, {}, _make_sim_config(), "test")
        self.assertIn("end_of_sim_policy", str(cm.exception))

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


class TestDecision7EndEpochWalkBack(unittest.TestCase):
    """Decision 7 (code review 2026-04-21): when end_epoch falls on a
    non-trading day (weekend, holiday), the end-of-sim force-close must
    walk back to the nearest prior MTM day so that exit_epoch and
    exit_price correspond to the same bar. Emit a warning so callers
    can see the alignment happened.
    """

    def test_end_epoch_on_non_trading_day_walks_back(self):
        """end_epoch at day 30, but MTM data only goes up to day 28.
        Force-close must record exit at day 28 (the nearest prior MTM day),
        not day 30 (the non-trading nominal end)."""
        start = 1577836800  # 2020-01-01
        end = start + 30 * SECONDS_IN_ONE_DAY  # "end_epoch" config value
        entry_epoch = start + 5 * SECONDS_IN_ONE_DAY
        natural_exit_epoch = start + 100 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": entry_epoch,
            "exit_epoch": natural_exit_epoch,
            "entry_price": 100.0, "exit_price": 110.0,
        }])

        # MTM data only through day 28 — days 29 and 30 are "holidays".
        # end_epoch (day 30) is NOT in mtm_epochs.
        mtm_epochs_list = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 29)]
        stats = _make_stats(mtm_epochs_list, "NSE:TEST", close_price=105.0)

        context = _make_trivial_context(start, end)

        _, _, snapshot, _, trade_log = process(
            context, df_orders, stats, {}, _make_sim_config(), "walkback_test")

        # Exactly one forced-close trade.
        self.assertEqual(len(trade_log), 1)
        trade = trade_log[0]
        self.assertEqual(trade["exit_reason"], "end_of_sim")
        # exit_epoch should be the LAST MTM day (day 28), not end_epoch (day 30).
        expected_effective_end = start + 28 * SECONDS_IN_ONE_DAY
        self.assertEqual(trade["exit_epoch"], expected_effective_end,
                         f"Expected walk-back to {expected_effective_end}, got {trade['exit_epoch']}")
        # Warning must be emitted.
        warnings = snapshot.get("warnings", [])
        self.assertTrue(
            any("not a trading day" in w for w in warnings),
            f"Expected walk-back warning, got: {warnings}"
        )

    def test_end_epoch_in_mtm_epochs_no_walkback_no_warning(self):
        """Happy path: end_epoch IS a trading day. No walk-back; no warning.
        Regression-guards the existing end-of-sim contract."""
        start = 1577836800
        end = start + 30 * SECONDS_IN_ONE_DAY
        entry_epoch = start + 5 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": entry_epoch,
            "exit_epoch": start + 100 * SECONDS_IN_ONE_DAY,
            "entry_price": 100.0, "exit_price": 110.0,
        }])
        # end_epoch IS in mtm_epochs (day 30 is a data day).
        mtm_epochs_list = [start + d * SECONDS_IN_ONE_DAY for d in range(0, 31)]
        stats = _make_stats(mtm_epochs_list, "NSE:TEST", close_price=105.0)

        _, _, snapshot, _, trade_log = process(
            _make_trivial_context(start, end), df_orders, stats, {},
            _make_sim_config(), "no_walkback_test")

        self.assertEqual(len(trade_log), 1)
        self.assertEqual(trade_log[0]["exit_epoch"], end)
        # No warnings key added for the happy path.
        self.assertNotIn("warnings", snapshot)

    def test_end_epoch_before_any_mtm_data(self):
        """Degenerate case: end_epoch precedes all MTM epochs. Should
        fall back to end_epoch verbatim and emit a distinct warning
        rather than crash on an empty bisect."""
        start = 1577836800
        # Instrument has data from day 10 onwards. But end_epoch is day 5.
        end = start + 5 * SECONDS_IN_ONE_DAY
        # Pre-load a snapshot with an open position so end-of-sim has work to do.
        prior_snapshot = {
            "margin_available": 900_000,
            "current_position_value": 100_000,
            "simulation_date": start,
            "current_positions_count": 1,
            "max_account_value": 1_000_000,
            "current_positions": {
                "NSE:ORPHAN": {
                    OrderKey(
                        instrument="NSE:ORPHAN",
                        entry_epoch=start - SECONDS_IN_ONE_DAY,
                        exit_epoch=start + 100 * SECONDS_IN_ONE_DAY,
                        entry_config_ids="e0",
                    ): {
                        "instrument": "NSE:ORPHAN",
                        "entry_epoch": start - SECONDS_IN_ONE_DAY,
                        "exit_epoch": start + 100 * SECONDS_IN_ONE_DAY,
                        "entry_price": 100.0,
                        "exit_price": 110.0,
                        "quantity": 100,
                        "last_close_price": 100.0,
                        "entry_charges": 10.0,
                        "entry_slippage": 5.0,
                        "entry_config_ids": "e0",
                        "exit_reason": "natural",
                    }
                }
            },
        }
        # MTM data starts AFTER end_epoch.
        mtm_epochs_list = [start + d * SECONDS_IN_ONE_DAY for d in range(10, 15)]
        stats = _make_stats(mtm_epochs_list, "NSE:ORPHAN", close_price=105.0)

        df_orders = pl.DataFrame(schema={
            "instrument": pl.Utf8, "entry_epoch": pl.Int64,
            "exit_epoch": pl.Int64, "entry_price": pl.Float64,
            "exit_price": pl.Float64,
        })

        _, _, snapshot, _, trade_log = process(
            _make_trivial_context(start, end), df_orders, stats,
            prior_snapshot, _make_sim_config(), "orphan_test")

        # The open snapshot position is force-closed.
        self.assertEqual(len(trade_log), 1)
        # exit_epoch falls back to end_epoch literally (no prior MTM day).
        self.assertEqual(trade_log[0]["exit_epoch"], end)
        # Warning must flag the degenerate case.
        warnings = snapshot.get("warnings", [])
        self.assertTrue(
            any("precedes all MTM data" in w for w in warnings),
            f"Expected degenerate-case warning, got: {warnings}"
        )


if __name__ == "__main__":
    unittest.main()

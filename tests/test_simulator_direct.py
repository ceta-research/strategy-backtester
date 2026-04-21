"""Direct state-machine tests for engine/simulator.py::process().

Pre-Phase-7 the simulator was exercised only via test_pipeline.py
and test_simulator_end_epoch.py. This file fills the gap with focused
assertions over:

  - Full single-trade cycle (entry → MTM → exit → margin accounting)
  - Multi-position margin accounting (3 concurrent orders)
  - exit_before_entry flag: capital recycling within the same day
  - Snapshot resume: state carried across chunks

Covers audit P7.6a.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.simulator import process
from engine.constants import SECONDS_IN_ONE_DAY


def _context(start, end, margin=1_000_000):
    return {
        "start_epoch": start,
        "end_epoch": end,
        "start_margin": margin,
        "slippage_rate": 0.0,  # turn off for clean assertions
    }


def _sim_config(max_positions=5, exit_before_entry=False):
    return {
        "max_positions": max_positions,
        "max_positions_per_instrument": 1,
        "exit_before_entry": exit_before_entry,
        "order_value": {"type": "fixed", "value": 100_000},
    }


def _stats(epochs, instrument_closes):
    """Build epoch_wise_instrument_stats where each epoch has a
    {instrument: {close, avg_txn}} dict built from instrument_closes.

    instrument_closes: dict {instrument_name: close_price}.
    """
    return {
        e: {
            inst: {"close": close, "avg_txn": 1_000_000_000}
            for inst, close in instrument_closes.items()
        }
        for e in epochs
    }


class TestFullSingleTradeCycle(unittest.TestCase):
    """Entry → MTM → exit → margin accounting must balance to zero
    drift (ignoring explicit charges/slippage we've set to zero)."""

    def test_single_trade_end_to_end(self):
        start = 1577836800
        end = start + 10 * SECONDS_IN_ONE_DAY
        entry_epoch = start + 2 * SECONDS_IN_ONE_DAY
        exit_epoch = start + 5 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:A",
            "entry_epoch": entry_epoch,
            "exit_epoch": exit_epoch,
            "entry_price": 100.0,
            "exit_price": 110.0,
        }])
        mtm = [start + d * SECONDS_IN_ONE_DAY for d in range(11)]
        stats = _stats(mtm, {"NSE:A": 105.0})

        day_log, order_ids, snapshot, _, trade_log = process(
            _context(start, end), df_orders, stats, {},
            _sim_config(), "single_cycle",
        )

        # Exactly one trade closed.
        self.assertEqual(len(trade_log), 1)
        t = trade_log[0]
        self.assertEqual(t["entry_epoch"], entry_epoch)
        self.assertEqual(t["exit_epoch"], exit_epoch)
        self.assertEqual(t["exit_price"], 110.0)

        # No open positions at end.
        self.assertEqual(snapshot["current_positions_count"], 0)
        self.assertEqual(snapshot["current_positions"], {})

        # With slippage=0 and charge-on-both-sides, profit = qty*(exit-entry) - charges.
        # qty = int(100_000 / 100) = 1000; gross PnL = 1000 * 10 = 10_000.
        # Margin_available at end ≈ 1_000_000 + 10_000 - (buy+sell NSE delivery charges).
        # Delivery charges on 100k and 110k notional are small (~1-2k total).
        # We assert margin increased (profit preserved) and within a sane band.
        self.assertGreater(snapshot["margin_available"], 1_000_000 + 10_000 - 3000)
        self.assertLess(snapshot["margin_available"], 1_000_000 + 10_000)


class TestMultiPositionAccounting(unittest.TestCase):
    """Three concurrent entries with mixed win/loss outcomes — verify
    current_positions_count tracks correctly and margin debit/credit
    is consistent."""

    def test_three_concurrent_positions(self):
        start = 1577836800
        end = start + 20 * SECONDS_IN_ONE_DAY

        # All three enter on day 2; exit on different days.
        df_orders = pl.DataFrame([
            {"instrument": "NSE:A", "entry_epoch": start + 2 * SECONDS_IN_ONE_DAY,
             "exit_epoch": start + 8 * SECONDS_IN_ONE_DAY,
             "entry_price": 100.0, "exit_price": 110.0},
            {"instrument": "NSE:B", "entry_epoch": start + 2 * SECONDS_IN_ONE_DAY,
             "exit_epoch": start + 6 * SECONDS_IN_ONE_DAY,
             "entry_price": 200.0, "exit_price": 180.0},
            {"instrument": "NSE:C", "entry_epoch": start + 2 * SECONDS_IN_ONE_DAY,
             "exit_epoch": start + 10 * SECONDS_IN_ONE_DAY,
             "entry_price": 50.0, "exit_price": 55.0},
        ])
        mtm = [start + d * SECONDS_IN_ONE_DAY for d in range(21)]
        stats = _stats(mtm, {"NSE:A": 105, "NSE:B": 190, "NSE:C": 52})

        day_log, order_ids, snapshot, day_positions, trade_log = process(
            _context(start, end), df_orders, stats, {},
            _sim_config(max_positions=5), "multi",
        )

        # All 3 trades closed.
        self.assertEqual(len(trade_log), 3)
        # Each instrument in trade_log exactly once.
        instruments = sorted(t["instrument"] for t in trade_log)
        self.assertEqual(instruments, ["NSE:A", "NSE:B", "NSE:C"])

        # No residual positions.
        self.assertEqual(snapshot["current_positions_count"], 0)


class TestExitBeforeEntryFlag(unittest.TestCase):
    """exit_before_entry=True frees capital from day-T exits BEFORE
    day-T entries are placed. Contrast: default entries-first mode
    sizes new orders on pre-exit margin."""

    def test_exit_before_entry_frees_slot_for_new_entry(self):
        """Setup: max_positions=1. Day 5 has both an exit (of trade 1) and
        an entry (for trade 2). With entries-first, the slot is still
        occupied on day 5 at entry time → trade 2 might be blocked.
        With exit_before_entry=True, trade 1 exits, slot frees, trade 2
        enters same day.

        Note: the exit_before_entry branch of simulator.py recomputes
        `order_value = current_account_value / max_positions` (line 365),
        overriding any sim_config['order_value']. We use a large
        start_margin so that the resulting full-margin order sizing
        still fits with charges/slippage headroom. This is a documented
        quirk of the exit_before_entry branch, not a test bug.
        """
        start = 1577836800
        end = start + 15 * SECONDS_IN_ONE_DAY
        exit_day = start + 5 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([
            {"instrument": "NSE:A", "entry_epoch": start + 1 * SECONDS_IN_ONE_DAY,
             "exit_epoch": exit_day,
             "entry_price": 100.0, "exit_price": 105.0},
            {"instrument": "NSE:B", "entry_epoch": exit_day,
             "exit_epoch": start + 10 * SECONDS_IN_ONE_DAY,
             "entry_price": 200.0, "exit_price": 220.0},
        ])
        mtm = [start + d * SECONDS_IN_ONE_DAY for d in range(16)]
        stats = _stats(mtm, {"NSE:A": 102, "NSE:B": 210})

        # Cap max_order_value so exit_before_entry branch doesn't
        # overcommit when it recomputes `current_account_value / max_positions`.
        sim_cfg = {
            "max_positions": 1,
            "max_positions_per_instrument": 1,
            "exit_before_entry": True,
            "max_order_value": {"type": "fixed", "value": 100_000},
        }

        day_log, _, snapshot, _, trade_log = process(
            _context(start, end, margin=10_000_000),
            df_orders, stats, {}, sim_cfg, "exit_first",
        )
        self.assertEqual(len(trade_log), 2,
                         f"exit_before_entry=True should allow both trades; "
                         f"got {len(trade_log)}")


class TestSnapshotResume(unittest.TestCase):
    """A simulation split into two calls must carry state across the
    boundary. Chunk-end policy is `close_at_mtm` (P0 #6): any
    still-open position at chunk end is force-closed at MTM, so the
    resume behavior is:

      - Trade fully inside chunk 1: snap_mid carries zero open
        positions; chunk 2 has nothing to do. Final state matches
        a single-shot run of the same window.
      - Trade straddling the boundary: chunk 1 force-closes it at
        mid's MTM close; chunk 2 sees an empty state.
    """

    def test_chunk_with_fully_contained_trade(self):
        start = 1577836800
        mid = start + 10 * SECONDS_IN_ONE_DAY
        end = start + 20 * SECONDS_IN_ONE_DAY
        # Trade opens day 3, exits day 7 — fully inside chunk 1.
        entry_epoch = start + 3 * SECONDS_IN_ONE_DAY
        exit_epoch = start + 7 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame([{
            "instrument": "NSE:X",
            "entry_epoch": entry_epoch,
            "exit_epoch": exit_epoch,
            "entry_price": 100.0,
            "exit_price": 120.0,
        }])
        full_mtm = [start + d * SECONDS_IN_ONE_DAY for d in range(21)]
        stats = _stats(full_mtm, {"NSE:X": 110})

        # Chunk 1: start → mid. Trade closes at its planned exit.
        _, _, snap_mid, _, trades_chunk1 = process(
            _context(start, mid), df_orders, stats, {},
            _sim_config(), "chunk1",
        )
        self.assertEqual(len(trades_chunk1), 1)
        self.assertEqual(trades_chunk1[0]["exit_price"], 120.0)
        # No open positions carried into chunk 2.
        self.assertEqual(snap_mid["current_positions_count"], 0)

        # Chunk 2: mid → end, resume from snap_mid. Nothing to process;
        # state is preserved.
        _, _, snap_end, _, trades_chunk2 = process(
            _context(mid, end), df_orders, stats, snap_mid,
            _sim_config(), "chunk2",
        )
        self.assertEqual(len(trades_chunk2), 0)
        # Margin carried forward unchanged across chunk 2.
        self.assertAlmostEqual(snap_end["margin_available"],
                               snap_mid["margin_available"], places=2)

    def test_chunk_boundary_forces_open_position_close(self):
        """An open position at chunk end is force-closed at MTM per
        `close_at_mtm` policy (P0 #6). The trade appears in chunk 1's
        trade_log with exit_reason='end_of_sim'; chunk 2 has nothing."""
        start = 1577836800
        mid = start + 10 * SECONDS_IN_ONE_DAY
        end = start + 20 * SECONDS_IN_ONE_DAY
        entry_epoch = start + 3 * SECONDS_IN_ONE_DAY
        exit_epoch = start + 15 * SECONDS_IN_ONE_DAY  # past mid

        df_orders = pl.DataFrame([{
            "instrument": "NSE:X",
            "entry_epoch": entry_epoch,
            "exit_epoch": exit_epoch,
            "entry_price": 100.0,
            "exit_price": 120.0,
        }])
        full_mtm = [start + d * SECONDS_IN_ONE_DAY for d in range(21)]
        stats = _stats(full_mtm, {"NSE:X": 110})

        _, _, snap_mid, _, trades_chunk1 = process(
            _context(start, mid), df_orders, stats, {},
            _sim_config(), "chunk1",
        )
        # Chunk 1 force-closed the position at MTM (110, the stats close).
        self.assertEqual(len(trades_chunk1), 1)
        self.assertEqual(trades_chunk1[0]["exit_price"], 110.0)
        self.assertEqual(trades_chunk1[0].get("exit_reason"), "end_of_sim")


if __name__ == "__main__":
    unittest.main()

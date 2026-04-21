"""Tests for engine.order_key and the tiered-collision fix.

Audit P0 #7: pre-fix, the simulator identified positions with the string
f"{instrument}_{entry_epoch}_{exit_epoch}". Tiered strategies emit
multiple orders at the same (instrument, entry_epoch, exit_epoch) but
different tier indices — they all hashed to the same position slot, and
later tiers silently overwrote earlier ones.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.order_key import OrderKey
from engine.simulator import process
from engine.constants import SECONDS_IN_ONE_DAY


class TestOrderKeyHashing(unittest.TestCase):

    def test_distinct_tiers_have_distinct_keys(self):
        k0 = OrderKey("NSE:TCS", 1000, 2000, "5_t0")
        k1 = OrderKey("NSE:TCS", 1000, 2000, "5_t1")
        k2 = OrderKey("NSE:TCS", 1000, 2000, "5_t2")
        self.assertNotEqual(k0, k1)
        self.assertNotEqual(k1, k2)
        d = {k0: "a", k1: "b", k2: "c"}
        self.assertEqual(len(d), 3)

    def test_same_fields_same_key(self):
        k1 = OrderKey("NSE:TCS", 1000, 2000, "5")
        k2 = OrderKey("NSE:TCS", 1000, 2000, "5")
        self.assertEqual(k1, k2)
        self.assertEqual(hash(k1), hash(k2))

    def test_frozen_unmutable(self):
        k = OrderKey("NSE:TCS", 1000, 2000, "5")
        with self.assertRaises(AttributeError):
            k.instrument = "NSE:INFY"  # type: ignore

    def test_str_format_includes_tier(self):
        k = OrderKey("NSE:TCS", 1000, 2000, "5_t1")
        s = str(k)
        self.assertIn("NSE:TCS", s)
        self.assertIn("5_t1", s)

    def test_str_format_omits_empty_config_ids(self):
        k = OrderKey("NSE:TCS", 1000, 2000, "")
        self.assertEqual(str(k), "NSE:TCS_1000_2000")

    def test_from_order_handles_missing_entry_config_ids(self):
        k = OrderKey.from_order({"instrument": "NSE:TCS",
                                 "entry_epoch": 1000, "exit_epoch": 2000})
        self.assertEqual(k.entry_config_ids, "")


class TestTieredCollisionFix(unittest.TestCase):
    """End-to-end check: two tier-variant orders at the same
    (instrument, entry_epoch, exit_epoch) both execute successfully."""

    def test_two_tiers_both_exit_cleanly(self):
        start = 1577836800
        end = start + 20 * SECONDS_IN_ONE_DAY
        entry = start + 2 * SECONDS_IN_ONE_DAY
        exit_ = start + 10 * SECONDS_IN_ONE_DAY

        # Two tiers of the SAME entry config, same instrument, same epochs.
        # Pre-fix, the second would silently overwrite the first.
        orders = pl.DataFrame([
            {
                "instrument": "NSE:TIERED",
                "entry_epoch": entry, "exit_epoch": exit_,
                "entry_price": 100.0, "exit_price": 105.0,
                "scanner_config_ids": "s0",
                "entry_config_ids": "5_t0", "exit_config_ids": "x0",
            },
            {
                "instrument": "NSE:TIERED",
                "entry_epoch": entry, "exit_epoch": exit_,
                "entry_price": 95.0, "exit_price": 105.0,  # deeper tier, diff entry_price
                "scanner_config_ids": "s0",
                "entry_config_ids": "5_t1", "exit_config_ids": "x0",
            },
        ])
        stats = {
            start + d * SECONDS_IN_ONE_DAY: {
                "NSE:TIERED": {"close": 100.0, "avg_txn": 10_000_000}
            }
            for d in range(21)
        }

        context = {
            "start_margin": 1_000_000,
            "start_epoch": start,
            "end_epoch": end,
            "slippage_rate": 0.0005,
        }
        sim_cfg = {
            "max_positions": 5,
            "max_positions_per_instrument": 2,  # allow both tiers open
        }

        _, order_ids, _, _, trade_log = process(
            context, orders, stats, {}, sim_cfg, "tiered_test")

        # Both tiers must have opened AND closed — 2 trade rows.
        self.assertEqual(len(trade_log), 2,
                         f"Expected 2 trades (one per tier), got {len(trade_log)}")
        # Both tier's OrderKeys should be distinct in config_order_ids.
        self.assertEqual(len(set(order_ids)), 2,
                         f"Tier OrderKeys collided: {order_ids}")

    def test_exact_duplicate_rejected(self):
        """Genuine duplicate (same OrderKey fields) is now a hard error.
        Pre-fix, this silently overwrote; post-fix, it raises."""
        start = 1577836800
        end = start + 20 * SECONDS_IN_ONE_DAY
        entry = start + 2 * SECONDS_IN_ONE_DAY
        exit_ = start + 10 * SECONDS_IN_ONE_DAY

        orders = pl.DataFrame([
            {
                "instrument": "NSE:DUP",
                "entry_epoch": entry, "exit_epoch": exit_,
                "entry_price": 100.0, "exit_price": 105.0,
                "scanner_config_ids": "s0",
                "entry_config_ids": "5", "exit_config_ids": "x0",
            },
            {
                "instrument": "NSE:DUP",
                "entry_epoch": entry, "exit_epoch": exit_,
                "entry_price": 100.0, "exit_price": 105.0,
                "scanner_config_ids": "s0",
                "entry_config_ids": "5", "exit_config_ids": "x0",  # identical
            },
        ])
        stats = {
            start + d * SECONDS_IN_ONE_DAY: {
                "NSE:DUP": {"close": 100.0, "avg_txn": 10_000_000}
            }
            for d in range(21)
        }

        context = {
            "start_margin": 1_000_000, "start_epoch": start, "end_epoch": end,
            "slippage_rate": 0.0005,
        }
        sim_cfg = {"max_positions": 5, "max_positions_per_instrument": 2}

        with self.assertRaises(ValueError) as cm:
            process(context, orders, stats, {}, sim_cfg, "dup_test")
        self.assertIn("Duplicate OrderKey", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

"""Tests for engine.simulator order_value computation.

Three order-sizing modes: fixed, percentage_of_account_value,
percentage_of_available_margin. Plus order_value_multiplier scaling.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine import simulator


def _build_orders(entry_epoch, exit_epoch, entry_price=100.0, exit_price=110.0):
    """One synthetic order: buy at entry_price, exit at exit_price."""
    return pl.DataFrame(
        {
            "instrument": ["TEST"],
            "exchange": ["NSE"],
            "entry_epoch": [entry_epoch],
            "exit_epoch": [exit_epoch],
            "entry_price": [float(entry_price)],
            "exit_price": [float(exit_price)],
            "entry_config_ids": ["1"],
            "exit_config_ids": ["1"],
            "scanner_config_ids": ["1"],
            "entry_config_id": ["1"],
            "exit_config_id": ["1"],
            "trade_type": ["DELIVERY"],
        }
    )


def _build_stats(entry_epoch, exit_epoch, entry_price, exit_price):
    """Minimal epoch_wise_instrument_stats: two trading days."""
    return {
        entry_epoch: {"TEST": {"close": entry_price, "avg_txn": 1_000_000_000}},
        exit_epoch: {"TEST": {"close": exit_price, "avg_txn": 1_000_000_000}},
    }


def _run(order_value_cfg, start_margin=1_000_000, multiplier=None):
    """Run one-order simulation and return (first_trade, day_wise_log)."""
    entry_epoch = 1_600_000_000
    exit_epoch = entry_epoch + 86400
    context = {
        "start_margin": start_margin,
        "start_epoch": entry_epoch,
        "end_epoch": exit_epoch,
        "prefetch_days": 0,
        "total_exit_configs": 1,
        "slippage_rate": 0.0,  # isolate sizing
    }
    sim_cfg = {
        "id": 1,
        "max_positions": 10,
        "max_positions_per_instrument": 1,
    }
    if order_value_cfg is not None:
        sim_cfg["order_value"] = order_value_cfg
    if multiplier is not None:
        sim_cfg["order_value_multiplier"] = multiplier

    df_orders = _build_orders(entry_epoch, exit_epoch)
    stats = _build_stats(entry_epoch, exit_epoch, 100.0, 110.0)

    day_log, _ids, _snap, _pos, trade_log = simulator.process(
        context, df_orders, stats, {}, sim_cfg, "cfg1"
    )
    return trade_log, day_log


class TestOrderValueTypes(unittest.TestCase):

    def test_fixed_sizes_at_literal_value(self):
        """fixed: quantity = int(200000 / 100) = 2000 shares."""
        trades, _ = _run({"type": "fixed", "value": 200_000})
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 2000)

    def test_percentage_of_account_value(self):
        """10% of 1_000_000 account → 100_000 notional → 1000 shares at ₹100."""
        trades, _ = _run({"type": "percentage_of_account_value", "value": 10.0})
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 1000)

    def test_percentage_of_available_margin(self):
        """20% of 1_000_000 cash (no prior positions) → 200_000 → 2000 shares."""
        trades, _ = _run({"type": "percentage_of_available_margin", "value": 20.0})
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 2000)

    def test_default_sizing_account_over_max_positions(self):
        """No order_value cfg → account_value / max_positions (10) = 100_000 → 1000 sh."""
        trades, _ = _run(None)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 1000)

    def test_order_value_multiplier_scales_fixed(self):
        """fixed 100k × 2× multiplier → 200_000 notional → 2000 shares."""
        trades, _ = _run({"type": "fixed", "value": 100_000}, multiplier=2)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 2000)

if __name__ == "__main__":
    unittest.main()

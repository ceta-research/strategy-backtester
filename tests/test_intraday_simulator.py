"""Tests for engine/intraday_simulator.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_simulator import simulate_intraday, _date_to_epoch, _get_charges_fn
from engine.charges import nse_intraday_charges, us_intraday_charges


DEFAULT_CONFIG = {
    "initial_capital": 500000,
    "max_positions": 5,
    "order_value": 50000,
}


def make_trade(symbol="TEST.NS", trade_date="2024-01-15",
               entry_price=100.0, exit_price=101.5,
               exit_type="signal", signal_strength=0.05, bench_ret=0.001,
               entry_bar=16):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_type": exit_type,
        "signal_strength": signal_strength,
        "bench_ret": bench_ret,
        "entry_bar": entry_bar,
    }


class TestSimulateIntraday(unittest.TestCase):

    def test_empty_trades(self):
        result = simulate_intraday([], DEFAULT_CONFIG)
        self.assertEqual(result["daily_returns"], [])
        self.assertEqual(result["bench_returns"], [])
        self.assertEqual(result["day_wise_log"], [])
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["win_count"], 0)

    def test_single_trade_pnl(self):
        entry = 100.0
        exit_ = 101.5
        order_value = 50000
        charges = nse_intraday_charges(order_value)
        expected_pnl = (exit_ - entry) / entry * order_value - charges

        trades = [make_trade(entry_price=entry, exit_price=exit_)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)

        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 1)
        self.assertEqual(len(result["daily_returns"]), 1)

        actual_ret = result["daily_returns"][0]
        expected_ret = expected_pnl / DEFAULT_CONFIG["initial_capital"]
        self.assertAlmostEqual(actual_ret, expected_ret, places=10)

    def test_losing_trade(self):
        trades = [make_trade(entry_price=100.0, exit_price=98.0)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertEqual(result["win_count"], 0)
        self.assertLess(result["daily_returns"][0], 0)

    def test_max_positions_cap(self):
        trades = [
            make_trade(symbol=f"S{i}.NS", signal_strength=i * 0.01,
                       entry_price=100.0, exit_price=101.0)
            for i in range(10)
        ]
        config = {**DEFAULT_CONFIG, "max_positions": 5}
        result = simulate_intraday(trades, config)
        self.assertEqual(result["trade_count"], 5)
        self.assertEqual(len(result["day_wise_log"]), 1)

    def test_multi_day(self):
        trades = [
            make_trade(trade_date="2024-01-15", entry_price=100, exit_price=101),
            make_trade(trade_date="2024-01-16", entry_price=200, exit_price=203),
            make_trade(trade_date="2024-01-17", entry_price=150, exit_price=148),
        ]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertEqual(len(result["daily_returns"]), 3)
        self.assertEqual(len(result["day_wise_log"]), 3)
        self.assertEqual(len(result["bench_returns"]), 3)

    def test_day_wise_log_format(self):
        trades = [make_trade()]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        log = result["day_wise_log"]

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertIn("log_date_epoch", entry)
        self.assertIn("invested_value", entry)
        self.assertIn("margin_available", entry)
        self.assertEqual(entry["invested_value"], 0)
        self.assertIsInstance(entry["margin_available"], float)

    def test_margin_accumulates(self):
        trades = [
            make_trade(trade_date="2024-01-15", entry_price=100, exit_price=102),
            make_trade(trade_date="2024-01-16", entry_price=100, exit_price=103),
        ]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        log = result["day_wise_log"]
        self.assertGreater(log[1]["margin_available"], log[0]["margin_available"])

    def test_zero_entry_price_skipped(self):
        trades = [make_trade(entry_price=0, exit_price=100)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 0)

    def test_none_entry_price_skipped(self):
        trades = [make_trade(entry_price=None, exit_price=100)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 0)

    def test_signal_strength_sorting(self):
        trades = [
            make_trade(symbol="LOW.NS", signal_strength=0.01,
                       entry_price=100, exit_price=110),
            make_trade(symbol="HIGH.NS", signal_strength=0.10,
                       entry_price=100, exit_price=90),
        ]
        config = {**DEFAULT_CONFIG, "max_positions": 1}
        result = simulate_intraday(trades, config)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 0)

    def test_bench_ret_passthrough(self):
        trades = [make_trade(bench_ret=0.0042)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertAlmostEqual(result["bench_returns"][0], 0.0042)

    # --- New tests ---

    def test_daily_ret_uses_current_margin(self):
        """Day 2 return uses updated margin (initial + day1_pnl), not initial."""
        ov = DEFAULT_CONFIG["order_value"]
        charges = nse_intraday_charges(ov)
        initial = DEFAULT_CONFIG["initial_capital"]

        # Day 1: 2% gain
        day1_pnl = 0.02 * ov - charges
        margin_after_day1 = initial + day1_pnl

        # Day 2: 1% gain
        day2_pnl = 0.01 * ov - charges

        trades = [
            make_trade(trade_date="2024-01-15", entry_price=100, exit_price=102),
            make_trade(trade_date="2024-01-16", entry_price=100, exit_price=101),
        ]
        result = simulate_intraday(trades, DEFAULT_CONFIG)

        # Day 2 return should be day2_pnl / margin_after_day1, NOT day2_pnl / initial
        expected_day2_ret = day2_pnl / margin_after_day1
        actual_day2_ret = result["daily_returns"][1]
        self.assertAlmostEqual(actual_day2_ret, expected_day2_ret, places=8)

        # Verify it's NOT using initial capital
        wrong_day2_ret = day2_pnl / initial
        self.assertNotAlmostEqual(actual_day2_ret, wrong_day2_ret, places=8)

    def test_trade_log_contents(self):
        """trade_log has symbol, prices, pnl, pnl_pct, charges, exit_type."""
        trades = [make_trade(entry_price=100, exit_price=103, exit_type="signal",
                             symbol="RELIANCE.NS", entry_bar=20)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)

        self.assertEqual(len(result["trade_log"]), 1)
        tl = result["trade_log"][0]

        self.assertEqual(tl["symbol"], "RELIANCE.NS")
        self.assertEqual(tl["entry_price"], 100.0)
        self.assertEqual(tl["exit_price"], 103.0)
        self.assertEqual(tl["exit_type"], "signal")
        self.assertEqual(tl["entry_bar"], 20)
        self.assertIn("pnl", tl)
        self.assertIn("pnl_pct", tl)
        self.assertIn("charges", tl)
        self.assertIn("signal_strength", tl)

        # pnl_pct should be ~3.0%
        self.assertAlmostEqual(tl["pnl_pct"], 3.0, places=2)

    def test_charges_deducted_from_pnl(self):
        """Zero price movement -> negative PnL (charges only)."""
        trades = [make_trade(entry_price=100, exit_price=100)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)

        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 0)
        # Return should be negative (charges)
        self.assertLess(result["daily_returns"][0], 0)
        # PnL = 0 - charges < 0
        self.assertLess(result["trade_log"][0]["pnl"], 0)

    def test_capital_depletion(self):
        """10 consecutive losing days -> margin decreases monotonically."""
        trades = [
            make_trade(trade_date=f"2024-01-{15+i:02d}",
                       entry_price=100, exit_price=95)  # 5% loss each day
            for i in range(10)
        ]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        log = result["day_wise_log"]

        self.assertEqual(len(log), 10)
        for i in range(1, len(log)):
            self.assertLess(log[i]["margin_available"], log[i-1]["margin_available"],
                            f"Margin should decrease: day {i}")

    def test_mixed_winners_losers_same_day(self):
        """2 trades same day: 1 win + 1 loss, verify counts."""
        trades = [
            make_trade(symbol="WIN.NS", entry_price=100, exit_price=110,
                       signal_strength=0.05),
            make_trade(symbol="LOSE.NS", entry_price=100, exit_price=90,
                       signal_strength=0.04),
        ]
        result = simulate_intraday(trades, DEFAULT_CONFIG)

        self.assertEqual(result["trade_count"], 2)
        self.assertEqual(result["win_count"], 1)
        self.assertEqual(len(result["daily_returns"]), 1)  # same day

    def test_none_signal_strength_handled(self):
        """signal_strength=None doesn't crash."""
        trades = [make_trade(signal_strength=None)]
        result = simulate_intraday(trades, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 1)

    def test_date_to_epoch(self):
        """_date_to_epoch('2024-01-15') -> correct Unix timestamp."""
        epoch = _date_to_epoch("2024-01-15")
        # 2024-01-15 00:00:00 UTC = 1705276800
        self.assertEqual(epoch, 1705276800)

    def test_exchange_aware_charges(self):
        """US exchange uses us_intraday_charges."""
        ov = 50000
        us_charges = us_intraday_charges(ov)
        nse_charges = nse_intraday_charges(ov)

        trades = [make_trade(entry_price=100, exit_price=101)]

        # NSE (default)
        result_nse = simulate_intraday(trades, DEFAULT_CONFIG)

        # NASDAQ
        us_config = {**DEFAULT_CONFIG, "exchange": "NASDAQ"}
        result_us = simulate_intraday(trades, us_config)

        # US should have higher returns (lower charges)
        self.assertGreater(result_us["daily_returns"][0], result_nse["daily_returns"][0])

        # Verify trade log charges differ
        self.assertAlmostEqual(result_nse["trade_log"][0]["charges"], nse_charges, places=2)
        self.assertAlmostEqual(result_us["trade_log"][0]["charges"], us_charges, places=2)


class TestGetChargesFn(unittest.TestCase):

    def test_dispatch_nse(self):
        self.assertIs(_get_charges_fn("NSE"), nse_intraday_charges)

    def test_dispatch_bse(self):
        self.assertIs(_get_charges_fn("BSE"), nse_intraday_charges)

    def test_dispatch_nasdaq(self):
        self.assertIs(_get_charges_fn("NASDAQ"), us_intraday_charges)

    def test_dispatch_nyse(self):
        self.assertIs(_get_charges_fn("NYSE"), us_intraday_charges)

    def test_dispatch_amex(self):
        self.assertIs(_get_charges_fn("AMEX"), us_intraday_charges)

    def test_dispatch_unknown_defaults_nse(self):
        self.assertIs(_get_charges_fn("UNKNOWN"), nse_intraday_charges)


if __name__ == "__main__":
    unittest.main()

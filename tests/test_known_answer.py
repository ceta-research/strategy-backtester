"""Known-answer integration test for the full intraday stack.

Hand-crafted scenario: 5 trades across 3 days, each held from its entry bar to the
session's last bar (no target or stop in reach), so it exits at that bar's close.
Verifies simulator + metrics + trade_log produce exact expected values.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_simulator_v2 import simulate_intraday_v2
from engine.charges import nse_intraday_charges
from lib.metrics import compute_metrics
from tests.test_intraday_simulator_v2 import signal_rows

# Target and stop out of reach, exits resolved up to the last bar: every trade exits at its last close.
HOLD_TO_CLOSE = {"target_pct": 1.0, "stop_pct": 0.9, "max_hold_bars": 60, "eod_buffer_bars": 0}


class TestKnownAnswer(unittest.TestCase):

    def test_five_trades_three_days(self):
        """5 synthetic trades, 3 days, hand-computed expected values."""
        initial_capital = 500_000
        order_value = 50_000
        charges = nse_intraday_charges(order_value)

        # Day 1: 2 trades (1 win, 1 loss)
        # Trade 1: entry=100, exit=103 -> pnl = 3/100 * 50000 - charges = 1500 - charges
        # Trade 2: entry=200, exit=196 -> pnl = -4/200 * 50000 - charges = -1000 - charges
        t1_pnl = (103 - 100) / 100 * order_value - charges  # ~1500 - 83.54
        t2_pnl = (196 - 200) / 200 * order_value - charges  # -1000 - 83.54
        day1_pnl = t1_pnl + t2_pnl
        margin_after_day1 = initial_capital + day1_pnl
        day1_ret = day1_pnl / initial_capital

        # Day 2: 1 trade (win)
        # Trade 3: entry=150, exit=156 -> pnl = 6/150 * 50000 - charges = 2000 - charges
        t3_pnl = (156 - 150) / 150 * order_value - charges
        day2_pnl = t3_pnl
        margin_after_day2 = margin_after_day1 + day2_pnl
        day2_ret = day2_pnl / margin_after_day1

        # Day 3: 2 trades (both win)
        # Trade 4: entry=50, exit=52 -> pnl = 2/50 * 50000 - charges = 2000 - charges
        # Trade 5: entry=300, exit=306 -> pnl = 6/300 * 50000 - charges = 1000 - charges
        t4_pnl = (52 - 50) / 50 * order_value - charges
        t5_pnl = (306 - 300) / 300 * order_value - charges
        day3_pnl = t4_pnl + t5_pnl
        margin_after_day3 = margin_after_day2 + day3_pnl
        day3_ret = day3_pnl / margin_after_day2

        # (symbol, trade_date, entry_bar, closes from entry to exit, signal_strength, bench_ret)
        rows = signal_rows(
            ("A.NS", "2024-01-15", 16, [100.0, 101.0, 103.0], 0.05, 0.001),
            ("B.NS", "2024-01-15", 18, [200.0, 198.0, 196.0], 0.03, 0.001),
            ("C.NS", "2024-01-16", 17, [150.0, 153.0, 156.0], 0.04, 0.002),
            ("D.NS", "2024-01-17", 16, [50.0, 51.0, 52.0], 0.06, 0.0015),
            ("E.NS", "2024-01-17", 20, [300.0, 303.0, 306.0], 0.02, 0.0015),
        )

        config = {
            "initial_capital": initial_capital,
            "max_positions": 5,
            "order_value": order_value,
            **HOLD_TO_CLOSE,
        }

        result = simulate_intraday_v2(rows, config)

        # Verify counts
        self.assertEqual(result["trade_count"], 5)
        self.assertEqual(result["win_count"], 4)  # t1, t3, t4, t5 win; t2 loses

        # Verify daily returns
        self.assertEqual(len(result["daily_returns"]), 3)
        self.assertAlmostEqual(result["daily_returns"][0], day1_ret, places=8)
        self.assertAlmostEqual(result["daily_returns"][1], day2_ret, places=8)
        self.assertAlmostEqual(result["daily_returns"][2], day3_ret, places=8)

        # Verify bench returns
        self.assertAlmostEqual(result["bench_returns"][0], 0.001)
        self.assertAlmostEqual(result["bench_returns"][1], 0.002)
        self.assertAlmostEqual(result["bench_returns"][2], 0.0015)

        # Verify final margin
        log = result["day_wise_log"]
        self.assertEqual(len(log), 3)
        self.assertAlmostEqual(log[2]["margin_available"], margin_after_day3, places=2)

        # Verify trade log
        tl = result["trade_log"]
        self.assertEqual(len(tl), 5)

        # Trade 1 details
        self.assertEqual(tl[0]["symbol"], "A.NS")
        self.assertAlmostEqual(tl[0]["pnl"], t1_pnl, places=2)
        self.assertAlmostEqual(tl[0]["pnl_pct"], 3.0, places=2)

        # Trade 2 (loser)
        self.assertEqual(tl[1]["symbol"], "B.NS")
        self.assertLess(tl[1]["pnl"], 0)
        self.assertAlmostEqual(tl[1]["pnl_pct"], -2.0, places=2)

        # Now verify metrics work on the result
        metrics = compute_metrics(
            result["daily_returns"],
            result["bench_returns"],
            periods_per_year=252,
            risk_free_rate=0.065,
        )
        port = metrics["portfolio"]
        self.assertIsNotNone(port["cagr"])
        self.assertIsNotNone(port["max_drawdown"])
        self.assertIsNotNone(port["sharpe_ratio"])

        # Portfolio should be net positive
        self.assertGreater(port["total_return"], 0)

    def test_known_answer_us_exchange(self):
        """Same structure but with US exchange - charges should be much lower."""
        initial_capital = 500_000
        order_value = 50_000

        from engine.charges import us_intraday_charges
        charges = us_intraday_charges(order_value)

        rows = signal_rows(
            ("AAPL", "2024-01-15", 16, [180.0, 181.0, 183.0], 0.05, 0.001),
            ("MSFT", "2024-01-16", 17, [400.0, 402.0, 404.0], 0.04, 0.002),
        )

        config = {
            "initial_capital": initial_capital,
            "max_positions": 5,
            "order_value": order_value,
            "exchange": "NASDAQ",
            **HOLD_TO_CLOSE,
        }

        result = simulate_intraday_v2(rows, config)

        # All trades should be winners (prices go up, charges are tiny)
        self.assertEqual(result["win_count"], 2)
        self.assertEqual(result["trade_count"], 2)

        # Verify charges in trade log are US-level (< $5)
        for tl in result["trade_log"]:
            self.assertLess(tl["charges"], 5.0)
            self.assertAlmostEqual(tl["charges"], charges, places=2)


if __name__ == "__main__":
    unittest.main()

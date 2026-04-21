"""Direct tests for lib/backtest_result.py methods.

Covers Phase 7 audit items P7.1/3/4 — hand-computed assertions over
per-period returns, trade metrics, portfolio metrics, and monthly /
yearly bucketing. These methods were read & resolved during the
initial audit, but lacked direct regression coverage.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.backtest_result import BacktestResult
from lib.equity_curve import EquityCurve, Frequency

DAY = 86400


def _fresh(strategy_name="test"):
    return BacktestResult(
        strategy_name=strategy_name,
        params={},
        instrument="NSE:TEST",
        exchange="NSE",
        capital=1_000_000,
        equity_curve_frequency=Frequency.DAILY_CALENDAR,
    )


def _mk_trade(entry_epoch, exit_epoch, entry_price, exit_price, qty=100,
              charges=0.0, slippage=0.0):
    """Build a trade dict in the shape BacktestResult._trade_metrics expects."""
    side = "LONG"
    gross = (exit_price - entry_price) * qty
    net = gross - charges - slippage
    pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0.0
    return {
        "entry_epoch": int(entry_epoch),
        "exit_epoch": int(exit_epoch),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": qty,
        "side": side,
        "gross_pnl": gross,
        "net_pnl": net,
        "pnl_pct": pnl_pct,
        "charges": charges,
        "slippage": slippage,
        "hold_days": (exit_epoch - entry_epoch) / DAY,
    }


# ── P7.1: per-period returns ────────────────────────────────────────────

class TestPeriodReturns(unittest.TestCase):
    """Hand-verify EquityCurve.period_returns — the single source of
    per-period returns consumed by metrics and time_extremes."""

    def test_basic_returns(self):
        # [100, 110, 121, 100] → 10%, 10%, -17.3554%
        epochs = [1577836800 + i * DAY for i in range(4)]
        curve = EquityCurve.from_pairs(
            [(e, v) for e, v in zip(epochs, [100.0, 110.0, 121.0, 100.0])],
            Frequency.DAILY_CALENDAR,
        )
        rets = curve.period_returns()
        self.assertEqual(len(rets), 3)
        self.assertAlmostEqual(rets[0], 0.10, places=10)
        self.assertAlmostEqual(rets[1], 0.10, places=10)
        self.assertAlmostEqual(rets[2], 100.0 / 121.0 - 1, places=10)

    def test_duplicate_values_give_zero(self):
        # Weekend forward-fill → consecutive identical values → 0 return.
        epochs = [1577836800 + i * DAY for i in range(3)]
        curve = EquityCurve.from_pairs(
            [(e, 100.0) for e in epochs], Frequency.DAILY_CALENDAR,
        )
        rets = curve.period_returns()
        self.assertEqual(rets, [0.0, 0.0])

    def test_zero_prev_value_gives_zero(self):
        # Defensive: prev <= 0 → return 0 instead of ZeroDivisionError.
        epochs = [1577836800 + i * DAY for i in range(3)]
        # Reconstruct values via the underlying dataclass (skip validation
        # by using a curve that reaches 0 via monotonic decline).
        curve = EquityCurve.from_pairs(
            [(epochs[0], 100.0), (epochs[1], 0.0), (epochs[2], 50.0)],
            Frequency.DAILY_CALENDAR,
        )
        rets = curve.period_returns()
        # v1/v0 = 0/100 - 1 = -1; v2/v1 with v1=0 → 0 by guard.
        self.assertAlmostEqual(rets[0], -1.0, places=10)
        self.assertEqual(rets[1], 0.0)


# ── P7.2: trade metrics ─────────────────────────────────────────────────

class TestTradeMetrics(unittest.TestCase):
    """Hand-compute win_rate, profit_factor, payoff, Kelly on a known
    trade set."""

    def test_three_wins_two_losses(self):
        # Wins: +1000, +500, +300 (net pnl after charges).
        # Losses: -400, -200.
        # Expected:
        #   win_rate = 3/5 = 0.60
        #   avg_win_pct = (+10, +5, +3)/3 = 6.0
        #   avg_loss_pct = (-4, -2)/2 = -3.0
        #   profit_factor = 1800 / 600 = 3.0
        #   payoff = |6.0 / -3.0| = 2.0
        #   expectancy = (1000+500+300-400-200) / 5 = 240
        #   Kelly = wr - (1-wr)/payoff = 0.6 - 0.4/2.0 = 0.4
        r = _fresh()
        base = 1577836800
        # qty=100 means net_pnl = (exit-entry)*100 - charges. Rig prices.
        r.trades = [
            _mk_trade(base + 0 * DAY, base + 2 * DAY, 100.0, 110.0),  # +10%, +1000
            _mk_trade(base + 3 * DAY, base + 5 * DAY, 100.0, 105.0),  # +5%,  +500
            _mk_trade(base + 6 * DAY, base + 8 * DAY, 100.0, 103.0),  # +3%,  +300
            _mk_trade(base + 9 * DAY, base + 11 * DAY, 100.0, 96.0),  # -4%,  -400
            _mk_trade(base + 12 * DAY, base + 14 * DAY, 100.0, 98.0), # -2%,  -200
        ]
        tm = r._trade_metrics()
        self.assertEqual(tm["total_trades"], 5)
        self.assertEqual(tm["winning_trades"], 3)
        self.assertEqual(tm["losing_trades"], 2)
        self.assertAlmostEqual(tm["win_rate"], 0.60, places=4)
        self.assertAlmostEqual(tm["avg_win_pct"], 6.0, places=4)
        self.assertAlmostEqual(tm["avg_loss_pct"], -3.0, places=4)
        self.assertAlmostEqual(tm["profit_factor"], 3.0, places=4)
        self.assertAlmostEqual(tm["payoff_ratio"], 2.0, places=4)
        self.assertAlmostEqual(tm["expectancy"], 240.0, places=2)
        self.assertAlmostEqual(tm["kelly_criterion"], 0.4, places=4)
        # Avg hold = (2,2,2,2,2) = 2 days
        self.assertAlmostEqual(tm["avg_hold_days"], 2.0, places=4)

    def test_all_wins_profit_factor_is_none(self):
        """All-wins sets profit_factor / payoff / Kelly to None (no
        losses to divide by), not infinity."""
        r = _fresh()
        base = 1577836800
        r.trades = [
            _mk_trade(base + 0 * DAY, base + 2 * DAY, 100.0, 110.0),
            _mk_trade(base + 3 * DAY, base + 5 * DAY, 100.0, 105.0),
        ]
        tm = r._trade_metrics()
        self.assertEqual(tm["winning_trades"], 2)
        self.assertEqual(tm["losing_trades"], 0)
        self.assertIsNone(tm["profit_factor"])
        self.assertIsNone(tm["payoff_ratio"])
        self.assertIsNone(tm["kelly_criterion"])

    def test_empty_trades_returns_none_metrics(self):
        r = _fresh()
        tm = r._trade_metrics()
        self.assertEqual(tm["total_trades"], 0)
        self.assertIsNone(tm["win_rate"])
        self.assertIsNone(tm["profit_factor"])

    def test_consecutive_streaks(self):
        # Trade sequence (by entry_epoch): W W L W L L L W
        # max_cw = 2, max_cl = 3
        r = _fresh()
        base = 1577836800
        closes = [(110, "W"), (105, "W"), (95, "L"), (108, "W"),
                  (95, "L"), (90, "L"), (85, "L"), (110, "W")]
        for i, (ep, _) in enumerate(closes):
            entry = base + i * 5 * DAY
            r.trades.append(_mk_trade(entry, entry + 2 * DAY, 100.0, ep))
        tm = r._trade_metrics()
        self.assertEqual(tm["max_consecutive_wins"], 2)
        self.assertEqual(tm["max_consecutive_losses_trades"], 3)


# ── P7.3: portfolio metrics ─────────────────────────────────────────────

class TestPortfolioMetrics(unittest.TestCase):
    """final_value, peak_value, time_in_market (interval union)."""

    def test_final_and_peak_values(self):
        r = _fresh()
        base = 1577836800
        for i, v in enumerate([1_000_000, 1_200_000, 800_000, 900_000]):
            r.add_equity_point(base + i * DAY, v)
        pm = r._portfolio_metrics()
        self.assertEqual(pm["final_value"], 900_000.0)
        self.assertEqual(pm["peak_value"], 1_200_000.0)

    def test_time_in_market_overlap_counted_once(self):
        # Two overlapping positions over a 10-day sim span.
        #   trade1: days 0-5 (5 days)
        #   trade2: days 3-8 (5 days)
        # Union: days 0-8 (8 days). Total span: 10 days.
        # time_in_market = 8/10 = 0.8 (NOT 10/10 from naive sum).
        r = _fresh()
        base = 1577836800
        for d in range(11):
            r.add_equity_point(base + d * DAY, 1_000_000 + d * 1000)
        r.trades = [
            _mk_trade(base + 0 * DAY, base + 5 * DAY, 100, 105),
            _mk_trade(base + 3 * DAY, base + 8 * DAY, 100, 110),
        ]
        pm = r._portfolio_metrics()
        self.assertAlmostEqual(pm["time_in_market"], 0.8, places=4)

    def test_time_in_market_disjoint_intervals(self):
        # Two disjoint trades; union = sum.
        r = _fresh()
        base = 1577836800
        for d in range(11):
            r.add_equity_point(base + d * DAY, 1_000_000 + d * 1000)
        r.trades = [
            _mk_trade(base + 0 * DAY, base + 2 * DAY, 100, 105),  # 2 days
            _mk_trade(base + 6 * DAY, base + 9 * DAY, 100, 110),  # 3 days
        ]
        pm = r._portfolio_metrics()
        # 5 days in market / 10 days total = 0.5
        self.assertAlmostEqual(pm["time_in_market"], 0.5, places=4)


# ── P7.4: monthly / yearly bucketing ────────────────────────────────────

class TestMonthlyBucketing(unittest.TestCase):
    """Monthly returns chain across months: bucket[i] base =
    prior bucket's last value."""

    def test_chained_monthly_returns(self):
        r = _fresh()
        # 2020-01-01 epoch = 1577836800 (Jan 1).
        # Jan: start 1000000, end 1100000 (+10%)
        # Feb: start 1100000, end 1155000 (+5%)  (chains from Jan's last)
        # Mar: start 1155000, end 1039500 (-10%)
        jan = 1577836800
        feb = 1580515200  # 2020-02-01
        mar = 1582934400  # 2020-03-01
        r.add_equity_point(jan, 1_000_000)
        r.add_equity_point(jan + 15 * DAY, 1_050_000)
        r.add_equity_point(jan + 29 * DAY, 1_100_000)  # end of Jan
        r.add_equity_point(feb + 10 * DAY, 1_120_000)
        r.add_equity_point(feb + 27 * DAY, 1_155_000)  # end of Feb
        r.add_equity_point(mar + 10 * DAY, 1_100_000)
        r.add_equity_point(mar + 28 * DAY, 1_039_500)  # end of Mar

        monthly = r._monthly_returns()
        self.assertAlmostEqual(monthly["2020"]["1"], 0.10, places=4)
        self.assertAlmostEqual(monthly["2020"]["2"], 0.05, places=4)
        self.assertAlmostEqual(monthly["2020"]["3"], -0.10, places=4)


class TestYearlyBucketing(unittest.TestCase):
    """Yearly returns + running-peak MDD (Phase 1.3 fix pinned here)."""

    def test_multi_year_return_and_running_peak(self):
        r = _fresh()
        # 2021: starts 1M, peaks 1.5M, ends 1.2M. MDD from 1.5 → 1.2 = -20%.
        # 2022: starts 1.2M (chains), goes to 1.0M. Running peak STILL 1.5M
        #       (carried across year boundary, Phase 1.3 fix).
        #       2022 MDD from 1.5M peak → 1.0M = -33.3%.
        y2021 = 1609459200
        y2022 = 1640995200
        r.add_equity_point(y2021, 1_000_000)
        r.add_equity_point(y2021 + 100 * DAY, 1_500_000)
        r.add_equity_point(y2021 + 250 * DAY, 1_200_000)
        r.add_equity_point(y2022, 1_200_000)
        r.add_equity_point(y2022 + 100 * DAY, 1_000_000)

        yr = r._yearly_returns()
        years = {y["year"]: y for y in yr}
        # 2021 return = 1.2/1.0 - 1 = +20%
        self.assertAlmostEqual(years[2021]["return"], 0.20, places=4)
        # 2021 MDD: 1.5 → 1.2 = -20%
        self.assertAlmostEqual(years[2021]["mdd"], -0.20, places=4)
        # 2022 return = 1.0/1.2 - 1 ≈ -16.67%  (chains from 2021's last)
        self.assertAlmostEqual(years[2022]["return"], -1 / 6, places=4)
        # 2022 MDD: carries 2021's 1.5 peak → 1.0 final = -33.3%.
        # This is THE Phase 1.3 regression lock.
        self.assertAlmostEqual(years[2022]["mdd"], -1 / 3, places=4)


if __name__ == "__main__":
    unittest.main()

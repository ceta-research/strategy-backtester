"""Tests for engine/intraday_simulator_v2.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_simulator_v2 import (
    simulate_intraday_v2, _build_entry_signals, _resolve_exit,
    _compute_order_value, _rank_entries, _compute_symbol_scores,
    _compute_payout, run_parallel_sweep,
)
from engine.intraday_simulator import simulate_intraday
from engine.charges import nse_intraday_charges


DEFAULT_CONFIG = {
    "initial_capital": 500000,
    "max_positions": 5,
    "order_value": 50000,
    "target_pct": 0.015,
    "stop_pct": 0.01,
    "max_hold_bars": 60,
}

EXIT_CONFIG = {
    "target_pct": 0.015,
    "stop_pct": 0.01,
    "max_hold_bars": 60,
}


def make_bars(entry_bar, closes, start_open=None):
    """Build a bar list from a list of close prices starting at entry_bar."""
    bars = []
    for i, close in enumerate(closes):
        bar_num = entry_bar + i
        open_price = start_open if (i == 0 and start_open) else close
        bars.append({
            "bar_num": bar_num,
            "open": open_price,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        })
    return bars


def make_signal_row(symbol, trade_date, entry_bar, entry_price,
                    bar_num, bar_close, or_high=None, or_low=None,
                    signal_strength=0.05, bench_ret=0.001,
                    bar_open=None, bar_high=None, bar_low=None):
    """Build a single signal matrix row."""
    or_high = or_high or entry_price + 5
    or_low = or_low or entry_price - 10
    or_range = or_high - or_low
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "entry_bar": entry_bar,
        "entry_price": entry_price,
        "or_high": or_high,
        "or_low": or_low,
        "or_range": or_range,
        "signal_strength": signal_strength,
        "bar_num": bar_num,
        "bar_open": bar_open or bar_close,
        "bar_high": bar_high or bar_close + 1,
        "bar_low": bar_low or bar_close - 1,
        "bar_close": bar_close,
        "bench_ret": bench_ret,
    }


def make_signal_matrix_from_bars(symbol, trade_date, entry_bar, entry_price,
                                  bars, or_high=None, or_low=None,
                                  signal_strength=0.05, bench_ret=0.001):
    """Build signal matrix rows from a list of bar dicts."""
    rows = []
    for bar in bars:
        rows.append(make_signal_row(
            symbol=symbol, trade_date=trade_date,
            entry_bar=entry_bar, entry_price=entry_price,
            bar_num=bar["bar_num"], bar_close=bar["close"],
            bar_open=bar.get("open", bar["close"]),
            bar_high=bar.get("high", bar["close"] + 1),
            bar_low=bar.get("low", bar["close"] - 1),
            or_high=or_high, or_low=or_low,
            signal_strength=signal_strength, bench_ret=bench_ret,
        ))
    return rows


# ============================================================
# A. Exit logic unit tests (_resolve_exit)
# ============================================================

class TestResolveExit(unittest.TestCase):

    def test_target_hit(self):
        """Target hit when close >= entry * (1 + target_pct)."""
        entry_price = 100.0
        target = entry_price * 1.015  # 101.5
        bars = make_bars(16, [100, 100.5, 101, 101.5, 102])
        result = _resolve_exit(bars, entry_price, 90.0, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "signal")
        self.assertGreaterEqual(result["exit_price"], target)
        self.assertEqual(result["exit_bar"], 19)  # bar with close=101.5

    def test_stop_hit(self):
        """Stop hit when close <= stop_price = min(entry*(1-stop), or_low)."""
        entry_price = 100.0
        # stop = min(100*0.99, 95) = 95. Close of 94 triggers stop.
        bars = make_bars(16, [100, 98, 96, 94])
        result = _resolve_exit(bars, entry_price, 95.0, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "signal")
        self.assertLessEqual(result["exit_price"], 95.0)

    def test_or_low_floor(self):
        """stop_price = min(entry*(1-stop), or_low). or_low > computed stop."""
        # entry=105, stop=1% -> 103.95. or_low=106 -> stop=min(103.95, 106)=103.95
        entry_price = 105.0
        or_low = 106.0  # or_low is ABOVE the computed stop
        bars = make_bars(16, [105, 104, 103.5])  # 103.5 <= 103.95
        result = _resolve_exit(bars, entry_price, or_low, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_price"], 103.5)

    def test_or_low_below_stop(self):
        """or_low < computed stop -> stop_price = or_low (wider stop, more loss allowed)."""
        # entry=100, stop=1% -> 99. or_low=95 -> stop=min(99, 95)=95
        # 98 and 96 don't trigger (above 95), only 95 does
        entry_price = 100.0
        or_low = 95.0
        bars = make_bars(16, [100, 98, 96, 95])
        result = _resolve_exit(bars, entry_price, or_low, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_price"], 95)

    def test_max_hold_timeout(self):
        """No target/stop within max_hold bars -> exit at LAST bar (EOD)."""
        entry_price = 100.0
        config = {**EXIT_CONFIG, "max_hold_bars": 3}
        # 5 bars total: entry + 4 post-entry. max_hold=3 means check bars 17,18,19 only.
        # No target/stop within those bars, so EOD at last bar (bar 20).
        bars = make_bars(16, [100, 100.2, 100.1, 100.3, 100.2])
        result = _resolve_exit(bars, entry_price, 90.0, config)
        self.assertEqual(result["exit_type"], "eod")
        self.assertEqual(result["exit_bar"], 20)
        self.assertEqual(result["exit_price"], 100.2)

    def test_eod_fallback(self):
        """No target/stop at all -> exit at last bar's close."""
        entry_price = 100.0
        config = {**EXIT_CONFIG, "max_hold_bars": 200}
        bars = make_bars(16, [100, 100.2, 100.1, 100.3, 100.2])
        result = _resolve_exit(bars, entry_price, 90.0, config)
        self.assertEqual(result["exit_type"], "eod")
        self.assertEqual(result["exit_price"], 100.2)

    def test_entry_bar_skipped(self):
        """Target met on entry bar itself is NOT triggered (v1: bar_num > entry_bar)."""
        entry_price = 100.0
        # Entry bar close = 102 (above target 101.5), but should be skipped
        bars = make_bars(16, [102, 100.5, 100.3])
        result = _resolve_exit(bars, entry_price, 90.0, EXIT_CONFIG)
        # Should NOT trigger on entry bar, should be EOD
        self.assertEqual(result["exit_type"], "eod")

    def test_single_bar_only(self):
        """Only entry bar, no subsequent bars -> exit at entry bar close."""
        bars = make_bars(16, [100])
        result = _resolve_exit(bars, 100.0, 90.0, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "eod")
        self.assertEqual(result["exit_price"], 100)
        self.assertEqual(result["exit_bar"], 16)


# ============================================================
# B. Full simulator tests
# ============================================================

class TestSimulateIntradayV2(unittest.TestCase):

    def test_empty_signal_matrix(self):
        result = simulate_intraday_v2([], DEFAULT_CONFIG)
        self.assertEqual(result["daily_returns"], [])
        self.assertEqual(result["bench_returns"], [])
        self.assertEqual(result["day_wise_log"], [])
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["win_count"], 0)
        self.assertEqual(result["trade_log"], [])

    def test_single_entry_target_hit(self):
        """One entry signal with bars showing target hit -> correct PnL."""
        entry_price = 100.0
        # Target = 101.5. Bar sequence: entry at 100, then 100.5, 101, 101.5 (target)
        bars = make_bars(16, [100, 100.5, 101, 101.5, 102])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, entry_price, bars, or_low=90.0
        )

        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 1)

        tl = result["trade_log"][0]
        self.assertEqual(tl["exit_type"], "signal")
        self.assertEqual(tl["exit_price"], 101.5)

        # Verify PnL
        ov = DEFAULT_CONFIG["order_value"]
        charges = nse_intraday_charges(ov)
        expected_pnl = (101.5 - 100.0) / 100.0 * ov - charges
        self.assertAlmostEqual(tl["pnl"], round(expected_pnl, 2), places=2)

    def test_losing_trade(self):
        """Entry signal where stop hits -> negative PnL."""
        entry_price = 100.0
        # Stop = min(99, or_low=90) = 90. Use close=89 to trigger stop.
        bars = make_bars(16, [100, 99, 89])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, entry_price, bars, or_low=90.0
        )
        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 0)
        self.assertLess(result["trade_log"][0]["pnl"], 0)

    def test_max_positions_cap(self):
        """10 entries same day, max=5 -> only 5 trades."""
        matrix = []
        for i in range(10):
            bars = make_bars(16, [100, 100.5, 101, 101.5])
            matrix.extend(make_signal_matrix_from_bars(
                f"S{i}.NS", "2024-01-15", 16, 100.0, bars,
                or_low=90.0, signal_strength=i * 0.01
            ))

        config = {**DEFAULT_CONFIG, "max_positions": 5}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 5)

    def test_multi_day_margin(self):
        """Day 2 return uses updated margin."""
        ov = DEFAULT_CONFIG["order_value"]
        charges = nse_intraday_charges(ov)
        initial = DEFAULT_CONFIG["initial_capital"]

        # Day 1: target hit at 101.5 (1.5% gain)
        bars1 = make_bars(16, [100, 100.5, 101, 101.5])
        day1_pnl = (101.5 - 100) / 100 * ov - charges
        margin_after_day1 = initial + day1_pnl

        # Day 2: target hit at 101.5 (same)
        bars2 = make_bars(16, [100, 100.5, 101, 101.5])
        day2_pnl = (101.5 - 100) / 100 * ov - charges

        matrix = []
        matrix.extend(make_signal_matrix_from_bars(
            "A.NS", "2024-01-15", 16, 100.0, bars1, or_low=90.0
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "B.NS", "2024-01-16", 16, 100.0, bars2, or_low=90.0
        ))

        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        expected_day2_ret = day2_pnl / margin_after_day1
        self.assertAlmostEqual(result["daily_returns"][1], expected_day2_ret, places=8)

    def test_charges_deducted(self):
        """Zero price movement -> negative PnL (charges only)."""
        bars = make_bars(16, [100, 100, 100, 100])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["win_count"], 0)
        self.assertLess(result["trade_log"][0]["pnl"], 0)

    def test_signal_strength_sorting(self):
        """Highest signal_strength selected first."""
        # Two entries same day, max_positions=1. High SS entry loses, low SS wins.
        bars_high = make_bars(16, [100, 89])  # will stop out (or_low=90 -> stop=90)
        bars_low = make_bars(16, [100, 110])  # big win

        matrix = []
        matrix.extend(make_signal_matrix_from_bars(
            "HIGH.NS", "2024-01-15", 16, 100.0, bars_high,
            or_low=90.0, signal_strength=0.10
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "LOW.NS", "2024-01-15", 16, 100.0, bars_low,
            or_low=90.0, signal_strength=0.01
        ))

        config = {**DEFAULT_CONFIG, "max_positions": 1}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 1)
        # HIGH.NS selected (higher signal_strength), which loses
        self.assertEqual(result["win_count"], 0)
        self.assertEqual(result["trade_log"][0]["symbol"], "HIGH.NS")

    def test_trade_log_contents(self):
        """All expected fields present with correct values."""
        bars = make_bars(20, [100, 100.5, 101, 101.5, 102])
        matrix = make_signal_matrix_from_bars(
            "RELIANCE.NS", "2024-01-15", 20, 100.0, bars, or_low=90.0
        )
        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        tl = result["trade_log"][0]

        self.assertEqual(tl["symbol"], "RELIANCE.NS")
        self.assertEqual(tl["entry_price"], 100.0)
        self.assertEqual(tl["exit_price"], 101.5)
        self.assertEqual(tl["exit_type"], "signal")
        self.assertEqual(tl["entry_bar"], 20)
        self.assertIn("pnl", tl)
        self.assertIn("pnl_pct", tl)
        self.assertIn("charges", tl)
        self.assertIn("signal_strength", tl)
        self.assertAlmostEqual(tl["pnl_pct"], 1.5, places=2)


# ============================================================
# C. Build entry signals
# ============================================================

class TestBuildEntrySignals(unittest.TestCase):

    def test_groups_by_date_and_symbol(self):
        matrix = [
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 16, 100.0),
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 17, 101.0),
            make_signal_row("B.NS", "2024-01-15", 18, 200.0, 18, 200.0),
            make_signal_row("A.NS", "2024-01-16", 16, 100.0, 16, 100.0),
        ]
        result = _build_entry_signals(matrix)

        self.assertIn("2024-01-15", result)
        self.assertIn("2024-01-16", result)
        self.assertEqual(len(result["2024-01-15"]), 2)  # A.NS + B.NS
        self.assertEqual(len(result["2024-01-16"]), 1)  # A.NS only

    def test_bars_sorted_by_bar_num(self):
        matrix = [
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 18, 102.0),
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 16, 100.0),
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 17, 101.0),
        ]
        result = _build_entry_signals(matrix)
        bars = result["2024-01-15"][0]["bars"]
        self.assertEqual([b["bar_num"] for b in bars], [16, 17, 18])

    def test_entry_metadata_from_first_row(self):
        matrix = [
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 16, 100.0,
                            signal_strength=0.08, bench_ret=0.003),
            make_signal_row("A.NS", "2024-01-15", 16, 100.0, 17, 101.0),
        ]
        result = _build_entry_signals(matrix)
        entry = result["2024-01-15"][0]
        self.assertEqual(entry["entry_bar"], 16)
        self.assertEqual(entry["entry_price"], 100.0)
        self.assertAlmostEqual(entry["signal_strength"], 0.08)
        self.assertAlmostEqual(entry["bench_ret"], 0.003)


# ============================================================
# D. v1/v2 equivalence test
# ============================================================

class TestV1V2Equivalence(unittest.TestCase):

    def test_v1_v2_equivalence(self):
        """Same synthetic data through v1 and v2 must produce identical results."""
        ov = 50000
        charges = nse_intraday_charges(ov)

        # Define 5 entries across 3 days with known exit outcomes
        entries = [
            # Day 1: Entry A - target hit at bar 20 (close=101.5)
            {
                "symbol": "A.NS", "trade_date": "2024-01-15",
                "entry_bar": 16, "entry_price": 100.0,
                "or_low": 90.0, "signal_strength": 0.05, "bench_ret": 0.001,
                "bars": make_bars(16, [100, 100.3, 100.5, 100.8, 101.5, 102]),
                # v1: exit at bar 20, price=101.5, type=signal
            },
            # Day 1: Entry B - stop hit at bar 18 (close=89)
            {
                "symbol": "B.NS", "trade_date": "2024-01-15",
                "entry_bar": 16, "entry_price": 100.0,
                "or_low": 90.0, "signal_strength": 0.04, "bench_ret": 0.001,
                "bars": make_bars(16, [100, 95, 89]),
                # v1: exit at bar 18, price=89, type=signal (89 <= min(99, 90)=90)
            },
            # Day 2: Entry C - EOD exit (no target/stop)
            {
                "symbol": "C.NS", "trade_date": "2024-01-16",
                "entry_bar": 16, "entry_price": 100.0,
                "or_low": 90.0, "signal_strength": 0.03, "bench_ret": 0.002,
                "bars": make_bars(16, [100, 100.2, 100.1, 100.3, 100.2]),
                # v1: exit at bar 20, price=100.2, type=eod
            },
            # Day 3: Entry D - target hit
            {
                "symbol": "D.NS", "trade_date": "2024-01-17",
                "entry_bar": 16, "entry_price": 100.0,
                "or_low": 90.0, "signal_strength": 0.06, "bench_ret": 0.0005,
                "bars": make_bars(16, [100, 100.5, 101.5, 102]),
                # v1: exit at bar 18, price=101.5, type=signal
            },
            # Day 3: Entry E - target hit
            {
                "symbol": "E.NS", "trade_date": "2024-01-17",
                "entry_bar": 18, "entry_price": 200.0,
                "or_low": 180.0, "signal_strength": 0.07, "bench_ret": 0.0005,
                "bars": make_bars(18, [200, 201, 203, 204]),
                # v1: exit at bar 20, price=203, type=signal
            },
        ]

        # Build v2 signal matrix
        signal_matrix = []
        for e in entries:
            signal_matrix.extend(make_signal_matrix_from_bars(
                e["symbol"], e["trade_date"], e["entry_bar"], e["entry_price"],
                e["bars"], or_low=e["or_low"],
                signal_strength=e["signal_strength"], bench_ret=e["bench_ret"],
            ))

        # Run v2
        v2_result = simulate_intraday_v2(signal_matrix, DEFAULT_CONFIG)

        # Build v1 trades: pre-compute exits to match what v2 should produce
        v1_trades = []
        for e in entries:
            exit_result = _resolve_exit(
                e["bars"], e["entry_price"], e["or_low"],
                {"target_pct": 0.015, "stop_pct": 0.01, "max_hold_bars": 60}
            )
            v1_trades.append({
                "symbol": e["symbol"],
                "trade_date": e["trade_date"],
                "entry_bar": e["entry_bar"],
                "entry_price": e["entry_price"],
                "exit_price": exit_result["exit_price"],
                "exit_type": exit_result["exit_type"],
                "signal_strength": e["signal_strength"],
                "bench_ret": e["bench_ret"],
            })

        v1_config = {
            "initial_capital": DEFAULT_CONFIG["initial_capital"],
            "max_positions": DEFAULT_CONFIG["max_positions"],
            "order_value": DEFAULT_CONFIG["order_value"],
        }
        v1_result = simulate_intraday(v1_trades, v1_config)

        # Compare
        self.assertEqual(v2_result["trade_count"], v1_result["trade_count"])
        self.assertEqual(v2_result["win_count"], v1_result["win_count"])

        for i, (v2_ret, v1_ret) in enumerate(
            zip(v2_result["daily_returns"], v1_result["daily_returns"])
        ):
            self.assertAlmostEqual(v2_ret, v1_ret, places=10,
                                   msg=f"Daily return mismatch on day {i}")

        for i, (v2_tl, v1_tl) in enumerate(
            zip(v2_result["trade_log"], v1_result["trade_log"])
        ):
            self.assertEqual(v2_tl["symbol"], v1_tl["symbol"],
                             msg=f"Trade {i} symbol mismatch")
            self.assertAlmostEqual(v2_tl["pnl"], v1_tl["pnl"], places=2,
                                   msg=f"Trade {i} PnL mismatch")
            self.assertEqual(v2_tl["exit_type"], v1_tl["exit_type"],
                             msg=f"Trade {i} exit_type mismatch")


# ============================================================
# E. Phase 4B: Trailing stop tests
# ============================================================

class TestTrailingStop(unittest.TestCase):

    def test_trailing_stop_locks_profit(self):
        """Trailing stop moves up as price rises, then triggers on pullback."""
        entry_price = 100.0
        # High target (110) so target doesn't trigger before trailing stop
        config = {"target_pct": 0.10, "stop_pct": 0.01, "max_hold_bars": 60,
                  "trailing_stop_pct": 0.02}
        # Highest: 100, 103, 105, 105, 105
        # Trail:    98, 100.94, 102.9, 102.9, 102.9
        # Bar 20 (102): 102 <= 102.9 -> TRIGGERED
        bars = make_bars(16, [100, 103, 105, 104, 102])
        result = _resolve_exit(bars, entry_price, 90.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 20)
        self.assertEqual(result["exit_price"], 102)

    def test_trailing_stop_ratchets_up(self):
        """Trailing stop follows highest price, never moves down."""
        entry_price = 100.0
        config = {"target_pct": 0.10, "stop_pct": 0.01, "max_hold_bars": 60,
                  "trailing_stop_pct": 0.03}
        # Closes: 100(entry), 103, 101, 106, 104, 102
        # Highest: 100, 103, 103, 106, 106, 106
        # Trail:   97, 99.91, 99.91, 102.82, 102.82, 102.82
        # Bar 21 (102): 102 <= 102.82 -> TRIGGERED
        bars = make_bars(16, [100, 103, 101, 106, 104, 102])
        result = _resolve_exit(bars, entry_price, 85.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 21)
        self.assertEqual(result["exit_price"], 102)

    def test_trailing_stop_never_below_fixed(self):
        """Trailing stop can't widen below fixed stop; fixed stop takes precedence."""
        entry_price = 100.0
        or_low = 95.0  # fixed_stop = min(99, 95) = 95
        config = {**EXIT_CONFIG, "trailing_stop_pct": 0.20}  # trail = 80
        # Trail (80) is far below fixed stop (95), so fixed governs.
        # Bar 18 (94): 94 <= 95 -> TRIGGERED by fixed stop
        # Without max(fixed, trail), trail=80, and 94 > 80 wouldn't trigger.
        bars = make_bars(16, [100, 99, 94])
        result = _resolve_exit(bars, entry_price, or_low, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 18)

    def test_trailing_stop_disabled_by_default(self):
        """trailing_stop_pct=0 (default) behaves same as v1."""
        entry_price = 100.0
        bars = make_bars(16, [100, 103, 105, 104, 102])
        result_with = _resolve_exit(bars, entry_price, 90.0, {**EXIT_CONFIG, "trailing_stop_pct": 0})
        result_without = _resolve_exit(bars, entry_price, 90.0, EXIT_CONFIG)
        self.assertEqual(result_with, result_without)


# ============================================================
# F. Phase 4B: Min hold bars tests
# ============================================================

class TestMinHoldBars(unittest.TestCase):

    def test_min_hold_prevents_early_exit(self):
        """Target hit within min_hold period is ignored."""
        entry_price = 100.0
        config = {**EXIT_CONFIG, "min_hold_bars": 3}
        # bars_held:          1,     2,    3,    4
        # Bar 17 (101.5): target hit but bars_held=1 <= 3, skipped
        # Bar 20 (101.5): bars_held=4 > 3, target hit -> TRIGGERED
        bars = make_bars(16, [100, 101.5, 101, 100.5, 101.5])
        result = _resolve_exit(bars, entry_price, 90.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 20)

    def test_min_hold_zero_default(self):
        """min_hold_bars=0 behaves same as v1."""
        entry_price = 100.0
        bars = make_bars(16, [100, 101.5, 101, 100.5])
        result_with = _resolve_exit(bars, entry_price, 90.0, {**EXIT_CONFIG, "min_hold_bars": 0})
        result_without = _resolve_exit(bars, entry_price, 90.0, EXIT_CONFIG)
        self.assertEqual(result_with, result_without)

    def test_min_hold_tracks_trailing_during_hold(self):
        """Trailing stop updates highest during min_hold (doesn't freeze)."""
        entry_price = 100.0
        config = {"target_pct": 0.10, "stop_pct": 0.01, "max_hold_bars": 60,
                  "trailing_stop_pct": 0.02, "min_hold_bars": 2}
        # Bar 17 (105): bars_held=1 <= 2, skip. But highest->105, trail->102.9
        # Bar 18 (100): bars_held=2 <= 2, skip.
        # Bar 19 (103.5): bars_held=3 > 2. 103.5 > 102.9, no trigger
        # Bar 20 (102): bars_held=4 > 2. 102 <= 102.9, TRIGGERED!
        # If tracking froze during hold, highest=100, trail=98, bar 20 wouldn't trigger.
        bars = make_bars(16, [100, 105, 100, 103.5, 102])
        result = _resolve_exit(bars, entry_price, 85.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 20)
        self.assertEqual(result["exit_price"], 102)


# ============================================================
# G. Phase 4B: Bar hi/lo exit tests
# ============================================================

class TestBarHiLoExit(unittest.TestCase):

    def test_hilo_target_uses_bar_high(self):
        """Target triggered by bar high even when close is below target."""
        entry_price = 100.0
        config = {**EXIT_CONFIG, "use_bar_hilo": True}
        # Target = 101.5. Bar 17: close=101 (below), high=102 (above)
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 100.5, "high": 102, "low": 100, "close": 101},
        ]
        result = _resolve_exit(bars, entry_price, 90.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertAlmostEqual(result["exit_price"], 101.5)  # filled at target_price

    def test_hilo_stop_uses_bar_low(self):
        """Stop triggered by bar low even when close is above stop."""
        entry_price = 100.0
        or_low = 98.0  # stop = min(99, 98) = 98
        config = {**EXIT_CONFIG, "use_bar_hilo": True}
        # Bar 17: close=99.5 (above stop), low=97.5 (below stop)
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 99.5, "high": 100, "low": 97.5, "close": 99.5},
        ]
        result = _resolve_exit(bars, entry_price, or_low, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertAlmostEqual(result["exit_price"], 98.0)  # filled at stop_price

    def test_hilo_both_triggered_conservative(self):
        """Both target and stop hit same bar -> exit at stop (conservative)."""
        entry_price = 100.0
        or_low = 98.0  # stop = min(99, 98) = 98
        config = {**EXIT_CONFIG, "use_bar_hilo": True}
        # Wide bar: high=103 (>= target 101.5), low=97 (<= stop 98)
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 100, "high": 103, "low": 97, "close": 100},
        ]
        result = _resolve_exit(bars, entry_price, or_low, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertAlmostEqual(result["exit_price"], 98.0)  # stop wins

    def test_hilo_disabled_by_default(self):
        """use_bar_hilo=False (default) only checks close, ignoring high/low."""
        entry_price = 100.0
        # Bar 17: high=102 (above target 101.5), but close=101 (below)
        # With use_bar_hilo=False, target NOT triggered
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 100.5, "high": 102, "low": 100, "close": 101},
            {"bar_num": 18, "open": 101, "high": 103, "low": 100.5, "close": 100.8},
        ]
        result = _resolve_exit(bars, entry_price, 90.0, EXIT_CONFIG)
        self.assertEqual(result["exit_type"], "eod")
        self.assertEqual(result["exit_price"], 100.8)

    def test_hilo_exit_price_at_target(self):
        """With use_bar_hilo, target exit fills at target_price, not bar close."""
        entry_price = 100.0
        config = {**EXIT_CONFIG, "use_bar_hilo": True}
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 101, "high": 105, "low": 100.5, "close": 103},
        ]
        result = _resolve_exit(bars, entry_price, 90.0, config)
        # Target=101.5, high=105 triggers. Exit at 101.5, not close=103
        self.assertAlmostEqual(result["exit_price"], 101.5)

    def test_hilo_exit_price_at_stop(self):
        """With use_bar_hilo, stop exit fills at stop_price, not bar close."""
        entry_price = 100.0
        or_low = 98.0  # stop = min(99, 98) = 98
        config = {**EXIT_CONFIG, "use_bar_hilo": True}
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 99, "high": 99.5, "low": 95, "close": 96},
        ]
        result = _resolve_exit(bars, entry_price, or_low, config)
        # Stop=98, low=95 triggers. Exit at 98, not close=96
        self.assertAlmostEqual(result["exit_price"], 98.0)


# ============================================================
# H. Phase 4B: Combined feature tests
# ============================================================

class TestCombinedFeatures(unittest.TestCase):

    def test_trailing_with_min_hold(self):
        """Trailing stop + min hold: trailing updates during hold, exits after."""
        entry_price = 100.0
        config = {"target_pct": 0.10, "stop_pct": 0.01, "max_hold_bars": 60,
                  "trailing_stop_pct": 0.02, "min_hold_bars": 2}
        # Bar 17 (105): bars_held=1 <= 2, skip. highest->105, trail->102.9
        # Bar 18 (103): bars_held=2 <= 2, skip.
        # Bar 19 (104): bars_held=3 > 2. 104 > 102.9, no trigger
        # Bar 20 (102): bars_held=4 > 2. 102 <= 102.9, TRIGGERED
        bars = make_bars(16, [100, 105, 103, 104, 102])
        result = _resolve_exit(bars, entry_price, 85.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 20)

    def test_all_features_combined(self):
        """Trailing + min hold + bar hilo all active."""
        entry_price = 100.0
        config = {
            **EXIT_CONFIG,
            "trailing_stop_pct": 0.02,
            "min_hold_bars": 1,
            "use_bar_hilo": True,
        }
        bars = [
            {"bar_num": 16, "open": 100, "high": 101, "low": 99, "close": 100},
            {"bar_num": 17, "open": 102, "high": 106, "low": 102, "close": 104},
            {"bar_num": 18, "open": 104, "high": 105, "low": 100, "close": 103},
        ]
        # Bar 17: bars_held=1 <= 1, skip. highest=106 (bar high), trail=103.88
        # Bar 18: bars_held=2 > 1. price_low=100 <= 103.88 -> stop hit
        #   exit_price = stop_price = max(min(99,85), 103.88) = 103.88
        result = _resolve_exit(bars, entry_price, 85.0, config)
        self.assertEqual(result["exit_type"], "signal")
        self.assertEqual(result["exit_bar"], 18)
        self.assertAlmostEqual(result["exit_price"], 103.88)


# ============================================================
# I. Phase 4B: Full simulator tests with new features
# ============================================================

class TestSimulatorPhase4B(unittest.TestCase):

    def test_simulator_with_trailing_stop(self):
        """Full simulator with trailing stop: profit locked in."""
        bars = make_bars(16, [100, 103, 105, 104, 102])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        # High target (10%) so trailing stop triggers before target
        config = {**DEFAULT_CONFIG, "target_pct": 0.10, "trailing_stop_pct": 0.02}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 1)
        tl = result["trade_log"][0]
        self.assertEqual(tl["exit_price"], 102)
        self.assertEqual(tl["exit_type"], "signal")
        self.assertGreater(tl["pnl"], 0)

    def test_simulator_with_bar_hilo(self):
        """Full simulator with bar hilo: exit at target_price, not close."""
        bars = [
            {"bar_num": 16, "open": 100, "high": 100.5, "low": 99.5, "close": 100},
            {"bar_num": 17, "open": 100.2, "high": 102, "low": 100, "close": 101},
        ]
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        config = {**DEFAULT_CONFIG, "use_bar_hilo": True}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 1)
        tl = result["trade_log"][0]
        # Target=101.5, high=102 triggers, fills at 101.5
        self.assertAlmostEqual(tl["exit_price"], 101.5)


# ============================================================
# J. Phase 4C: Position sizing tests
# ============================================================

class TestComputeOrderValue(unittest.TestCase):

    def test_fixed_default(self):
        config = {"order_value": 50000}
        self.assertEqual(_compute_order_value("fixed", config, 500000, 5), 50000)

    def test_equal_weight(self):
        self.assertEqual(_compute_order_value("equal_weight", {}, 500000, 5), 100000)

    def test_equal_weight_zero_positions(self):
        self.assertEqual(_compute_order_value("equal_weight", {}, 500000, 0), 0)

    def test_pct_equity(self):
        config = {"sizing_pct": 20}
        self.assertEqual(_compute_order_value("pct_equity", config, 500000, 5), 100000)

    def test_pct_equity_default_10pct(self):
        self.assertEqual(_compute_order_value("pct_equity", {}, 500000, 5), 50000)

    def test_unknown_type_falls_back_to_fixed(self):
        config = {"order_value": 30000}
        self.assertEqual(_compute_order_value("unknown", config, 500000, 5), 30000)


class TestDynamicSizing(unittest.TestCase):

    def test_fixed_sizing_backward_compat(self):
        """sizing_type=fixed gives same results as no sizing_type."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        result_default = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        result_fixed = simulate_intraday_v2(matrix, {**DEFAULT_CONFIG, "sizing_type": "fixed"})
        self.assertEqual(result_default["trade_log"][0]["pnl"],
                         result_fixed["trade_log"][0]["pnl"])

    def test_equal_weight_sizing(self):
        """equal_weight: position size = margin / max_positions."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        config = {**DEFAULT_CONFIG, "sizing_type": "equal_weight"}
        # margin=500K, max_positions=5 -> ov=100K
        result = simulate_intraday_v2(matrix, config)
        tl = result["trade_log"][0]
        self.assertEqual(tl["order_value"], 100000)
        # PnL should be 2x the default (100K vs 50K order_value)
        default_result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        pnl_ratio = tl["pnl"] / default_result["trade_log"][0]["pnl"]
        self.assertAlmostEqual(pnl_ratio, 2.0, places=1)

    def test_pct_equity_sizing(self):
        """pct_equity: position size = margin * sizing_pct / 100."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        config = {**DEFAULT_CONFIG, "sizing_type": "pct_equity", "sizing_pct": 20}
        # margin=500K * 20% = 100K
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_log"][0]["order_value"], 100000)

    def test_equal_weight_compounds(self):
        """Day 2 order value reflects updated margin (compounding)."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = []
        matrix.extend(make_signal_matrix_from_bars(
            "A.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "B.NS", "2024-01-16", 16, 100.0, bars, or_low=90.0
        ))
        config = {**DEFAULT_CONFIG, "sizing_type": "equal_weight"}
        result = simulate_intraday_v2(matrix, config)
        # Day 1 ov = 500K/5 = 100K
        self.assertEqual(result["trade_log"][0]["order_value"], 100000)
        # Day 2: margin grew from winning trade, ov should be > 100K
        self.assertGreater(result["trade_log"][1]["order_value"], 100000)

    def test_max_order_value_cap(self):
        """max_order_value caps the computed order value."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        # equal_weight would give 100K, but cap at 75K
        config = {**DEFAULT_CONFIG, "sizing_type": "equal_weight",
                  "max_order_value": 75000}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_log"][0]["order_value"], 75000)

    def test_margin_check_skips_trade(self):
        """Insufficient margin skips trades."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = []
        for i in range(10):
            matrix.extend(make_signal_matrix_from_bars(
                f"S{i}.NS", "2024-01-15", 16, 100.0, bars,
                or_low=90.0, signal_strength=i * 0.01
            ))
        # margin=100K, equal_weight -> ov=20K, max_positions=5
        # 5 * 20K = 100K = margin, all 5 fit
        config = {**DEFAULT_CONFIG, "initial_capital": 100000,
                  "sizing_type": "equal_weight", "max_positions": 5}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 5)

        # margin=80K -> ov=16K, 5*16K=80K, all 5 fit
        config2 = {**config, "initial_capital": 80000}
        result2 = simulate_intraday_v2(matrix, config2)
        self.assertEqual(result2["trade_count"], 5)

        # margin=60K -> ov=12K, 5*12K=60K, all 5 fit
        config3 = {**config, "initial_capital": 60000}
        result3 = simulate_intraday_v2(matrix, config3)
        self.assertEqual(result3["trade_count"], 5)

    def test_margin_insufficient_for_fixed(self):
        """With fixed sizing, margin < order_value -> no trades."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        # margin=10K but order_value=50K -> can't afford
        config = {**DEFAULT_CONFIG, "initial_capital": 10000}
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["trade_count"], 0)

    def test_per_instrument_limit(self):
        """Per-instrument limit rejects duplicate symbols."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        # Two entries for same symbol (shouldn't happen with ORB SQL, but test the guard)
        matrix = []
        matrix.extend(make_signal_matrix_from_bars(
            "SAME.NS", "2024-01-15", 16, 100.0, bars,
            or_low=90.0, signal_strength=0.10
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "SAME.NS", "2024-01-15", 18, 200.0,
            make_bars(18, [200, 201, 203]),
            or_low=180.0, signal_strength=0.05
        ))
        config = {**DEFAULT_CONFIG, "max_positions_per_instrument": 1}
        result = simulate_intraday_v2(matrix, config)
        # Only 1 trade for SAME.NS despite 2 entries
        self.assertEqual(result["trade_count"], 1)

    def test_order_value_in_trade_log(self):
        """trade_log includes order_value field."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertIn("order_value", result["trade_log"][0])
        self.assertEqual(result["trade_log"][0]["order_value"], 50000)


# ============================================================
# K. Phase 4D: Walk-forward ranking tests
# ============================================================

class TestRankEntries(unittest.TestCase):

    def test_signal_strength_default(self):
        """Default ranking sorts by signal_strength descending."""
        entries = [
            {"symbol": "A", "signal_strength": 0.03},
            {"symbol": "B", "signal_strength": 0.10},
            {"symbol": "C", "signal_strength": 0.05},
        ]
        ranked = _rank_entries(entries, "signal_strength", {})
        self.assertEqual([e["symbol"] for e in ranked], ["B", "C", "A"])

    def test_top_performer_positive_first(self):
        """top_performer puts positive-P&L symbols before zero/negative."""
        entries = [
            {"symbol": "LOSER", "signal_strength": 0.10},
            {"symbol": "WINNER", "signal_strength": 0.01},
            {"symbol": "NEW", "signal_strength": 0.05},
        ]
        scores = {"WINNER": 5.0, "LOSER": -3.0}  # NEW has no history
        ranked = _rank_entries(entries, "top_performer", scores)
        self.assertEqual(ranked[0]["symbol"], "WINNER")

    def test_top_performer_score_ordering(self):
        """top_performer ranks by score descending within positive group."""
        entries = [
            {"symbol": "A", "signal_strength": 0.01},
            {"symbol": "B", "signal_strength": 0.01},
            {"symbol": "C", "signal_strength": 0.01},
        ]
        scores = {"A": 10.0, "B": 20.0, "C": 5.0}
        ranked = _rank_entries(entries, "top_performer", scores)
        self.assertEqual([e["symbol"] for e in ranked], ["B", "A", "C"])

    def test_top_performer_tiebreak_by_signal_strength(self):
        """When scores are equal, tiebreak by signal_strength."""
        entries = [
            {"symbol": "A", "signal_strength": 0.03},
            {"symbol": "B", "signal_strength": 0.08},
        ]
        scores = {"A": 5.0, "B": 5.0}
        ranked = _rank_entries(entries, "top_performer", scores)
        self.assertEqual(ranked[0]["symbol"], "B")


class TestComputeSymbolScores(unittest.TestCase):

    def test_trailing_window(self):
        """Only recent P&L within window is summed."""
        history = {
            "A": [("2024-01-01", 2.0), ("2024-06-01", 3.0), ("2024-07-01", 1.0)],
        }
        # Window=90 days from 2024-07-15 -> cutoff ~2024-04-16
        scores = _compute_symbol_scores(history, "2024-07-15", 90)
        # Only 2024-06-01 (3.0) and 2024-07-01 (1.0) are within window
        self.assertAlmostEqual(scores["A"], 4.0)

    def test_empty_history(self):
        scores = _compute_symbol_scores({}, "2024-07-15", 180)
        self.assertEqual(scores, {})

    def test_all_outside_window(self):
        """Trades older than window contribute 0."""
        history = {"A": [("2023-01-01", 5.0), ("2023-02-01", 3.0)]}
        scores = _compute_symbol_scores(history, "2024-07-15", 180)
        self.assertAlmostEqual(scores["A"], 0.0)


class TestWalkForwardSimulation(unittest.TestCase):

    def test_top_performer_shifts_selection(self):
        """Walk-forward ranking changes which symbols are selected over time."""
        # Day 1: A wins, B loses. Both available, max_positions=1.
        # Default (signal_strength) would always pick the higher SS.
        # top_performer should favor A on day 2 (positive trailing P&L).
        bars_win = make_bars(16, [100, 100.5, 101, 101.5])  # target hit
        bars_lose = make_bars(16, [100, 95, 89])  # stop hit

        matrix = []
        # Day 1: both available, A has higher SS -> A selected (wins)
        matrix.extend(make_signal_matrix_from_bars(
            "A.NS", "2024-01-15", 16, 100.0, bars_win,
            or_low=90.0, signal_strength=0.10
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "B.NS", "2024-01-15", 16, 100.0, bars_lose,
            or_low=90.0, signal_strength=0.05
        ))
        # Day 2: same entries but B has higher SS. With signal_strength ranking,
        # B would be selected. With top_performer, A should be selected
        # (positive trailing P&L from day 1).
        matrix.extend(make_signal_matrix_from_bars(
            "A.NS", "2024-01-16", 16, 100.0, bars_win,
            or_low=90.0, signal_strength=0.03  # lower SS
        ))
        matrix.extend(make_signal_matrix_from_bars(
            "B.NS", "2024-01-16", 16, 100.0, bars_lose,
            or_low=90.0, signal_strength=0.08  # higher SS
        ))

        # With signal_strength: day 1 picks A (ss=0.10), day 2 picks B (ss=0.08)
        config_ss = {**DEFAULT_CONFIG, "max_positions": 1,
                     "ranking_type": "signal_strength"}
        result_ss = simulate_intraday_v2(matrix, config_ss)
        self.assertEqual(result_ss["trade_log"][1]["symbol"], "B.NS")

        # With top_performer: day 1 picks A (highest SS, no history).
        # Day 2: A has positive score, B has no score -> A selected
        config_tp = {**DEFAULT_CONFIG, "max_positions": 1,
                     "ranking_type": "top_performer"}
        result_tp = simulate_intraday_v2(matrix, config_tp)
        self.assertEqual(result_tp["trade_log"][1]["symbol"], "A.NS")

    def test_signal_strength_backward_compat(self):
        """ranking_type=signal_strength gives same results as default."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        result_default = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        result_ss = simulate_intraday_v2(matrix, {**DEFAULT_CONFIG,
                                                   "ranking_type": "signal_strength"})
        self.assertEqual(result_default["trade_log"][0]["pnl"],
                         result_ss["trade_log"][0]["pnl"])


# ============================================================
# L. Phase 4E: Payout simulation tests
# ============================================================

class TestComputePayout(unittest.TestCase):

    def test_fixed_payout(self):
        self.assertEqual(_compute_payout({"type": "fixed", "value": 10000}, 500000), 10000)

    def test_percentage_payout(self):
        self.assertEqual(_compute_payout({"type": "percentage", "value": 5}, 500000), 25000)

    def test_payout_capped_at_margin(self):
        """Can't withdraw more than available margin."""
        self.assertEqual(_compute_payout({"type": "fixed", "value": 100000}, 50000), 50000)

    def test_payout_zero_margin(self):
        self.assertEqual(_compute_payout({"type": "fixed", "value": 10000}, 0), 0)


class TestPayoutSimulation(unittest.TestCase):

    def _make_multi_day_matrix(self, n_days):
        """Create signal matrix with one entry per day for n_days."""
        matrix = []
        bars = make_bars(16, [100, 100.5, 101, 101.5])  # target hit
        for i in range(n_days):
            day = f"2024-01-{15 + i:02d}"
            matrix.extend(make_signal_matrix_from_bars(
                f"S{i}.NS", day, 16, 100.0, bars, or_low=90.0
            ))
        return matrix

    def test_payout_reduces_margin(self):
        """Payout withdraws capital, reducing margin."""
        matrix = self._make_multi_day_matrix(5)
        # Payout every 2 days, 10K fixed, no lockup
        config = {
            **DEFAULT_CONFIG,
            "payout": {"type": "fixed", "value": 10000,
                       "interval_days": 2, "lockup_days": 0},
        }
        result = simulate_intraday_v2(matrix, config)
        # Should have withdrawals
        self.assertGreater(result["total_withdrawn"], 0)
        # Margin should be lower than without payouts
        result_no_payout = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertLess(
            result["day_wise_log"][-1]["margin_available"],
            result_no_payout["day_wise_log"][-1]["margin_available"],
        )

    def test_payout_lockup(self):
        """No payouts during lockup period."""
        matrix = self._make_multi_day_matrix(3)
        # Interval=1, lockup=10 (longer than 3 days) -> no payouts
        config = {
            **DEFAULT_CONFIG,
            "payout": {"type": "fixed", "value": 10000,
                       "interval_days": 1, "lockup_days": 10},
        }
        result = simulate_intraday_v2(matrix, config)
        self.assertEqual(result["total_withdrawn"], 0)

    def test_payout_percentage(self):
        """Percentage payout withdraws % of current margin."""
        matrix = self._make_multi_day_matrix(5)
        config = {
            **DEFAULT_CONFIG,
            "payout": {"type": "percentage", "value": 2,
                       "interval_days": 2, "lockup_days": 0},
        }
        result = simulate_intraday_v2(matrix, config)
        self.assertGreater(result["total_withdrawn"], 0)

    def test_no_payout_by_default(self):
        """Without payout config, total_withdrawn is 0."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        result = simulate_intraday_v2(matrix, DEFAULT_CONFIG)
        self.assertEqual(result["total_withdrawn"], 0)


# ============================================================
# M. Phase 4F: Parallel sweep tests
# ============================================================

class TestParallelSweep(unittest.TestCase):

    def test_parallel_matches_sequential(self):
        """Parallel sweep produces same results as sequential."""
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        configs = [
            {**DEFAULT_CONFIG, "target_pct": 0.015},
            {**DEFAULT_CONFIG, "target_pct": 0.02},
            {**DEFAULT_CONFIG, "target_pct": 0.03},
        ]
        # Sequential
        seq_results = [simulate_intraday_v2(matrix, c) for c in configs]
        # Parallel (force max_workers=1 to avoid spawn issues in tests)
        par_results = run_parallel_sweep(matrix, configs, max_workers=1)

        self.assertEqual(len(par_results), len(seq_results))
        for s, p in zip(seq_results, par_results):
            self.assertEqual(s["trade_count"], p["trade_count"])
            self.assertAlmostEqual(
                s["trade_log"][0]["pnl"] if s["trade_log"] else 0,
                p["trade_log"][0]["pnl"] if p["trade_log"] else 0,
            )

    def test_empty_configs(self):
        self.assertEqual(run_parallel_sweep([], []), [])

    def test_single_config(self):
        bars = make_bars(16, [100, 100.5, 101, 101.5])
        matrix = make_signal_matrix_from_bars(
            "TEST.NS", "2024-01-15", 16, 100.0, bars, or_low=90.0
        )
        results = run_parallel_sweep(matrix, [DEFAULT_CONFIG], max_workers=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trade_count"], 1)


if __name__ == "__main__":
    unittest.main()

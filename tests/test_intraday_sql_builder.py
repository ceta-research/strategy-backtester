"""Tests for engine/intraday_sql_builder.py"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_sql_builder import build_orb_sql, build_orb_signal_sql, build_vwap_mr_signal_sql


DEFAULT_CFG = {
    "start_date": "2020-01-06",
    "end_date": "2026-03-09",
    "min_volume": 5000000,
    "min_price": 100,
    "min_range_pct": 0.01,
    "or_window": 15,
    "max_entry_bar": 120,
    "target_pct": 0.015,
    "stop_pct": 0.01,
    "max_hold_bars": 60,
}


class TestBuildOrbSql(unittest.TestCase):

    def test_contains_all_ctes(self):
        sql = build_orb_sql(DEFAULT_CFG)
        expected_ctes = [
            "filtered_eod", "bench", "bars", "opening_range",
            "entry_candidates", "first_entry", "exit_candidates",
            "first_exit", "eod_exit",
        ]
        for cte in expected_ctes:
            self.assertIn(cte, sql, f"Missing CTE: {cte}")

    def test_params_injected(self):
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("2020-01-06", sql)
        self.assertIn("2026-03-09", sql)
        self.assertIn("5000000", sql)
        self.assertIn("100", sql)
        self.assertIn("0.01", sql)
        self.assertIn("15", sql)
        self.assertIn("120", sql)

    def test_target_stop_factors(self):
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("1.015", sql)
        self.assertIn("0.99", sql)

    def test_different_target_stop(self):
        cfg = {**DEFAULT_CFG, "target_pct": 0.02, "stop_pct": 0.015}
        sql = build_orb_sql(cfg)
        self.assertIn("1.02", sql)
        self.assertIn("0.985", sql)

    def test_output_columns(self):
        sql = build_orb_sql(DEFAULT_CFG)
        for col in ["symbol", "trade_date", "entry_bar", "entry_price",
                     "exit_price", "exit_type", "or_range_pct",
                     "signal_strength", "bench_ret"]:
            self.assertIn(col, sql, f"Missing output column: {col}")

    def test_returns_string(self):
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIsInstance(sql, str)
        self.assertGreater(len(sql), 500)

    def test_nse_filter(self):
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("%.NS", sql)
        self.assertIn("exchange = 'NSE'", sql)

    def test_or_window_30(self):
        cfg = {**DEFAULT_CFG, "or_window": 30, "max_entry_bar": 60}
        sql = build_orb_sql(cfg)
        self.assertIn("bar_num <= 30", sql)
        self.assertIn("bar_num > 30", sql)
        self.assertIn("bar_num <= 60", sql)

    # --- New tests ---

    def test_lag_present_no_look_ahead(self):
        """Regression test: LAG(volume) and prev_day_volume in SQL."""
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("LAG(volume)", sql)
        self.assertIn("prev_day_volume", sql)
        # Also check the range filter uses previous day
        self.assertIn("prev_day_range_pct", sql)

    def test_all_eod_cte_present(self):
        """New all_eod CTE exists (introduced for look-ahead fix)."""
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("all_eod", sql)
        # all_eod should be defined as a CTE
        self.assertIn("all_eod AS", sql)

    def test_custom_symbol_filter_us(self):
        """Custom symbol_filter/exchange_filter overrides NSE defaults."""
        cfg = {
            **DEFAULT_CFG,
            "symbol_filter": "symbol NOT LIKE '%.%'",
            "exchange_filter": "m.exchange = 'NASDAQ'",
        }
        sql = build_orb_sql(cfg)
        self.assertIn("symbol NOT LIKE '%.%'", sql)
        self.assertIn("m.exchange = 'NASDAQ'", sql)
        # NSE defaults should NOT be present
        self.assertNotIn("%.NS", sql)
        self.assertNotIn("exchange = 'NSE'", sql)

    def test_default_nse_when_no_override(self):
        """Without overrides, defaults to %.NS and exchange = 'NSE'."""
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("%.NS", sql)
        self.assertIn("exchange = 'NSE'", sql)

    def test_eod_exit_fallback_coalesce(self):
        """COALESCE(x.exit_price, eod.eod_price) present."""
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("COALESCE(x.exit_price, eod.eod_price)", sql)

    def test_exit_uses_least_for_stop(self):
        """LEAST(e.entry_price * stop_factor, e.or_low) present."""
        sql = build_orb_sql(DEFAULT_CFG)
        self.assertIn("LEAST(", sql)
        self.assertIn("e.or_low", sql)


SIGNAL_CFG = {
    "start_date": "2020-01-06",
    "end_date": "2026-03-09",
    "min_volume": 5000000,
    "min_price": 100,
    "min_range_pct": 0.01,
    "or_window": 15,
    "max_entry_bar": 120,
}


class TestBuildOrbSignalSql(unittest.TestCase):

    def test_signal_sql_no_exit_ctes(self):
        """build_orb_signal_sql() does NOT contain exit CTEs."""
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertNotIn("exit_candidates", sql)
        self.assertNotIn("first_exit", sql)
        self.assertNotIn("eod_exit", sql)

    def test_signal_sql_has_bar_columns(self):
        """Output has bar_open, bar_high, bar_low, bar_close."""
        sql = build_orb_signal_sql(SIGNAL_CFG)
        for col in ["bar_open", "bar_high", "bar_low", "bar_close"]:
            self.assertIn(col, sql, f"Missing column: {col}")

    def test_signal_sql_no_target_stop(self):
        """No target_factor or stop_factor in SQL."""
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertNotIn("target_factor", sql)
        self.assertNotIn("stop_factor", sql)
        # Should not contain pre-computed factors like 1.015 or 0.99
        # (these are strategy-specific values from v1)
        self.assertNotIn("LEAST(", sql)
        self.assertNotIn("COALESCE(x.exit_price", sql)

    def test_signal_sql_has_entry_ctes(self):
        """Has all shared CTEs: all_eod through first_entry."""
        sql = build_orb_signal_sql(SIGNAL_CFG)
        for cte in ["all_eod", "filtered_eod", "bench", "bars",
                     "opening_range", "entry_candidates", "first_entry"]:
            self.assertIn(cte, sql, f"Missing CTE: {cte}")

    def test_signal_sql_returns_string(self):
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertIsInstance(sql, str)
        self.assertGreater(len(sql), 400)

    def test_signal_sql_has_signal_strength(self):
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertIn("signal_strength", sql)

    def test_signal_sql_has_bench_ret(self):
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertIn("bench_ret", sql)

    def test_signal_sql_joins_bars_from_entry(self):
        """Bars joined from entry_bar onward (bar_num >= entry_bar)."""
        sql = build_orb_signal_sql(SIGNAL_CFG)
        self.assertIn("b.bar_num >= e.entry_bar", sql)


VWAP_CFG = {
    "start_date": "2020-01-06",
    "end_date": "2026-03-09",
    "min_volume": 5000000,
    "min_price": 100,
    "min_range_pct": 0.01,
    "warmup_bars": 30,
    "max_entry_bar": 120,
    "dip_pct": 0.01,
}


class TestBuildVwapMrSignalSql(unittest.TestCase):

    def test_returns_string(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIsInstance(sql, str)
        self.assertGreater(len(sql), 400)

    def test_has_vwap_computation(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIn("vwap", sql)
        # Running VWAP uses cumulative SUM of (typical_price * volume) / SUM(volume)
        self.assertIn("SUM(", sql)

    def test_has_shared_ctes(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        for cte in ["all_eod", "filtered_eod", "bench", "bars"]:
            self.assertIn(cte, sql, f"Missing CTE: {cte}")

    def test_no_opening_range_ctes(self):
        """VWAP MR does not use ORB-specific CTEs."""
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertNotIn("opening_range", sql)
        self.assertNotIn("or_window", sql)

    def test_has_entry_candidates(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIn("entry_candidates", sql)
        self.assertIn("first_entry", sql)

    def test_dip_pct_injected(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        # Entry condition: close < vwap * (1 - dip_pct)
        self.assertIn("0.01", sql)

    def test_warmup_bars_injected(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIn("30", sql)  # warmup_bars

    def test_output_columns(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        for col in ["symbol", "trade_date", "entry_bar", "entry_price",
                     "or_high", "or_low", "or_range", "signal_strength",
                     "bar_num", "bar_open", "bar_high", "bar_low", "bar_close",
                     "bench_ret"]:
            self.assertIn(col, sql, f"Missing output column: {col}")

    def test_no_exit_logic(self):
        """v2 signal SQL has no exit logic -- that's in Python."""
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertNotIn("exit_candidates", sql)
        self.assertNotIn("first_exit", sql)
        self.assertNotIn("eod_exit", sql)

    def test_bars_joined_from_entry(self):
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIn("b.bar_num >= e.entry_bar", sql)

    def test_split_adjustment_check(self):
        """Has FMP split-adjustment check like ORB."""
        sql = build_vwap_mr_signal_sql(VWAP_CFG)
        self.assertIn("eod_open * 0.8", sql)
        self.assertIn("eod_open * 1.2", sql)

    def test_custom_exchange_filter(self):
        cfg = {**VWAP_CFG,
               "symbol_filter": "symbol NOT LIKE '%.%'",
               "exchange_filter": "m.exchange = 'NASDAQ'"}
        sql = build_vwap_mr_signal_sql(cfg)
        self.assertIn("symbol NOT LIKE '%.%'", sql)
        self.assertIn("m.exchange = 'NASDAQ'", sql)

    def test_different_dip_pct(self):
        cfg = {**VWAP_CFG, "dip_pct": 0.015}
        sql = build_vwap_mr_signal_sql(cfg)
        self.assertIn("0.015", sql)


if __name__ == "__main__":
    unittest.main()

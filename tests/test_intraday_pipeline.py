"""Tests for engine/intraday_pipeline.py"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_pipeline import (
    _cartesian, _make_config_id, SQL_KEYS, SIM_KEYS,
    SQL_KEYS_V2, SIM_KEYS_V2,
    EXCHANGE_SQL_DEFAULTS, run_intraday_pipeline,
)


class TestCartesian(unittest.TestCase):

    def test_empty(self):
        result = _cartesian({})
        self.assertEqual(result, [{}])

    def test_single_param(self):
        result = _cartesian({"a": [1, 2]})
        self.assertEqual(len(result), 2)
        self.assertIn({"a": 1}, result)
        self.assertIn({"a": 2}, result)

    def test_two_params(self):
        result = _cartesian({"a": [1, 2], "b": [10, 20]})
        self.assertEqual(len(result), 4)
        self.assertIn({"a": 1, "b": 10}, result)
        self.assertIn({"a": 1, "b": 20}, result)
        self.assertIn({"a": 2, "b": 10}, result)
        self.assertIn({"a": 2, "b": 20}, result)

    def test_single_value_params(self):
        result = _cartesian({"a": [1], "b": [2]})
        self.assertEqual(result, [{"a": 1, "b": 2}])


class TestMakeConfigId(unittest.TestCase):

    def test_basic(self):
        config_id = _make_config_id({"min_volume": 5000000}, {"max_positions": 5})
        self.assertIsInstance(config_id, str)
        self.assertIn("volume=5000000", config_id)
        self.assertIn("mxpositions=5", config_id)

    def test_empty(self):
        config_id = _make_config_id({}, {})
        self.assertEqual(config_id, "")

    def test_pct_abbreviation(self):
        config_id = _make_config_id({"target_pct": 0.015}, {})
        self.assertIn("target=0.015", config_id)


class TestKeyClassification(unittest.TestCase):

    def test_no_overlap(self):
        """SQL_KEYS and SIM_KEYS must be disjoint."""
        overlap = SQL_KEYS & SIM_KEYS
        self.assertEqual(overlap, set(), f"Keys in both sets: {overlap}")

    def test_sql_keys_present(self):
        expected = {"min_volume", "min_price", "min_range_pct", "or_window",
                    "max_entry_bar", "target_pct", "stop_pct", "max_hold_bars"}
        self.assertEqual(SQL_KEYS, expected)

    def test_sim_keys_present(self):
        expected = {"max_positions", "order_value"}
        self.assertEqual(SIM_KEYS, expected)


class TestExchangeSqlDefaults(unittest.TestCase):

    def test_nse_defaults(self):
        d = EXCHANGE_SQL_DEFAULTS["NSE"]
        self.assertIn("%.NS", d["symbol_filter"])
        self.assertIn("NSE", d["exchange_filter"])

    def test_nasdaq_defaults(self):
        d = EXCHANGE_SQL_DEFAULTS["NASDAQ"]
        self.assertIn("NOT LIKE", d["symbol_filter"])
        self.assertIn("NASDAQ", d["exchange_filter"])

    def test_nyse_defaults(self):
        d = EXCHANGE_SQL_DEFAULTS["NYSE"]
        self.assertIn("NYSE", d["exchange_filter"])


class TestPipelineWithMockClient(unittest.TestCase):

    def _make_config_file(self, tmp_path, exchange="NSE"):
        """Create a minimal YAML config and return the path."""
        import tempfile
        import yaml

        config = {
            "static": {
                "strategy_name": "orb",
                "pipeline_version": "v1",
                "start_date": "2024-01-01",
                "end_date": "2024-03-31",
                "initial_capital": 500000,
                "risk_free_rate": 0.065,
                "exchange": exchange,
            },
            "scanner": {
                "min_volume": 5000000,
                "min_price": 100,
                "min_range_pct": 0.01,
            },
            "entry": {
                "or_window": 15,
                "max_entry_bar": 120,
            },
            "exit": {
                "target_pct": 0.015,
                "stop_pct": 0.01,
                "max_hold_bars": 60,
            },
            "simulation": {
                "max_positions": 5,
                "order_value": 50000,
            },
        }

        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f)
        return path

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_pipeline_produces_sorted_results(self, MockCR):
        """Mock CR query -> verify full pipeline produces SweepResult."""
        mock_trades = [
            {"symbol": "TEST.NS", "trade_date": "2024-01-15",
             "entry_price": 100.0, "exit_price": 102.0,
             "exit_type": "signal", "signal_strength": 0.05,
             "bench_ret": 0.001, "entry_bar": 16},
            {"symbol": "TEST2.NS", "trade_date": "2024-01-15",
             "entry_price": 200.0, "exit_price": 196.0,
             "exit_type": "eod", "signal_strength": 0.03,
             "bench_ret": 0.001, "entry_bar": 18},
            {"symbol": "TEST.NS", "trade_date": "2024-01-16",
             "entry_price": 100.0, "exit_price": 103.0,
             "exit_type": "signal", "signal_strength": 0.04,
             "bench_ret": 0.002, "entry_bar": 17},
        ]
        mock_client = MagicMock()
        mock_client.query.return_value = mock_trades
        MockCR.return_value = mock_client

        config_path = self._make_config_file(None)
        try:
            sweep = run_intraday_pipeline(config_path)
        finally:
            os.unlink(config_path)

        from lib.backtest_result import SweepResult
        self.assertIsInstance(sweep, SweepResult)
        self.assertEqual(len(sweep.configs), 1)  # 1 SQL combo x 1 sim combo
        params, result = sweep.configs[0]
        d = result.to_dict()
        self.assertIn("cagr", d["summary"])
        self.assertEqual(d["summary"]["total_trades"], 3)
        self.assertEqual(len(d["trades"]), 3)

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_pipeline_us_exchange_passes_to_sim(self, MockCR):
        """Pipeline with exchange=NASDAQ passes it through to simulator."""
        mock_trades = [
            {"symbol": "AAPL", "trade_date": "2024-01-15",
             "entry_price": 180.0, "exit_price": 182.0,
             "exit_type": "signal", "signal_strength": 0.05,
             "bench_ret": 0.001, "entry_bar": 16},
            {"symbol": "MSFT", "trade_date": "2024-01-16",
             "entry_price": 400.0, "exit_price": 404.0,
             "exit_type": "signal", "signal_strength": 0.04,
             "bench_ret": 0.002, "entry_bar": 17},
        ]
        mock_client = MagicMock()
        mock_client.query.return_value = mock_trades
        MockCR.return_value = mock_client

        config_path = self._make_config_file(None, exchange="NASDAQ")
        try:
            sweep = run_intraday_pipeline(config_path)
        finally:
            os.unlink(config_path)

        self.assertEqual(len(sweep.configs), 1)
        params, result = sweep.configs[0]
        d = result.to_dict()
        # US charges are much lower, so trade charges should be small
        for t in d["trades"]:
            self.assertLess(t["charges"], 5.0, "US charges should be <$5")

        # Verify SQL used NASDAQ exchange filter
        call_args = mock_client.query.call_args
        sql = call_args[0][0]
        self.assertIn("NASDAQ", sql)
        self.assertNotIn("%.NS", sql)


class TestV2KeyClassification(unittest.TestCase):

    def test_v2_no_overlap(self):
        """SQL_KEYS_V2 and SIM_KEYS_V2 must be disjoint."""
        overlap = SQL_KEYS_V2 & SIM_KEYS_V2
        self.assertEqual(overlap, set(), f"Keys in both sets: {overlap}")

    def test_v2_target_stop_hold_are_sim_keys(self):
        """target_pct, stop_pct, max_hold_bars classified as SIM in v2."""
        for key in ("target_pct", "stop_pct", "max_hold_bars"):
            self.assertIn(key, SIM_KEYS_V2, f"{key} should be in SIM_KEYS_V2")
            self.assertNotIn(key, SQL_KEYS_V2, f"{key} should NOT be in SQL_KEYS_V2")

    def test_v2_more_sim_keys(self):
        """v2 has more SIM keys than v1 (exit params moved to sim)."""
        self.assertGreater(len(SIM_KEYS_V2), len(SIM_KEYS))

    def test_v2_sql_keys_content(self):
        expected = {"min_volume", "min_price", "min_range_pct", "or_window", "max_entry_bar",
                    "min_rvol", "warmup_bars", "dip_pct"}
        self.assertEqual(SQL_KEYS_V2, expected)

    def test_v2_sim_keys_content(self):
        expected = {"max_positions", "order_value", "target_pct", "stop_pct", "max_hold_bars",
                    "trailing_stop_pct", "min_hold_bars", "use_bar_hilo",
                    "eod_buffer_bars",
                    "time_stop_bars", "use_atr_stop", "atr_multiplier", "exit_reentry_range",
                    "sizing_type", "sizing_pct", "max_order_value",
                    "max_positions_per_instrument",
                    "ranking_type", "ranking_window_days"}
        self.assertEqual(SIM_KEYS_V2, expected)


class TestV2PipelineWithMockClient(unittest.TestCase):

    def _make_v2_config_file(self, exchange="NSE"):
        """Create a minimal v2 YAML config and return the path."""
        import tempfile
        import yaml

        config = {
            "static": {
                "strategy_name": "orb",
                "pipeline_version": "v2",
                "start_date": "2024-01-01",
                "end_date": "2024-03-31",
                "initial_capital": 500000,
                "risk_free_rate": 0.065,
                "exchange": exchange,
            },
            "scanner": {
                "min_volume": 5000000,
                "min_price": 100,
                "min_range_pct": 0.01,
            },
            "entry": {
                "or_window": 15,
                "max_entry_bar": 120,
            },
            "exit": {
                "target_pct": 0.015,
                "stop_pct": 0.01,
                "max_hold_bars": 60,
            },
            "simulation": {
                "max_positions": 5,
                "order_value": 50000,
            },
        }

        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f)
        return path

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_v2_pipeline_produces_results(self, MockCR):
        """Mock CR query -> v2 pipeline produces SweepResult."""
        # Signal matrix rows: 2 entries, each with 5 bars
        mock_signal_matrix = []
        for bar_num in range(16, 21):
            close = 100 + (bar_num - 16) * 0.5
            mock_signal_matrix.append({
                "symbol": "TEST.NS", "trade_date": "2024-01-15",
                "entry_bar": 16, "entry_price": 100.0,
                "or_high": 105, "or_low": 90, "or_range": 15,
                "signal_strength": 0.05,
                "bar_num": bar_num,
                "bar_open": close, "bar_high": close + 1,
                "bar_low": close - 1, "bar_close": close,
                "bench_ret": 0.001,
            })
        for bar_num in range(18, 23):
            close = 200 + (bar_num - 18) * 1.0
            mock_signal_matrix.append({
                "symbol": "TEST2.NS", "trade_date": "2024-01-16",
                "entry_bar": 18, "entry_price": 200.0,
                "or_high": 210, "or_low": 185, "or_range": 25,
                "signal_strength": 0.03,
                "bar_num": bar_num,
                "bar_open": close, "bar_high": close + 2,
                "bar_low": close - 2, "bar_close": close,
                "bench_ret": 0.002,
            })

        mock_client = MagicMock()
        mock_client.query.return_value = mock_signal_matrix
        MockCR.return_value = mock_client

        config_path = self._make_v2_config_file()
        try:
            sweep = run_intraday_pipeline(config_path)
        finally:
            os.unlink(config_path)

        from lib.backtest_result import SweepResult
        self.assertIsInstance(sweep, SweepResult)
        self.assertEqual(len(sweep.configs), 1)
        params, result = sweep.configs[0]
        d = result.to_dict()
        self.assertGreater(d["summary"]["total_trades"], 0)
        self.assertGreater(len(d["trades"]), 0)

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_v1_dispatch(self, MockCR):
        """pipeline_version=v1 dispatches to v1 pipeline."""
        import tempfile
        import yaml

        config = {
            "static": {
                "strategy_name": "orb",
                "pipeline_version": "v1",
                "start_date": "2024-01-01",
                "end_date": "2024-03-31",
                "initial_capital": 500000,
                "risk_free_rate": 0.065,
                "exchange": "NSE",
            },
            "scanner": {"min_volume": 5000000, "min_price": 100, "min_range_pct": 0.01},
            "entry": {"or_window": 15, "max_entry_bar": 120},
            "exit": {"target_pct": 0.015, "stop_pct": 0.01, "max_hold_bars": 60},
            "simulation": {"max_positions": 5, "order_value": 50000},
        }

        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f)

        mock_trades = [
            {"symbol": "TEST.NS", "trade_date": "2024-01-15",
             "entry_price": 100.0, "exit_price": 102.0,
             "exit_type": "signal", "signal_strength": 0.05,
             "bench_ret": 0.001, "entry_bar": 16},
            {"symbol": "TEST.NS", "trade_date": "2024-01-16",
             "entry_price": 100.0, "exit_price": 103.0,
             "exit_type": "signal", "signal_strength": 0.04,
             "bench_ret": 0.002, "entry_bar": 17},
        ]
        mock_client = MagicMock()
        mock_client.query.return_value = mock_trades
        MockCR.return_value = mock_client

        try:
            sweep = run_intraday_pipeline(path)
        finally:
            os.unlink(path)

        from lib.backtest_result import SweepResult
        self.assertIsInstance(sweep, SweepResult)
        self.assertEqual(len(sweep.configs), 1)
        params, result = sweep.configs[0]
        self.assertEqual(result.to_dict()["summary"]["total_trades"], 2)


class TestVwapMrRegistration(unittest.TestCase):

    def test_vwap_mr_in_sql_builders_v2(self):
        from engine.intraday_pipeline import SQL_BUILDERS_V2
        self.assertIn("vwap_mr", SQL_BUILDERS_V2)

    def test_vwap_mr_builder_callable(self):
        from engine.intraday_pipeline import SQL_BUILDERS_V2
        builder = SQL_BUILDERS_V2["vwap_mr"]
        cfg = {
            "start_date": "2024-01-01", "end_date": "2024-03-31",
            "min_volume": 5000000, "min_price": 100, "min_range_pct": 0.01,
            "warmup_bars": 30, "max_entry_bar": 120, "dip_pct": 0.01,
        }
        sql = builder(cfg)
        self.assertIsInstance(sql, str)
        self.assertIn("vwap", sql)


class TestSweepResultStructure(unittest.TestCase):
    """Verify SweepResult from pipeline has correct structure."""

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_sweep_result_has_meta_and_configs(self, MockCR):
        """SweepResult has .meta with strategy info and .configs with BacktestResults."""
        mock_trades = [
            {"symbol": "TEST.NS", "trade_date": "2024-01-15",
             "entry_price": 100.0, "exit_price": 102.0,
             "exit_type": "signal", "signal_strength": 0.05,
             "bench_ret": 0.001, "entry_bar": 16},
            {"symbol": "TEST.NS", "trade_date": "2024-01-16",
             "entry_price": 101.0, "exit_price": 103.0,
             "exit_type": "signal", "signal_strength": 0.04,
             "bench_ret": 0.002, "entry_bar": 17},
        ]
        mock_client = MagicMock()
        mock_client.query.return_value = mock_trades
        MockCR.return_value = mock_client

        config_path = TestPipelineWithMockClient._make_config_file(self, None)
        try:
            sweep = run_intraday_pipeline(config_path)
        finally:
            os.unlink(config_path)

        # Meta fields
        self.assertEqual(sweep.meta["strategy_name"], "orb")
        self.assertEqual(sweep.meta["exchange"], "NSE")
        self.assertEqual(sweep.meta["capital"], 500000)

        # Config BacktestResult has equity_curve, trades, summary
        params, br = sweep.configs[0]
        d = br.to_dict()
        self.assertEqual(d["type"], "single")
        self.assertIn("equity_curve", d)
        self.assertIn("trades", d)
        self.assertIn("monthly_returns", d)
        self.assertIn("yearly_returns", d)
        self.assertIn("costs", d)

        # Summary has standard metric keys
        for key in ("cagr", "max_drawdown", "sharpe_ratio", "total_trades"):
            self.assertIn(key, d["summary"], f"Missing metric: {key}")

    @patch("engine.intraday_pipeline.CetaResearch")
    def test_sweep_save_produces_valid_json(self, MockCR):
        """SweepResult.save() writes parseable JSON with correct schema."""
        import json
        import tempfile

        mock_trades = [
            {"symbol": "TEST.NS", "trade_date": "2024-01-15",
             "entry_price": 100.0, "exit_price": 102.0,
             "exit_type": "signal", "signal_strength": 0.05,
             "bench_ret": 0.001, "entry_bar": 16},
        ]
        mock_client = MagicMock()
        mock_client.query.return_value = mock_trades
        MockCR.return_value = mock_client

        config_path = TestPipelineWithMockClient._make_config_file(self, None)
        try:
            sweep = run_intraday_pipeline(config_path)
        finally:
            os.unlink(config_path)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name

        try:
            sweep.save(out_path)
            with open(out_path) as f:
                data = json.load(f)

            self.assertEqual(data["type"], "sweep")
            self.assertEqual(data["total_configs"], 1)
            self.assertIn("all_configs", data)
            self.assertIn("detailed", data)
            self.assertEqual(len(data["detailed"]), 1)
            self.assertIn("equity_curve", data["detailed"][0])
        finally:
            os.unlink(out_path)


if __name__ == "__main__":
    unittest.main()

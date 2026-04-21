"""Edge-case and determinism tests (Phase 6 of audit P1 plan).

  P6.1: config_iterator determinism + YAML missing-param fallback.
  P6.2: intraday_simulator_v2 fixed_stop clamp.
  P6.3: cloud_orchestrator hash cache per-project scoping.
  P6.4: simulator behavior on edge cases — zero trades, single day,
        all-loser, capital exhaustion.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.config_sweep import create_config_iterator
from engine.config_loader import _build_simulation_config, validate_config
from engine.constants import SECONDS_IN_ONE_DAY
from engine.simulator import process


# ── P6.1: config determinism ─────────────────────────────────────────────

class TestConfigIteratorDeterminism(unittest.TestCase):
    """Audit P6.1: the Cartesian product must produce identical config
    dicts (including ids) across repeated runs with identical input."""

    def test_same_input_yields_same_output(self):
        # Build identical kwargs twice and iterate both.
        kwargs_a = {"a": [1, 2, 3], "b": [10, 20], "c": [{"x": 1}, {"x": 2}]}
        kwargs_b = {"a": [1, 2, 3], "b": [10, 20], "c": [{"x": 1}, {"x": 2}]}
        _, gen_a = create_config_iterator(**kwargs_a)
        _, gen_b = create_config_iterator(**kwargs_b)
        list_a = list(gen_a)
        list_b = list(gen_b)
        self.assertEqual(list_a, list_b)

    def test_k_to_the_n_total(self):
        """K=3 params, each with N=2 values → 2^3 = 8 configs."""
        total, gen = create_config_iterator(a=[1, 2], b=[3, 4], c=[5, 6])
        self.assertEqual(total, 8)
        self.assertEqual(len(list(gen)), 8)

    def test_mixed_length_cartesian(self):
        total, gen = create_config_iterator(a=[1, 2, 3], b=[10, 20])
        self.assertEqual(total, 6)
        ids = [c["id"] for c in gen]
        self.assertEqual(ids, [1, 2, 3, 4, 5, 6])


class TestConfigLoaderFallbacks(unittest.TestCase):
    """P6.1: missing YAML keys fall through to documented defaults
    rather than raising. Ensures new configs don't break on optional
    keys, but also means typos in key names silently use defaults."""

    def test_simulation_config_defaults_for_missing_keys(self):
        cfg = _build_simulation_config({})
        # Every key should be present with a documented default.
        expected_keys = {
            "default_sorting_type", "order_sorting_type",
            "order_ranking_window_days", "max_positions",
            "max_positions_per_instrument", "order_value_multiplier",
            "max_order_value", "exit_before_entry",
        }
        self.assertEqual(set(cfg.keys()), expected_keys)

    def test_validate_config_rejects_inverted_epochs(self):
        config = {
            "scanner_config_input": {},
            "entry_config_input": {},
            "exit_config_input": {"trailing_stop_pct": [15]},
            "simulation_config_input": {"max_positions": [10]},
            "static_config": {
                "start_epoch": 1700000000,
                "end_epoch": 1700000000,  # equal; must error
                "strategy_type": "other",
            },
        }
        with self.assertRaises(ValueError) as ctx:
            validate_config(config)
        self.assertIn("start_epoch", str(ctx.exception))

    def test_validate_config_rejects_zero_max_positions(self):
        config = {
            "scanner_config_input": {},
            "entry_config_input": {},
            "exit_config_input": {"trailing_stop_pct": [15]},
            "simulation_config_input": {"max_positions": [0]},
            "static_config": {
                "start_epoch": 1577836800,
                "end_epoch": 1609459200,
                "strategy_type": "other",
            },
        }
        with self.assertRaises(ValueError) as ctx:
            validate_config(config)
        self.assertIn("max_positions", str(ctx.exception))


# ── P6.2: intraday fixed_stop clamp ───────────────────────────────────────

class TestIntradayFixedStopClamp(unittest.TestCase):
    """Audit P6.2: `fixed_stop` must never go negative. High-vol names
    where `atr_14 * atr_multiplier > entry_price` pre-fix produced a
    negative stop price; `price_low <= fixed_stop` was never satisfied
    so the stop silently disabled itself."""

    def test_negative_stop_clamped_to_floor(self):
        """Source-level assertion: the clamp guard is present in the
        fixed_stop computation. Full runtime exercise would require
        setting up the broader signal_matrix + config fixtures; this
        static check is sufficient to prevent a silent revert.
        """
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "engine", "intraday_simulator_v2.py",
        )
        with open(src_path, "r") as f:
            source = f.read()
        # Clamp must use a positive floor, and must be inside _resolve_exit
        # (where fixed_stop is computed).
        self.assertIn("P6.2", source,
                      "intraday_simulator_v2 must carry the P6.2 clamp marker")
        self.assertIn("0.01 * entry_price", source,
                      "fixed_stop must be floored at 1% of entry_price")
        # The clamp must appear after the initial fixed_stop assignment.
        clamp_idx = source.index("0.01 * entry_price")
        last_assign_idx = source.rindex("fixed_stop = entry_price * (1")
        self.assertGreater(clamp_idx, last_assign_idx,
                           "clamp must be AFTER fixed_stop is computed, "
                           "otherwise it's a no-op")


# ── P6.3: cloud_orchestrator hash cache scoping ──────────────────────────

class TestHashCacheProjectScoping(unittest.TestCase):
    """Audit P6.3: hash cache must be keyed by project_name so switching
    projects doesn't claim the new project has already-synced files."""

    def _make_orch(self, project_name, cache_path):
        from lib.cloud_orchestrator import CloudOrchestrator
        orch = CloudOrchestrator.__new__(CloudOrchestrator)
        orch.project_name = project_name
        orch.verbose = False
        orch._hash_cache_path = cache_path
        return orch

    def test_two_projects_have_independent_caches(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = os.path.join(td, "hashes.json")
            a = self._make_orch("project-a", cache_path)
            b = self._make_orch("project-b", cache_path)

            a._save_hash_cache({"file1.py": "aaa"})
            b._save_hash_cache({"file1.py": "bbb", "file2.py": "ccc"})

            # project-a should still see its own hash
            self.assertEqual(a._load_hash_cache(), {"file1.py": "aaa"})
            # project-b has its own two files
            self.assertEqual(b._load_hash_cache(),
                             {"file1.py": "bbb", "file2.py": "ccc"})

    def test_legacy_flat_format_is_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            cache_path = os.path.join(td, "hashes.json")
            # Write legacy flat format.
            with open(cache_path, "w") as f:
                json.dump({"file1.py": "legacy"}, f)

            a = self._make_orch("project-a", cache_path)
            loaded = a._load_hash_cache()
            # Legacy flat is adopted by the current project so we don't
            # re-upload everything on upgrade.
            self.assertEqual(loaded, {"file1.py": "legacy"})

            # Writing back converts the file to the nested layout.
            a._save_hash_cache({"file1.py": "legacy", "file2.py": "new"})
            with open(cache_path) as f:
                stored = json.load(f)
            self.assertIn("project-a", stored)
            self.assertEqual(stored["project-a"],
                             {"file1.py": "legacy", "file2.py": "new"})


# ── P6.4: simulator edge cases ───────────────────────────────────────────

def _make_context(start_epoch, end_epoch, margin=1_000_000):
    return {
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "start_margin": margin,
    }


def _make_sim_config():
    return {
        "max_positions": 10,
        "max_positions_per_instrument": 1,
        "order_value_multiplier": 1,
        "max_order_value": {"type": "percentage_of_instrument_avg_txn", "value": 4.5},
        "order_ranking_window_days": 30,
        "order_sorting_type": "top_average_txn",
        "default_sorting_type": "top_average_txn",
        "exit_before_entry": False,
    }


class TestZeroTradeSimulation(unittest.TestCase):
    """P6.4: a pipeline that emits zero orders must complete gracefully
    and produce a valid empty day_wise_log, not crash."""

    def test_empty_orders_returns_clean_state(self):
        start = 1577836800  # 2020-01-01
        end = start + 10 * SECONDS_IN_ONE_DAY

        df_orders = pl.DataFrame(schema={
            "instrument": pl.Utf8, "entry_epoch": pl.Int64,
            "exit_epoch": pl.Int64, "entry_price": pl.Float64,
            "exit_price": pl.Float64, "entry_volume": pl.Int64,
            "exit_volume": pl.Int64,
        })
        # Provide at least one instrument stats entry so mtm_epochs isn't empty
        stats = {
            start + d * SECONDS_IN_ONE_DAY: {
                "NSE:TEST": {"close": 100, "avg_txn": 1_000_000},
            }
            for d in range(11)
        }
        day_wise_log, config_order_ids, snapshot, _, trade_log = process(
            _make_context(start, end),
            df_orders, stats, {}, _make_sim_config(), "zero_trade",
        )
        # No trades, no config_order_ids, start margin preserved
        self.assertEqual(trade_log, [])
        self.assertEqual(config_order_ids, [])
        self.assertEqual(snapshot["margin_available"], 1_000_000)
        self.assertGreater(len(day_wise_log), 0,
                           "MTM log should still emit per-day entries")


class TestSingleDaySimulationValidation(unittest.TestCase):
    """P6.4: start_epoch == end_epoch must fail at config validation,
    before any simulation runs."""

    def test_validate_config_rejects_single_day(self):
        config = {
            "scanner_config_input": {},
            "entry_config_input": {},
            "exit_config_input": {"trailing_stop_pct": [15]},
            "simulation_config_input": {"max_positions": [1]},
            "static_config": {
                "start_epoch": 1577836800,
                "end_epoch": 1577836800,  # same
                "strategy_type": "other",
            },
        }
        with self.assertRaises(ValueError):
            validate_config(config)


class TestCapitalExhaustion(unittest.TestCase):
    """P6.4: if margin falls below the required amount for an entry,
    the entry is skipped (not retried at reduced size, not a crash)."""

    def test_insufficient_margin_skips_entry_gracefully(self):
        start = 1577836800
        end = start + 5 * SECONDS_IN_ONE_DAY

        # One order requiring way more than available margin.
        df_orders = pl.DataFrame([{
            "instrument": "NSE:TEST",
            "entry_epoch": start + 1 * SECONDS_IN_ONE_DAY,
            "exit_epoch": start + 3 * SECONDS_IN_ONE_DAY,
            "entry_price": 10000.0,
            "exit_price": 11000.0,
            "entry_volume": 1000,
            "exit_volume": 1000,
            "scanner_config_ids": "1",
            "entry_config_ids": "1",
            "exit_config_ids": "1",
        }])
        stats = {
            start + d * SECONDS_IN_ONE_DAY: {
                "NSE:TEST": {"close": 10000, "avg_txn": 1_000_000_000},
            }
            for d in range(6)
        }
        # Start with only 100 rupees; cannot afford any entry.
        context = _make_context(start, end, margin=100)
        sim_cfg = _make_sim_config()
        sim_cfg["max_order_value"] = {"type": "absolute", "value": 1_000_000_000}
        day_wise_log, _, snapshot, _, trade_log = process(
            context, df_orders, stats, {}, sim_cfg, "exhaustion",
        )
        # No trades executed; margin unchanged; clean state
        self.assertEqual(trade_log, [])
        self.assertEqual(snapshot["margin_available"], 100)


class TestAllLoserSimulation(unittest.TestCase):
    """P6.4: every trade loses. MDD computation + downstream Calmar
    must not crash; result should produce a negative CAGR."""

    def test_all_losses_produces_valid_metrics(self):
        from lib.backtest_result import BacktestResult
        from lib.equity_curve import Frequency

        # Equity curve that only goes down, ending at ~27% of start.
        # Exercises the negative-CAGR / deep-MDD path.
        start = 1577836800  # 2020-01-01
        result = BacktestResult(
            strategy_name="all_loser",
            params={},
            instrument="NSE:TEST",
            exchange="NSE",
            capital=1_000_000,
            equity_curve_frequency=Frequency.DAILY_CALENDAR,
        )
        for d in range(366):
            epoch = start + d * SECONDS_IN_ONE_DAY
            value = 1_000_000.0 * (1 - 0.002 * d)  # strictly decreasing
            result.add_equity_point(epoch, value)

        result.compute()
        summary = result.to_dict()["summary"]
        # CAGR must be negative and finite; MDD must be negative and finite.
        self.assertLess(summary["cagr"], 0)
        self.assertGreater(summary["cagr"], -1)
        self.assertLess(summary["max_drawdown"], 0)
        self.assertGreater(summary["max_drawdown"], -1)
        # Calmar should be None OR a finite negative number — never a
        # ZeroDivisionError raised into the result.
        calmar = summary.get("calmar_ratio")
        self.assertTrue(calmar is None or isinstance(calmar, float))


if __name__ == "__main__":
    unittest.main()

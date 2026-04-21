"""Tests for SweepResult None-metric sorting and compact() flag."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.backtest_result import BacktestResult, SweepResult
from lib.equity_curve import Frequency


def _make_config(calmar_value, cagr_value=0.1, max_dd=-0.1):
    """Build a minimal BacktestResult with an injected summary dict.

    We bypass compute() by pre-populating _computed so we can unit-test
    the sorting logic without running a full simulation.
    """
    r = BacktestResult(
        "synthetic", {}, "NIFTYBEES", "NSE", 1_000_000,
        equity_curve_frequency=Frequency.DAILY_CALENDAR,
    )
    r._computed = {
        "version": "1.1", "type": "single",
        "strategy": r.strategy,
        "summary": {
            "cagr": cagr_value,
            "calmar_ratio": calmar_value,
            "max_drawdown": max_dd,
        },
        "equity_curve": [], "trades": [],
        "monthly_returns": {}, "yearly_returns": [],
        "costs": {"total_charges": 0, "total_slippage": 0,
                  "total_cost": 0, "cost_pct_of_capital": 0},
    }
    return r


class TestSweepSortingNoneHandling(unittest.TestCase):

    def _build_sweep_with_unscored(self):
        """Sweep: 4 scored configs + 1 unscored (MDD=0 → calmar=None)."""
        sweep = SweepResult("test", "NIFTYBEES", "NSE", 1_000_000)
        sweep.add_config({"id": "A"}, _make_config(calmar_value=0.2))
        sweep.add_config({"id": "B"}, _make_config(calmar_value=0.5))
        sweep.add_config({"id": "unscored"}, _make_config(calmar_value=None, max_dd=0.0))
        sweep.add_config({"id": "C"}, _make_config(calmar_value=0.8))
        sweep.add_config({"id": "D"}, _make_config(calmar_value=0.1))
        return sweep

    def test_unscored_is_not_ranked_worst(self):
        """Scored configs ranked first (desc), unscored appended after."""
        sweep = self._build_sweep_with_unscored()
        sorted_ = sweep._sorted("calmar_ratio")
        ids = [p["id"] for p, _ in sorted_]
        # Scored configs should appear in descending calmar order first
        scored_ids_expected = ["C", "B", "A", "D"]
        self.assertEqual(ids[: len(scored_ids_expected)], scored_ids_expected)
        # Unscored appears at the end — but distinguishable, not mixed with scored
        self.assertEqual(ids[-1], "unscored")

    def test_unscored_configs_accessor(self):
        sweep = self._build_sweep_with_unscored()
        unscored = sweep._unscored_configs("calmar_ratio")
        self.assertEqual(len(unscored), 1)
        self.assertEqual(unscored[0][0]["id"], "unscored")

    def test_all_scored_preserves_descending_order(self):
        sweep = SweepResult("test", "NIFTYBEES", "NSE", 1_000_000)
        sweep.add_config({"id": "A"}, _make_config(calmar_value=0.2))
        sweep.add_config({"id": "B"}, _make_config(calmar_value=0.9))
        sweep.add_config({"id": "C"}, _make_config(calmar_value=0.5))
        ids = [p["id"] for p, _ in sweep._sorted("calmar_ratio")]
        self.assertEqual(ids, ["B", "C", "A"])

    def test_all_unscored_keeps_insertion_order(self):
        sweep = SweepResult("test", "NIFTYBEES", "NSE", 1_000_000)
        sweep.add_config({"id": "X"}, _make_config(calmar_value=None))
        sweep.add_config({"id": "Y"}, _make_config(calmar_value=None))
        sweep.add_config({"id": "Z"}, _make_config(calmar_value=None))
        ids = [p["id"] for p, _ in sweep._sorted("calmar_ratio")]
        self.assertEqual(ids, ["X", "Y", "Z"])

    def test_sort_by_sharpe_ratio_arithmetic(self):
        """Dual-Sharpe (D1): sorting by the new arithmetic Sharpe key works."""
        sweep = SweepResult("test", "NIFTYBEES", "NSE", 1_000_000)
        for i, val in enumerate([0.3, 0.8, 0.5]):
            r = _make_config(calmar_value=0.1)
            r._computed["summary"]["sharpe_ratio_arithmetic"] = val
            sweep.add_config({"id": f"cfg{i}"}, r)
        sorted_ = sweep._sorted("sharpe_ratio_arithmetic")
        ids = [p["id"] for p, _ in sorted_]
        self.assertEqual(ids, ["cfg1", "cfg2", "cfg0"])


class TestCompactFlag(unittest.TestCase):
    """compact() marks the result as compacted for downstream detection."""

    def test_compact_sets_flag(self):
        r = _make_config(calmar_value=0.5)
        # _make_config pre-populates _computed; add non-empty equity_curve
        r._computed["equity_curve"] = [{"epoch": 0, "value": 100}]
        r._computed["trades"] = [{"entry_epoch": 0}]
        r.compact()
        d = r.to_dict()
        self.assertTrue(d.get("compacted"))
        self.assertEqual(d["equity_curve"], [])
        self.assertEqual(d["trades"], [])

    def test_non_compacted_result_has_no_flag(self):
        r = _make_config(calmar_value=0.5)
        d = r.to_dict()
        self.assertFalse(d.get("compacted", False))


if __name__ == "__main__":
    unittest.main()

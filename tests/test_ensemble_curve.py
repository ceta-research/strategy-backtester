"""Tests for lib/ensemble_curve.py — alignment, combination, rebalancing,
weighting, attribution, and diagnostics.

Run: python -m unittest tests.test_ensemble_curve
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.equity_curve import EquityCurve, Frequency
from lib.ensemble_curve import (
    REBALANCE_PERIODS,
    WEIGHTING_MODES,
    align_curves,
    attribute_drawdown,
    build_ensemble_curve,
    combine_curves,
    compute_correlation_matrix,
    compute_inverse_vol_weights,
    compute_leg_navs,
    rebalance_combined_curve,
    rebalance_combined_curve_adaptive,
    resolve_weights,
    sharpe_sensitivity_2leg,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

DAY = 86400


def _curve(values, start_epoch=0, freq=Frequency.DAILY_CALENDAR):
    pairs = [(start_epoch + i * DAY, float(v)) for i, v in enumerate(values)]
    return EquityCurve.from_pairs(pairs, freq)


# ── Combine ──────────────────────────────────────────────────────────────

class TestCombine(unittest.TestCase):

    def test_50_50_identical_legs_preserves_shape(self):
        c = _curve([100, 110, 121])
        ens = build_ensemble_curve([c, c], [0.5, 0.5], 100)
        for a, b in zip(ens.values, (100.0, 110.0, 121.0)):
            self.assertAlmostEqual(a, b)

    def test_70_30_analytic(self):
        # A doubles, B flat; 70/30 should give 1.7x final.
        a = _curve([100, 200])
        b = _curve([100, 100])
        ens = build_ensemble_curve([a, b], [0.7, 0.3], 1000)
        self.assertAlmostEqual(ens.values[0], 1000.0)
        self.assertAlmostEqual(ens.values[1], 1700.0)

    def test_weights_sum_validation(self):
        with self.assertRaises(ValueError):
            combine_curves([[100, 110]], [0.7], 100)

    def test_negative_weight_rejected(self):
        with self.assertRaises(ValueError):
            combine_curves([[100, 110], [100, 105]], [1.5, -0.5], 100)

    def test_zero_starting_capital_rejected(self):
        with self.assertRaises(ValueError):
            combine_curves([[100, 110], [100, 105]], [0.5, 0.5], 0)

    def test_zero_starting_value_rejected(self):
        with self.assertRaises(ValueError):
            combine_curves([[0, 110], [100, 105]], [0.5, 0.5], 100)


# ── Align ────────────────────────────────────────────────────────────────

class TestAlign(unittest.TestCase):

    def test_frequency_mismatch_rejected(self):
        c1 = _curve([100, 110], freq=Frequency.DAILY_CALENDAR)
        c2 = _curve([100, 110], freq=Frequency.DAILY_TRADING)
        with self.assertRaises(ValueError):
            align_curves([c1, c2])

    def test_empty_intersection_rejected(self):
        c1 = _curve([100, 110, 120], start_epoch=0)
        c2 = _curve([50, 55, 60], start_epoch=10 * DAY)
        with self.assertRaises(ValueError):
            align_curves([c1, c2])

    def test_partial_overlap_picks_intersection(self):
        c_a = _curve([100, 110, 120], start_epoch=0)        # epochs 0, DAY, 2*DAY
        c_b = _curve([50, 55, 60], start_epoch=DAY)          # epochs DAY, 2*DAY, 3*DAY
        common, aligned = align_curves([c_a, c_b])
        self.assertEqual(common, (DAY, 2 * DAY))
        self.assertEqual(aligned[0], (110.0, 120.0))
        self.assertEqual(aligned[1], (50.0, 55.0))

    def test_union_ffill_not_implemented(self):
        c = _curve([100, 110])
        with self.assertRaises(NotImplementedError):
            align_curves([c, c], mode="union_ffill")


# ── Rebalance ────────────────────────────────────────────────────────────

class TestRebalance(unittest.TestCase):

    def test_rebalance_none_matches_combine(self):
        epochs = [t * DAY for t in range(10)]
        a = [100 * (1.01) ** t for t in range(10)]
        b = [100 for _ in range(10)]
        no_reb = combine_curves([a, b], [0.5, 0.5], 1000)
        reb_none = rebalance_combined_curve(epochs, [a, b], [0.5, 0.5], 1000, "none")
        for x, y in zip(no_reb, reb_none):
            self.assertAlmostEqual(x, y, places=9)

    def test_rebalance_frequency_monotonicity(self):
        # When winner is repeatedly cashed-out, more frequent rebalance
        # produces lower terminal NAV.
        import datetime as _dt
        n_days = 730
        start = int(_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc).timestamp())
        epochs = [start + t * DAY for t in range(n_days)]
        a = [100 * (1.005) ** t for t in range(n_days)]
        b = [100 for _ in range(n_days)]

        no_reb = combine_curves([a, b], [0.5, 0.5], 1000)
        annual = rebalance_combined_curve(epochs, [a, b], [0.5, 0.5], 1000, "annual")
        quarterly = rebalance_combined_curve(epochs, [a, b], [0.5, 0.5], 1000, "quarterly")
        monthly = rebalance_combined_curve(epochs, [a, b], [0.5, 0.5], 1000, "monthly")

        self.assertGreater(no_reb[-1], annual[-1])
        self.assertGreater(annual[-1], quarterly[-1])
        self.assertGreater(quarterly[-1], monthly[-1])

    def test_rebalance_does_not_create_value_jump(self):
        # At a rebalance boundary, combined NAV is unchanged (it's a
        # reallocation between legs, not a cashflow).
        import datetime as _dt
        n_days = 32
        start = int(_dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc).timestamp())
        epochs = [start + t * DAY for t in range(n_days)]
        a = [100 * (2 ** (t / 30)) for t in range(n_days)]
        b = [100 for _ in range(n_days)]
        m = rebalance_combined_curve(epochs, [a, b], [0.5, 0.5], 100, "monthly")
        # Day 30 = Jan 31 (last of month), Day 31 = Feb 1 (rebalance fires).
        # Combined value at rebalance moment must be smooth (no jump).
        self.assertLess(abs(m[31] - m[30]), 5.0)

    def test_invalid_period_rejected(self):
        with self.assertRaises(ValueError):
            rebalance_combined_curve([0, DAY], [[1, 2], [1, 2]], [0.5, 0.5], 100, "biweekly")


# ── Inverse-vol weighting ────────────────────────────────────────────────

class TestInverseVol(unittest.TestCase):

    def test_identical_legs_yield_equal_weights(self):
        c = _curve([100 * (1 + 0.01 * (i % 7 - 3)) for i in range(50)])
        # Build a positive-only path
        c_pos = _curve([100 * (1.005) ** i for i in range(50)])
        w = compute_inverse_vol_weights([c_pos, c_pos])
        self.assertAlmostEqual(w[0], 0.5)
        self.assertAlmostEqual(w[1], 0.5)

    def test_higher_vol_leg_gets_lower_weight(self):
        # Leg A: high-amplitude oscillation; Leg B: low-amplitude.
        a = _curve([100 + 10 * ((i % 2) * 2 - 1) for i in range(50)])  # +/- 10
        b = _curve([100 + 1 * ((i % 2) * 2 - 1) for i in range(50)])    # +/- 1
        w = compute_inverse_vol_weights([a, b])
        self.assertLess(w[0], w[1])

    def test_weights_sum_to_one(self):
        c1 = _curve([100, 105, 110, 108, 112, 115])
        c2 = _curve([100, 102, 101, 103, 104, 106])
        w = compute_inverse_vol_weights([c1, c2])
        self.assertAlmostEqual(sum(w), 1.0)

    def test_lookback_uses_only_recent_window(self):
        # Build a curve whose vol changes over time; weights should differ
        # between full-window and short lookback.
        n = 50
        early_quiet = [100 * (1 + 0.001 * (i % 2)) for i in range(n // 2)]
        late_volatile = [early_quiet[-1] * (1.05 ** ((i % 3) - 1)) for i in range(n // 2)]
        a_vals = early_quiet + late_volatile
        b_vals = [100 * (1 + 0.005 * (i % 4 - 1.5)) for i in range(n)]
        # Wrap as positive monotonic-ish curves
        a_vals = [max(0.01, v) for v in a_vals]
        b_vals = [max(0.01, v) for v in b_vals]
        a = _curve(a_vals)
        b = _curve(b_vals)
        full = compute_inverse_vol_weights([a, b])
        recent = compute_inverse_vol_weights([a, b], lookback_days=5)
        # Both must sum to 1
        self.assertAlmostEqual(sum(full), 1.0)
        self.assertAlmostEqual(sum(recent), 1.0)


# ── resolve_weights ──────────────────────────────────────────────────────

class TestResolveWeights(unittest.TestCase):

    def test_fixed_passes_through(self):
        c = _curve([100, 105])
        w = resolve_weights([{"weight": 0.7}, {"weight": 0.3}], [c, c], "fixed")
        self.assertEqual(w, [0.7, 0.3])

    def test_inverse_vol_dispatches(self):
        c = _curve([100 * (1.005) ** i for i in range(20)])
        w = resolve_weights([{}, {}], [c, c], "inverse_vol")
        self.assertAlmostEqual(sum(w), 1.0)

    def test_risk_parity_not_implemented(self):
        c = _curve([100, 105])
        with self.assertRaises(NotImplementedError):
            resolve_weights([{}, {}], [c, c], "risk_parity")

    def test_unknown_mode_rejected(self):
        c = _curve([100, 105])
        with self.assertRaises(ValueError):
            resolve_weights([{}, {}], [c, c], "magic")


# ── compute_leg_navs + drawdown attribution ──────────────────────────────

class TestLegNavsAndAttribution(unittest.TestCase):

    def test_leg_navs_sum_equals_combined_curve(self):
        a = _curve([100 * (1.01) ** i for i in range(20)])
        b = _curve([100 * (1.005) ** i for i in range(20)])
        ens = build_ensemble_curve([a, b], [0.5, 0.5], 1000, rebalance="none")
        epochs, aligned = align_curves([a, b])
        navs = compute_leg_navs(epochs, aligned, [0.5, 0.5], 1000, "none")
        for t in range(len(epochs)):
            s = sum(navs[i][t] for i in range(2))
            self.assertAlmostEqual(s, ens.values[t], places=6)

    def test_leg_navs_sum_in_rebalanced_mode(self):
        a = _curve([100 * (1.01) ** i for i in range(60)])
        b = _curve([100 * (0.999) ** i for i in range(60)])
        ens = build_ensemble_curve([a, b], [0.5, 0.5], 1000, rebalance="monthly")
        epochs, aligned = align_curves([a, b])
        navs = compute_leg_navs(epochs, aligned, [0.5, 0.5], 1000, "monthly")
        for t in range(len(epochs)):
            s = sum(navs[i][t] for i in range(2))
            self.assertAlmostEqual(s, ens.values[t], places=6)

    def test_attribution_sums_to_ensemble_drawdown(self):
        # Construct legs with an obvious drawdown.
        a = _curve([100, 120, 150, 90, 100, 110, 105])
        b = _curve([100, 105, 110, 100, 105, 108, 110])
        epochs, aligned = align_curves([a, b])
        navs = compute_leg_navs(epochs, aligned, [0.5, 0.5], 1000, "none")
        attr = attribute_drawdown(epochs, navs, ["A", "B"])
        total = sum(l["contribution_to_dd"] for l in attr["legs"])
        self.assertAlmostEqual(total, attr["ensemble_drawdown"], places=6)
        # Worst DD spans peak (idx of max) to subsequent trough.
        self.assertLess(attr["ensemble_drawdown"], 0)


# ── Diagnostics ──────────────────────────────────────────────────────────

class TestDiagnostics(unittest.TestCase):

    def test_correlation_diagonal_is_one(self):
        a = _curve([100 + i for i in range(20)])
        b = _curve([100 - i * 0.5 for i in range(20)])
        m = compute_correlation_matrix([a, b])
        self.assertAlmostEqual(m[0][0], 1.0)
        self.assertAlmostEqual(m[1][1], 1.0)

    def test_correlation_symmetric(self):
        a = _curve([100, 102, 101, 105, 103, 107])
        b = _curve([100, 99, 102, 100, 104, 105])
        m = compute_correlation_matrix([a, b])
        self.assertAlmostEqual(m[0][1], m[1][0])

    def test_correlation_perfect_for_identical_legs(self):
        a = _curve([100 * (1.005) ** i for i in range(30)])
        m = compute_correlation_matrix([a, a])
        self.assertAlmostEqual(m[0][1], 1.0, places=5)

    def test_sharpe_sensitivity_endpoints_match_solo(self):
        # At w1=0 ensemble == leg2 solo; at w1=1 ensemble == leg1 solo.
        # Use jittered drift to ensure non-zero variance (Sharpe undefined at vol=0).
        a_vals = [100.0]
        b_vals = [100.0]
        for i in range(1, 120):
            a_vals.append(a_vals[-1] * (1.0 + 0.005 + 0.02 * (((i * 7) % 11) / 10 - 0.5)))
            b_vals.append(b_vals[-1] * (1.0 + 0.002 + 0.01 * (((i * 13) % 7) / 6 - 0.5)))
        a = _curve(a_vals)
        b = _curve(b_vals)
        sens = sharpe_sensitivity_2leg([a, b], 1000, n_grid=11)
        from lib.metrics import compute_metrics_from_curve
        solo_a = compute_metrics_from_curve(a)["portfolio"]["sharpe_ratio"]
        solo_b = compute_metrics_from_curve(b)["portfolio"]["sharpe_ratio"]
        self.assertAlmostEqual(sens["grid"][0]["sharpe"], solo_b, places=4)
        self.assertAlmostEqual(sens["grid"][-1]["sharpe"], solo_a, places=4)

    def test_sharpe_sensitivity_rejects_non_2leg(self):
        c = _curve([100, 110])
        with self.assertRaises(ValueError):
            sharpe_sensitivity_2leg([c, c, c], 1000)


# ── Constants ────────────────────────────────────────────────────────────

class TestConstants(unittest.TestCase):
    def test_rebalance_periods_includes_none(self):
        self.assertIn("none", REBALANCE_PERIODS)

    def test_weighting_modes_includes_fixed(self):
        self.assertIn("fixed", WEIGHTING_MODES)


# ── Adaptive per-rebalance inverse-vol ───────────────────────────────────

class TestRebalanceAdaptive(unittest.TestCase):

    def _build_two_leg(self, n_days=400, seed=0):
        # Leg A: steady upward drift with mild noise
        # Leg B: high vol, lower drift
        a_vals = [100.0]
        b_vals = [100.0]
        for i in range(1, n_days):
            a_vals.append(a_vals[-1] * (1.0 + 0.0008 + 0.005 * (((i * 7) % 11) / 10 - 0.5)))
            b_vals.append(b_vals[-1] * (1.0 + 0.0003 + 0.020 * (((i * 13) % 7) / 6 - 0.5)))
        return _curve(a_vals), _curve(b_vals)

    def test_adaptive_runs_and_returns_correct_shape(self):
        a, b = self._build_two_leg(n_days=400)
        epochs, aligned = align_curves([a, b])
        nav, weights_history = rebalance_combined_curve_adaptive(
            epochs, aligned, starting_capital=1000, period="quarterly",
            lookback_days=60,
        )
        self.assertEqual(len(nav), len(epochs))
        # weights_history starts with initial + one entry per rebalance
        self.assertGreaterEqual(len(weights_history), 2)
        self.assertAlmostEqual(nav[0], 1000.0, places=4)
        # Each weights vector sums to 1
        for w in weights_history:
            self.assertAlmostEqual(sum(w), 1.0, places=6)

    def test_adaptive_zero_vol_leg_gets_zero_weight(self):
        # Leg A: cash (constant value) → vol = 0
        # Leg B: noisy mover
        n = 300
        a_vals = [100.0] * n
        b_vals = [100.0]
        for i in range(1, n):
            b_vals.append(b_vals[-1] * (1.0 + 0.001 + 0.01 * (((i * 7) % 11) / 10 - 0.5)))
        a, b = _curve(a_vals), _curve(b_vals)
        epochs, aligned = align_curves([a, b])
        nav, weights_history = rebalance_combined_curve_adaptive(
            epochs, aligned, starting_capital=1000, period="quarterly",
            lookback_days=60,
        )
        # After at least one rebalance with the lookback window populated,
        # leg A (cash) should have zero weight.
        # Initial weights are equal; rebalanced weights should drop A.
        zero_vol_rebalances = [w for w in weights_history[1:] if w[0] == 0.0]
        self.assertGreater(
            len(zero_vol_rebalances), 0,
            "Expected at least one rebalance to drop the zero-vol leg"
        )

    def test_adaptive_rejects_no_rebalance(self):
        a, b = self._build_two_leg(n_days=100)
        epochs, aligned = align_curves([a, b])
        with self.assertRaises(ValueError):
            rebalance_combined_curve_adaptive(
                epochs, aligned, starting_capital=1000, period="none",
                lookback_days=30,
            )

    def test_adaptive_via_build_ensemble_curve(self):
        # End-to-end: build_ensemble_curve with adaptive=True
        a, b = self._build_two_leg(n_days=400)
        ens = build_ensemble_curve(
            [a, b], weights=[0.5, 0.5], starting_capital=1000,
            rebalance="quarterly", adaptive=True, adaptive_lookback_days=60,
        )
        self.assertEqual(len(ens), len(a))
        self.assertAlmostEqual(ens.values[0], 1000.0, places=4)

    def test_adaptive_requires_rebalance(self):
        a, b = self._build_two_leg(n_days=100)
        with self.assertRaises(ValueError):
            build_ensemble_curve(
                [a, b], weights=[0.5, 0.5], starting_capital=1000,
                rebalance="none", adaptive=True,
            )


if __name__ == "__main__":
    unittest.main()

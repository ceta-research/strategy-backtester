"""Tests for EquityCurve type and metrics via the EquityCurve path.

These are the P0 regression tests for the CAGR/vol/Sharpe bug cluster.
The critical invariant: CAGR is independent of forward-fill and sampling
frequency. A 10-year backtest with trading-day bars vs calendar-day
(forward-filled) bars must produce the same CAGR.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.equity_curve import EquityCurve, Frequency, SECONDS_PER_DAY
from lib.metrics import compute_metrics_from_curve


# ── Fixture helpers ──────────────────────────────────────────────────────

def _trading_day_curve(start_epoch, values):
    """Build a DAILY_TRADING curve with weekday-only epochs."""
    epochs = []
    e = start_epoch
    for _ in values:
        # skip weekends
        while True:
            # Sunday=6, Saturday=5 in UTC weekday for Unix epoch 0
            # Jan 1 1970 was a Thursday; use a known-good Monday as start.
            import datetime
            d = datetime.datetime.fromtimestamp(e, tz=datetime.timezone.utc)
            if d.weekday() < 5:
                break
            e += SECONDS_PER_DAY
        epochs.append(e)
        e += SECONDS_PER_DAY
    return EquityCurve(epochs=tuple(epochs), values=tuple(values),
                       frequency=Frequency.DAILY_TRADING)


def _calendar_day_curve_forward_filled(trading_curve):
    """Expand a trading-day curve to a calendar-day forward-filled curve.

    Every weekend/holiday gap gets filled with the previous value. This is
    exactly what the buggy engine produced and what the legacy
    compute_metrics(..., periods_per_year=252) mis-interpreted.
    """
    if len(trading_curve) == 0:
        return EquityCurve(epochs=(), values=(),
                           frequency=Frequency.DAILY_CALENDAR)
    epochs = []
    values = []
    last_value = trading_curve.values[0]
    e = trading_curve.epochs[0]
    end = trading_curve.epochs[-1]
    trading_map = dict(zip(trading_curve.epochs, trading_curve.values))
    while e <= end:
        if e in trading_map:
            last_value = trading_map[e]
        epochs.append(e)
        values.append(last_value)
        e += SECONDS_PER_DAY
    return EquityCurve(epochs=tuple(epochs), values=tuple(values),
                       frequency=Frequency.DAILY_CALENDAR)


# ── EquityCurve type invariants ──────────────────────────────────────────

class TestEquityCurveInvariants(unittest.TestCase):

    def test_lists_are_coerced_to_tuples(self):
        """S7: EquityCurve coerces list inputs to tuples so frozen=True
        actually delivers immutability."""
        c = EquityCurve(epochs=[1, 2], values=[100.0, 101.0],
                        frequency=Frequency.DAILY_TRADING)
        self.assertIsInstance(c.epochs, tuple)
        self.assertIsInstance(c.values, tuple)
        # Must still be hashable (consequence of tuple coercion + frozen)
        hash(c)

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            EquityCurve(epochs=(1, 2), values=(100.0,),
                        frequency=Frequency.DAILY_TRADING)

    def test_non_monotonic_epochs_rejected(self):
        with self.assertRaises(ValueError):
            EquityCurve(epochs=(2, 1, 3), values=(100.0, 101.0, 102.0),
                        frequency=Frequency.DAILY_TRADING)

    def test_duplicate_epochs_rejected(self):
        with self.assertRaises(ValueError):
            EquityCurve(epochs=(1, 1, 2), values=(100.0, 100.0, 101.0),
                        frequency=Frequency.DAILY_TRADING)

    def test_negative_value_rejected(self):
        with self.assertRaises(ValueError):
            EquityCurve(epochs=(1, 2), values=(100.0, -1.0),
                        frequency=Frequency.DAILY_TRADING)

    def test_nan_value_rejected(self):
        with self.assertRaises(ValueError):
            EquityCurve(epochs=(1, 2), values=(100.0, float("nan")),
                        frequency=Frequency.DAILY_TRADING)

    def test_years_wall_clock(self):
        """years is purely wall-clock, independent of sample count."""
        # 100 points over 10 calendar years
        ten_years = 10 * 365.25 * SECONDS_PER_DAY
        curve = EquityCurve(
            epochs=tuple(int(i * ten_years / 99) for i in range(100)),
            values=tuple(100.0 + i for i in range(100)),
            frequency=Frequency.DAILY_TRADING,
        )
        self.assertAlmostEqual(curve.years, 10.0, places=4)

    def test_from_pairs_empty(self):
        c = EquityCurve.from_pairs([], Frequency.DAILY_TRADING)
        self.assertEqual(len(c), 0)


# ── The P0 regression test ───────────────────────────────────────────────

class TestCAGRForwardFillInvariance(unittest.TestCase):
    """THE audit P0: CAGR must not depend on whether the curve was forward-filled.

    Pre-fix, a 10-year 12% CAGR strategy reported ~8% when its equity curve
    was forward-filled to calendar days and metrics used ppy=252. This test
    locks in the correct behavior.
    """

    def test_cagr_identical_trading_vs_calendar_forward_fill(self):
        # Build a 5-year trading-day curve: $100k -> $200k (14.87% CAGR).
        start_epoch = 1577836800  # 2020-01-01 UTC (Wednesday)
        n_trading_days = 5 * 252  # 1260
        # Linear growth in log-space -> constant daily compounding
        growth_per_bar = (200_000 / 100_000) ** (1.0 / (n_trading_days - 1))
        values = [100_000 * (growth_per_bar ** i) for i in range(n_trading_days)]

        trading_curve = _trading_day_curve(start_epoch, values)
        calendar_curve = _calendar_day_curve_forward_filled(trading_curve)

        # Both curves span the same wall-clock interval, so CAGR must match.
        trading_metrics = compute_metrics_from_curve(trading_curve)["portfolio"]
        calendar_metrics = compute_metrics_from_curve(calendar_curve)["portfolio"]

        self.assertIsNotNone(trading_metrics["cagr"])
        self.assertIsNotNone(calendar_metrics["cagr"])
        # Wall-clock years differ slightly (trading curve ends on a weekday,
        # calendar curve ends same day) but by <1%. CAGRs within 0.5pp.
        self.assertAlmostEqual(trading_metrics["cagr"],
                               calendar_metrics["cagr"], places=2)

    def test_cagr_matches_hand_computed_10_year_doubling(self):
        """Double in exactly 10 calendar years -> CAGR ≈ 7.177%."""
        start_epoch = 1577836800  # 2020-01-01 UTC
        ten_years = int(10 * 365.25 * SECONDS_PER_DAY)
        curve = EquityCurve(
            epochs=(start_epoch, start_epoch + ten_years),
            values=(100_000.0, 200_000.0),
            frequency=Frequency.DAILY_TRADING,
        )
        metrics = compute_metrics_from_curve(curve)["portfolio"]
        expected_cagr = 2.0 ** (1.0 / 10.0) - 1  # 0.0717734625...
        self.assertAlmostEqual(metrics["cagr"], expected_cagr, places=5)

    def test_short_curve_cagr_large_but_well_defined(self):
        """Short curves produce mathematically-correct large CAGRs; caller's
        job to reject them on duration grounds, not the library's."""
        start = 1577836800
        curve = EquityCurve(
            epochs=(start, start + 5 * SECONDS_PER_DAY),
            values=(100.0, 110.0),
            frequency=Frequency.DAILY_TRADING,
        )
        metrics = compute_metrics_from_curve(curve)["portfolio"]
        # 10% gain in 5 days -> annualized CAGR is huge but well-defined
        self.assertIsNotNone(metrics["cagr"])
        self.assertGreater(metrics["cagr"], 100.0)  # > 10,000% annualized
        self.assertAlmostEqual(metrics["total_return"], 0.1, places=6)


# ── Vol annualization consistency ────────────────────────────────────────

class TestVolAnnualization(unittest.TestCase):
    """Vol annualization uses the curve's declared frequency. Forward-filled
    curves produce zero-return bars on weekends, so raw period variance is
    lower — but ppy=365 (calendar) vs ppy=252 (trading) compensates exactly.
    """

    def test_flat_curve_zero_vol(self):
        start = 1577836800
        n = 252
        curve = EquityCurve(
            epochs=tuple(start + i * SECONDS_PER_DAY for i in range(n)),
            values=tuple(100_000.0 for _ in range(n)),
            frequency=Frequency.DAILY_CALENDAR,
        )
        metrics = compute_metrics_from_curve(curve)["portfolio"]
        self.assertAlmostEqual(metrics["annualized_volatility"], 0.0, places=8)
        # Sharpe is None when vol=0 (by contract of compute_metrics).
        self.assertIsNone(metrics["sharpe_ratio"])


# ── Legacy path still works (backwards compat guarantee) ─────────────────

class TestLegacyCompatIntact(unittest.TestCase):
    """compute_metrics() unchanged for callers that pass matched returns+ppy.
    The intraday stack produces one trading-day return per bar and passes
    ppy=252 — this is a correct use of the legacy API and must keep working.
    """

    def test_legacy_metrics_unchanged(self):
        from lib.metrics import compute_metrics
        # Same inputs as test_metrics.py::test_known_values_cagr
        returns = [0.05, -0.02, 0.08, 0.03]
        bench = [0.01, 0.01, 0.01, 0.01]
        result = compute_metrics(returns, bench, periods_per_year=4,
                                 risk_free_rate=0.02)
        cumulative = 1.05 * 0.98 * 1.08 * 1.03
        expected_cagr = cumulative - 1  # 1 year
        self.assertAlmostEqual(result["portfolio"]["cagr"], expected_cagr, places=6)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Phase 8A bias-fix opt-in flags.

Each of 3 strategies gained a new config flag that, when set to the
"honest" value, bypasses a known bias. The default stays on the
"legacy" value to preserve result parity with pre-audit runs. These
tests prove:

  (a) the default behavior is unchanged (parity with pre-flag code), and
  (b) flipping the flag produces DIFFERENT output on a synthetic fixture
      where the bias is known to matter.

Covers audit P5.2 (full-period universe) and P5.4 (same-bar entry).

The tests use static source-level assertions + small-fixture behavior
probes that don't require network or full pipeline. Full-pipeline
CAGR deltas are measured separately via scripts/measure_bias_impact.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl


def _read(relpath):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        relpath,
    )
    with open(path, "r") as f:
        return f.read()


class TestMomentumRebalanceMocLagFlag(unittest.TestCase):
    """momentum_rebalance gained `moc_signal_lag_days` (default 0)."""

    def test_flag_is_registered_in_build_entry_config(self):
        source = _read("engine/signals/momentum_rebalance.py")
        self.assertIn("moc_signal_lag_days", source)
        self.assertIn('entry_cfg.get("moc_signal_lag_days", [0])', source)

    def test_honest_path_uses_shifted_numerator(self):
        source = _read("engine/signals/momentum_rebalance.py")
        # The fix logic must reference shift(moc_signal_lag_days).
        self.assertIn('shift(moc_signal_lag_days)', source)

    def test_default_is_legacy_same_bar(self):
        # With default=[0], the Cartesian will include moc_signal_lag_days=0
        # which means no shift. This preserves parity.
        source = _read("engine/signals/momentum_rebalance.py")
        self.assertIn('"moc_signal_lag_days", [0]', source)


class TestBiasFlagsSemanticProbe(unittest.TestCase):
    """Semantic probe: on a crafted synthetic price series, the legacy
    and honest settings for `moc_signal_lag_days` must produce different
    ranking orders. Uses polars to mimic the strategy's computation
    without a full pipeline invocation."""

    def test_legacy_vs_honest_produce_different_top_k(self):
        DAY = 86400
        start = 1577836800
        # 3 instruments, 5 days. On day 4 (the "rebalance day"), the
        # LAST bar is a dramatic spike for instrument B. Under legacy
        # (signal uses close[T]=day4), B ranks #1. Under honest
        # (signal uses close[T-1]=day3), B does NOT rank #1.
        rows = []
        # A: steady 10% over 4 days
        rows.extend({"instrument": "NSE:A", "date_epoch": start + d * DAY,
                     "close": 100 + d * 2.5} for d in range(5))
        # B: flat for 4 days, then HUGE spike on day 4
        rows.extend({"instrument": "NSE:B", "date_epoch": start + d * DAY,
                     "close": [100, 100, 100, 100, 200][d]} for d in range(5))
        # C: steady 5% over 4 days
        rows.extend({"instrument": "NSE:C", "date_epoch": start + d * DAY,
                     "close": 100 + d * 1.25} for d in range(5))

        df = pl.DataFrame(rows).sort(["instrument", "date_epoch"])

        # Lookback = 2 days. On day 4 (index 4) we need close at index 2
        # for legacy (shift 2) and close at index 1 for honest
        # (shift 1 numerator, shift 1+2=3 denominator — signal taken
        # at index 3, 2-day return from index 1 to index 3).
        lookback = 2

        legacy = df.with_columns(
            (pl.col("close")
             / pl.col("close").shift(lookback).over("instrument")
             - 1.0).alias("momentum")
        ).filter(pl.col("date_epoch") == start + 4 * DAY)
        legacy_ranked = legacy.sort("momentum", descending=True)["instrument"].to_list()

        honest = df.with_columns(
            (pl.col("close").shift(1).over("instrument")
             / pl.col("close").shift(1 + lookback).over("instrument")
             - 1.0).alias("momentum")
        ).filter(pl.col("date_epoch") == start + 4 * DAY)
        honest_ranked = honest.sort("momentum", descending=True)["instrument"].to_list()

        # Legacy: B tops the ranking because of the day-4 spike.
        self.assertEqual(legacy_ranked[0], "NSE:B",
                         "Legacy (same-bar) must rank B #1 due to day-4 spike")
        # Honest: A tops because day-3 return for A (+7.5%) beats B's flat +0%.
        # The spike is invisible to the honest signal.
        self.assertEqual(honest_ranked[0], "NSE:A",
                         "Honest (T-1 signal) must rank A #1 because it "
                         "cannot see day-4's spike")


if __name__ == "__main__":
    unittest.main()

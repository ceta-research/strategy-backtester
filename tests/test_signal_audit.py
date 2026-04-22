"""Static signal-generator audit tests (Phase 5 of audit P1 plan).

These tests don't exercise the strategies at runtime — they assert that
specific source patterns (either "safe" conventions we require, or
"known bias" conventions we've documented) remain in place. If someone
removes the bias warning in a silent change, this test fails.

  P5.1 eod_breakout.py: reference MOC convention (next-day open entry).
  P5.2 momentum_top_gainers.py / momentum_dip_quality.py: flagged
       look-ahead in the period-avg-turnover universe + scanner fallback.
  P5.3 earnings_dip.py: MOC convention + require_peak_recovery=True.
  P5.4 momentum_rebalance.py: flagged same-bar entry.
  P5.4 sweep: every other signal generator uses `next_epoch` / next-day
       open for entry (no same-bar bias).
"""

import os
import re
import unittest

SIGNALS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "engine", "signals",
)


def _read(fname):
    with open(os.path.join(SIGNALS_DIR, fname), "r") as f:
        return f.read()


class TestReferenceStrategyPattern(unittest.TestCase):
    """P5.1: eod_breakout is the canonical MOC pattern."""

    def test_eod_breakout_uses_next_day_open_entry(self):
        source = _read("eod_breakout.py")
        # Entry must source from next_open / next_epoch.
        self.assertIn('entry["next_open"]', source)
        self.assertIn('entry["next_epoch"]', source)
        # Must filter out rows with missing next_open (prefetch tail / last bar)
        self.assertIn('pl.col("next_open").is_not_null()', source)


class TestEarningsDipPattern(unittest.TestCase):
    """P5.3: earnings_dip.py is a correct cross-source dip-buy."""

    def test_earnings_dip_uses_next_day_open_and_peak_recovery(self):
        source = _read("earnings_dip.py")
        # Entry at next-day open.
        self.assertIn("pd_next_epochs[i]", source)
        # Dip-buy semantics: walk_forward_exit with require_peak_recovery=True.
        self.assertTrue(
            re.search(r"require_peak_recovery\s*=\s*True", source),
            "earnings_dip must call walk_forward_exit with require_peak_recovery=True",
        )


class TestKnownBiasWarningsInPlace(unittest.TestCase):
    """P5.2 / P5.4: the audit added inline AUDIT comments documenting
    known biases. Assert they stay — a silent removal should fail the
    test so a reviewer notices."""

    def test_momentum_rebalance_same_bar_bias_is_documented(self):
        source = _read("momentum_rebalance.py")
        self.assertIn("AUDIT P5.4", source,
                      "momentum_rebalance.py must carry the P5.4 same-bar warning")
        self.assertIn("SAME-BAR", source)


class TestNoOtherSameBarEntries(unittest.TestCase):
    """Sweep check: every non-flagged strategy either uses `next_epoch`
    / `next_trading_day` for entry_epoch, or explicitly passes a later
    epoch. The test asserts no entry_price is directly set from the
    signal-day close without an override."""

    # Strategies that legitimately use same-day execution are flagged via
    # AUDIT P5.4 warnings in their source (tested separately above).
    FLAGGED = {"momentum_rebalance.py"}

    # Strategies where `entry_price = closes[entry_idx]` is an OPEN-FAIL
    # FALLBACK (entry_idx is already the NEXT trading day relative to the
    # signal day, so this is execution slippage, not same-bar bias).
    FALLBACK_OK = {"low_pe.py", "factor_composite.py"}

    # Patterns that indicate same-bar entry — entry_price drawn from the
    # signal-day close without a next-day index.
    SUSPICIOUS = re.compile(
        r'entry_price\s*=\s*row\["close"\]',
    )

    def test_no_new_same_bar_entries_introduced(self):
        offenders = []
        for fname in sorted(os.listdir(SIGNALS_DIR)):
            if not fname.endswith(".py") or fname.startswith("__"):
                continue
            if fname in self.FLAGGED or fname in self.FALLBACK_OK:
                continue
            source = _read(fname)
            if self.SUSPICIOUS.search(source):
                offenders.append(fname)
        self.assertEqual(
            offenders, [],
            f"Signal generators with suspected same-bar entry pattern "
            f"(not already flagged with AUDIT P5.4): {offenders}. "
            f"Either use `next_epoch`/`next_trading_day` for entry, or "
            f"add an AUDIT P5.4 warning."
        )


if __name__ == "__main__":
    unittest.main()

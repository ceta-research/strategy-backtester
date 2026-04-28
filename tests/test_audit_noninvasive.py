"""Regression test for Phase 2 audit hooks (inspection drill 2026-04-28).

The audit hooks added in Phase 2b (eod_breakout) and Phase 2c (eod_technical
legacy path) are observation-only. With ``audit_mode=False`` the code path
must be byte-identical to the pinned baseline, and with ``audit_mode=True``
the trades + equity_curve must still match (collector populates extra data,
but does not affect the simulated outcome).

Two test layers:

1. **Static guards (always run, fast).** Source-level asserts that every
   hook body is gated on ``audit_mode and audit_collector is not None``
   (or similar) so a future edit can't silently un-gate a hook. Mirrors
   the convention in tests/test_signal_audit.py.

2. **Runtime regression (opt-in, slow).** Runs both champion configs end
   to end with ``audit_mode=False`` and ``audit_mode=True`` and diffs the
   summary/trades/equity_curve against
   ``results/<strategy>/champion_pre_audit_baseline.json``.

   Slow tests are gated by env var ``STRATEGY_BACKTESTER_AUDIT_REGRESSION=1``
   so the standard ``unittest discover tests`` run stays fast. They also
   require the baseline JSONs to exist on disk and a working data provider
   for the strategy's config (champion uses ``nse_charting`` — needs CR).
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

EOD_BREAKOUT_PATH = os.path.join(REPO_ROOT, "engine", "signals", "eod_breakout.py")
SCANNER_PATH = os.path.join(REPO_ROOT, "engine", "scanner.py")
ORDER_GEN_PATH = os.path.join(REPO_ROOT, "engine", "order_generator.py")

EOD_BREAKOUT_BASELINE = os.path.join(
    REPO_ROOT, "results", "eod_breakout", "champion_pre_audit_baseline.json"
)
EOD_TECHNICAL_BASELINE = os.path.join(
    REPO_ROOT, "results", "eod_technical", "champion_pre_audit_baseline.json"
)
EOD_BREAKOUT_CHAMPION = os.path.join(
    REPO_ROOT, "strategies", "eod_breakout", "config_champion.yaml"
)
EOD_TECHNICAL_CHAMPION = os.path.join(
    REPO_ROOT, "strategies", "eod_technical", "config_champion.yaml"
)


def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Static guards — always run.
# ---------------------------------------------------------------------------
class TestAuditHooksAreGated(unittest.TestCase):
    """Every audit emission must be wrapped in an audit_mode guard.

    Each hook's emission site (the line that mutates the audit collector)
    must be preceded by a check on ``audit_mode``. The check pattern can
    vary slightly (``self.audit_mode``, plain ``audit_mode``), but the
    tests below assert the substantive guard exists in source.
    """

    def test_eod_breakout_hooks_guarded(self):
        src = _read(EOD_BREAKOUT_PATH)
        # Setup line (pulls audit_mode from context).
        self.assertIn("context.get(\"audit_mode\", False)", src)
        # All four hook bodies guarded by `audit_mode and audit_collector is not None`.
        guards = re.findall(
            r"if audit_mode and audit_collector is not None:", src
        )
        self.assertGreaterEqual(
            len(guards), 3,
            "Expected ≥3 hook-body guards in eod_breakout.py "
            "(HOOK 1 + HOOK 2 + HOOK 3); HOOK 3a is signature-only.",
        )
        # _walk_forward_tsl must return three values (epoch, price, reason).
        self.assertRegex(
            src, r"return decision\.exit_epoch, decision\.exit_price, \"anomalous_drop\"",
            "_walk_forward_tsl must return reason on anomalous_drop branch",
        )
        self.assertRegex(
            src, r"\"trailing_stop\"",
            "_walk_forward_tsl must label trailing-stop exits",
        )
        self.assertRegex(
            src, r"\"regime_flip\"",
            "_walk_forward_tsl must label regime-flip exits",
        )

    def test_scanner_hook_guarded(self):
        src = _read(SCANNER_PATH)
        self.assertIn("context.get(\"audit_mode\", False)", src)
        self.assertIn("if audit_mode and audit_collector is not None:", src)
        # HOOK B must emit the 4 expected aggregate fields.
        for field in ("price_rejects", "avg_txn_rejects",
                       "n_day_gain_rejects", "pass_count"):
            self.assertIn(field, src,
                          f"scanner.py HOOK B must emit `{field}` field")
        self.assertIn("scanner_reject_summaries", src)

    def test_order_generator_hooks_guarded(self):
        src = _read(ORDER_GEN_PATH)
        # Constructor takes audit args.
        self.assertRegex(
            src,
            r"def __init__\(self, df_tick_data: pl\.DataFrame, "
            r"audit_mode: bool = False,\s+audit_collector: dict = None\):",
            "OrderGenerationUtil must accept audit_mode + audit_collector",
        )
        self.assertIn("self.audit_mode = bool(audit_mode)", src)
        # HOOK C clause cols guarded.
        self.assertIn("if self.audit_mode:", src)
        # HOOK D + F bodies guarded.
        guards = re.findall(
            r"if (?:self\.)?audit_mode and (?:self\.)?audit_collector is not None:",
            src,
        )
        self.assertGreaterEqual(
            len(guards), 2,
            "order_generator.py must have ≥2 collector-emission guards "
            "(HOOK D in update_config_order_map, HOOK F in process)",
        )
        # HOOK F populates trade_log_audits with at-entry context.
        self.assertIn("trade_log_audits", src)
        self.assertIn("entry_close_signal", src)
        self.assertIn("entry_n_day_high", src)


class TestAuditCollectorContract(unittest.TestCase):
    """The collector keys form a small, stable contract.

    These keys are documented in the pt7/pt8 handover docs and consumed by
    the Phase 2e runner + Phase 3 inspection queries. Renaming silently
    breaks downstream — fail loud if a key disappears.
    """

    EXPECTED_EOD_B = {"scanner_snapshots", "entry_audits", "trade_log_audits"}
    EXPECTED_EOD_T = {
        "scanner_reject_summaries", "entry_audits", "trade_log_audits",
    }

    def test_eod_breakout_collector_keys(self):
        src = _read(EOD_BREAKOUT_PATH)
        for key in self.EXPECTED_EOD_B:
            self.assertIn(
                f'"{key}"', src,
                f"eod_breakout.py must emit collector key `{key}`",
            )

    def test_eod_technical_collector_keys(self):
        scanner_src = _read(SCANNER_PATH)
        og_src = _read(ORDER_GEN_PATH)
        joined = scanner_src + og_src
        for key in self.EXPECTED_EOD_T:
            self.assertIn(
                f'"{key}"', joined,
                f"eod_technical legacy path must emit collector key `{key}`",
            )


# ---------------------------------------------------------------------------
# Runtime regression — opt-in (slow).
# ---------------------------------------------------------------------------
SLOW_REGRESSION_ENABLED = (
    os.environ.get("STRATEGY_BACKTESTER_AUDIT_REGRESSION") == "1"
)


def _diff_summary_trades_equity(baseline_json: str, candidate_json: str) -> dict:
    """Return dict of section -> bool(identical) for the standard sections."""
    a = json.load(open(baseline_json))
    b = json.load(open(candidate_json))
    da = a["detailed"][0]
    db = b["detailed"][0]
    return {
        section: da.get(section) == db.get(section)
        for section in (
            "summary", "trades", "equity_curve",
            "monthly_returns", "yearly_returns", "costs",
        )
    }


@unittest.skipUnless(
    SLOW_REGRESSION_ENABLED,
    "Slow audit regression. Enable with STRATEGY_BACKTESTER_AUDIT_REGRESSION=1.",
)
class TestAuditModeOff(unittest.TestCase):
    """audit_mode=False on hooked code must reproduce the pinned baseline."""

    def _run_and_diff(self, config_path: str, baseline_path: str,
                       output_path: str):
        from engine.pipeline import run_pipeline
        sweep = run_pipeline(config_path)
        sweep.save(output_path)
        diff = _diff_summary_trades_equity(baseline_path, output_path)
        for section, identical in diff.items():
            self.assertTrue(
                identical,
                f"audit_mode=False diverges from baseline on `{section}` "
                f"({config_path})",
            )

    def test_eod_breakout(self):
        if not os.path.isfile(EOD_BREAKOUT_BASELINE):
            self.skipTest(f"baseline missing: {EOD_BREAKOUT_BASELINE}")
        self._run_and_diff(
            EOD_BREAKOUT_CHAMPION, EOD_BREAKOUT_BASELINE,
            os.path.join(REPO_ROOT, "results", "eod_breakout",
                         "test_audit_off.json"),
        )

    def test_eod_technical(self):
        if not os.path.isfile(EOD_TECHNICAL_BASELINE):
            self.skipTest(f"baseline missing: {EOD_TECHNICAL_BASELINE}")
        self._run_and_diff(
            EOD_TECHNICAL_CHAMPION, EOD_TECHNICAL_BASELINE,
            os.path.join(REPO_ROOT, "results", "eod_technical",
                         "test_audit_off.json"),
        )


@unittest.skipUnless(
    SLOW_REGRESSION_ENABLED,
    "Slow audit regression. Enable with STRATEGY_BACKTESTER_AUDIT_REGRESSION=1.",
)
class TestAuditModeOn(unittest.TestCase):
    """audit_mode=True must produce same trades + equity as baseline.

    Collector populates with audit data, but the simulated outcome is
    invariant. Monkey-patches the strategy's generate_orders entry point
    to inject audit_mode + a stub collector via the context dict.
    """

    def _run_with_audit_and_diff(self, strategy_module, generator_class_name,
                                  config_path: str, baseline_path: str,
                                  output_path: str):
        from engine.pipeline import run_pipeline

        gen_class = getattr(strategy_module, generator_class_name)
        original = gen_class.generate_orders

        def patched(self, context, df_tick_data):
            context = dict(context)
            context["audit_mode"] = True
            context["audit_collector"] = {}
            return original(self, context, df_tick_data)

        gen_class.generate_orders = patched
        try:
            sweep = run_pipeline(config_path)
            sweep.save(output_path)
        finally:
            gen_class.generate_orders = original

        diff = _diff_summary_trades_equity(baseline_path, output_path)
        for section, identical in diff.items():
            self.assertTrue(
                identical,
                f"audit_mode=True diverges from baseline on `{section}` "
                f"({config_path})",
            )

    def test_eod_breakout(self):
        if not os.path.isfile(EOD_BREAKOUT_BASELINE):
            self.skipTest(f"baseline missing: {EOD_BREAKOUT_BASELINE}")
        from engine.signals import eod_breakout
        self._run_with_audit_and_diff(
            eod_breakout, "EodBreakoutSignalGenerator",
            EOD_BREAKOUT_CHAMPION, EOD_BREAKOUT_BASELINE,
            os.path.join(REPO_ROOT, "results", "eod_breakout",
                         "test_audit_on.json"),
        )

    def test_eod_technical(self):
        if not os.path.isfile(EOD_TECHNICAL_BASELINE):
            self.skipTest(f"baseline missing: {EOD_TECHNICAL_BASELINE}")
        from engine.signals import eod_technical
        self._run_with_audit_and_diff(
            eod_technical, "EodTechnicalSignalGenerator",
            EOD_TECHNICAL_CHAMPION, EOD_TECHNICAL_BASELINE,
            os.path.join(REPO_ROOT, "results", "eod_technical",
                         "test_audit_on.json"),
        )


if __name__ == "__main__":
    unittest.main()

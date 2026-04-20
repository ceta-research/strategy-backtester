# Strategy Optimization Prompt

Use this prompt to optimize the next strategy in the queue. Run in a fresh session AFTER the metrics audit + engine cleanup is complete and regression tests pass.

**Baseline reference:** `{POST_AUDIT_BASELINE}` — the commit at which the metrics audit landed. Replace this placeholder with the actual commit hash once the audit is complete. All drift checks below compare against this commit.

---

## Pre-flight checks

Before doing ANY work:

1. Run the regression test:
```bash
python scripts/regression_test.py
```
If any champion Calmar deviates >2% from the saved fixture, STOP and flag the issue. Do not proceed with optimization until the engine + metrics are verified clean.

The regression test has two modes:
- **Bootstrap mode** (first run after audit): no saved fixtures exist, records current numbers as ground truth.
- **Check mode** (default): compares against saved fixtures, fails on >2% drift.

2. Verify shared engine and metrics code have NOT been modified from the audit baseline:
```bash
git diff {POST_AUDIT_BASELINE} HEAD -- \
  engine/pipeline.py engine/utils.py engine/simulator.py \
  engine/ranking.py engine/charges.py \
  lib/metrics.py lib/backtest_result.py | wc -l
```
If non-zero, STOP. These files must not be changed during optimization.

---

## Prompt

```
Read these files for context:
- docs/sessions/completed/2026-04-14/SESSION_HANDOVER_OPTIMIZATION_RUNBOOK.md
- strategies/OPTIMIZATION_QUEUE.yaml
- docs/OPTIMIZATION_RUNBOOK.md

Read all necessary code to get a comprehensive understanding of the engine-pipeline
backtesting code — how configs drive signal generation, simulation, and result output.

Run the regression test (scripts/regression_test.py) FIRST. If any strategy's Calmar
deviates >2% from its documented value, STOP and report the issue.

If regression passes, proceed:

Claim the next PENDING strategy from OPTIMIZATION_QUEUE.yaml.
Follow docs/OPTIMIZATION_RUNBOOK.md and run the full analysis for the strategy (all
rounds and all additional work as necessary/discovered).

This is iterative. If Round 1 shows monotonic response, extend and re-sweep. If Round 3
fails robustness, revisit Round 2. Do not treat the rounds as a single linear pass.

Data sources:
- Rounds 0-3 (optimization): nse.nse_charting_day
- Round 4 cross-data-source (NSE):
  - nse.nse_charting_day (primary, already tested)
  - fmp.stock_eod with ".NS" suffix
  - nse.nse_bhavcopy_historical
- Round 4 cross-exchange (fmp.stock_eod):
  US, UK, Canada, China, Euronext, Hong Kong, South Korea, Germany, Saudi Arabia, Taiwan

Note: nse.nse_charting_day dataset grows over time. Small drifts (e.g. eod_breakout Cal
0.516 → 0.419 from 2026-03 to 2026-04) are expected as newer data is added. This is
not a regression — the regression test's ±2% tolerance is calibrated accordingly.

Scope: ONE strategy, fully complete (Rounds 0-4 + OPTIMIZATION.md filled in).
This may span multiple sessions — track state in strategies/{name}/OPTIMIZATION.md
so the next session can resume.

Feel free to use any tools (web-fetches, web-search, etc) as necessary.

Update strategies/OPTIMIZATION_QUEUE.yaml and other docs as appropriate.

Execution:
- Run on the server (48 vCPU, 251GB RAM): ssh swas@80.241.215.48
- Workflow: git push locally → ssh → cd /opt/insydia/strategy-backtester && git pull → python run.py → git push results (or scp back)
- If server unavailable, run locally (python run.py --config ... --output ...).
- Do NOT use CR project execution APIs (run_remote.py / CloudOrchestrator). They are unreliable.
- Data fetching uses CR APIs internally (cr_client.py) — this is fine, no action needed.

CRITICAL RULES:
- NEVER modify shared engine or metrics code. Protected files:
    engine/pipeline.py, engine/utils.py, engine/simulator.py,
    engine/ranking.py, engine/charges.py,
    lib/metrics.py, lib/backtest_result.py
  These were verified correct during the metrics audit (see docs/AUDIT_FINDINGS.md).
  If you hit OOM or performance issues, fix them ONLY in the strategy's signal generator.
- After each round completes, verify the result is plausible:
  - Calmar > 2.0 on NSE is suspicious (check for bugs)          # TODO-POST-AUDIT: recalibrate
  - CAGR > 35% on 16-year backtest is suspicious                # TODO-POST-AUDIT: recalibrate
  - If OOS Calmar > 2x IS Calmar, investigate before celebrating
  NOTE: thresholds above were calibrated on the pre-audit (deflated) CAGR formula.
  After the metrics fix lands, raise them accordingly (likely Calmar ~2.9, CAGR ~50%)
  and remove the TODO markers. See docs/AUDIT_FINDINGS.md for the corrected formula.
- Track all state in strategies/{name}/OPTIMIZATION.md so any session can resume.
```

---

## Pre-requisites (must be done in a separate session first)

1. **✓ Engine reverted & committed** — commit `e1d233f` on 2026-04-20:
   - `engine/pipeline.py` restored from e7122a6 (original, with forward-fill, no instrument limits)
   - `engine/utils.py` restored from e7122a6 (original, with forward-fill)
   - `engine/signals/momentum_dip_quality.py` kept with signal gen optimizations (lazy exit_data, bisect, etc.)

2. **Complete the metrics audit** (docs/AUDIT_CHECKLIST.md):
   - Tier 1 (metrics library) is mandatory — covers the known CAGR `ppy=252` bug on calendar-day inputs
   - Tier 2 (pipeline/simulator) items flagged P0/P1 are mandatory
   - Tiers 3-5 can be deferred but recommended

3. **Commit audit fixes** and capture the commit hash as `{POST_AUDIT_BASELINE}`. Replace the placeholder in the Pre-flight checks section above with this hash.

4. **Write `docs/AUDIT_FINDINGS.md`** — one entry per bug found, with file:line, impact, and fix sketch. Referenced by the CRITICAL RULES above.

5. **Archive existing results** — all historical `results/*` were produced on the buggy metrics formula:
   ```bash
   mkdir -p results/pre_audit_2026-04
   mv results/* results/pre_audit_2026-04/ 2>/dev/null || true
   # Keep results/_archive/ where it was (historical standalone runs)
   mv results/pre_audit_2026-04/_archive results/ 2>/dev/null || true
   ```

6. **Build `scripts/regression_test.py`** (does not exist yet):
   - Bootstrap mode: on first run, records champion numbers as ground-truth fixtures under `tests/fixtures/`
   - Check mode (default): runs each COMPLETE strategy's champion config, asserts Calmar within ±2% of its fixture
   - Prints PASS/FAIL per strategy with the numeric deviation
   - Must use the same code path as run.py (no shortcuts)

7. **Reset OPTIMIZATION_QUEUE.yaml:**
   - All 6 previously-completed strategies → `status: PENDING`
   - Clear `best_calmar`, `best_cagr`, `session` fields
   - Keep `priority` ordering unchanged
   - Keep `notes` but prepend `"[PRE-AUDIT] "` to flag that the numbers inside are from the old formula

8. **Update `docs/OPTIMIZATION_RUNBOOK.md`** — recalibrate plausibility thresholds (Calmar, CAGR, Sharpe ranges) based on the corrected metrics formula. Remove the TODO-POST-AUDIT markers in this prompt once done.

9. **Fix cloud execution for heavy strategies** (optional, separate effort):
   - See docs/CLOUD_EXECUTION_ISSUES.md
   - Key: fix `_safe_to_list` monkey-patch, fix dep-install reliability
   - Do NOT modify shared engine code — all fixes must be in signal generators or cloud_orchestrator

# Strategy Optimization Prompt

Use this prompt to optimize the next strategy in the queue. Run in a fresh session AFTER the engine code cleanup is complete and regression tests pass.

---

## Pre-flight checks

Before doing ANY work:

1. Run the regression test:
```bash
python scripts/regression_test.py
```
If any champion Calmar deviates >2% from documented value, STOP and flag the issue. Do not proceed with optimization until the engine is verified clean.

2. Verify `engine/pipeline.py` and `engine/utils.py` have NOT been modified from the baseline commit. Run:
```bash
git diff e7122a6 HEAD -- engine/pipeline.py engine/utils.py | wc -l
```
If non-zero, STOP. These files must not be changed.

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
- NEVER modify engine/pipeline.py or engine/utils.py. These are shared by all strategies.
  If you hit OOM or performance issues, fix them ONLY in the strategy's signal generator.
- After each round completes, verify the result is plausible:
  - Calmar > 2.0 on NSE is suspicious (check for bugs)
  - CAGR > 35% on 16-year backtest is suspicious
  - If OOS Calmar > 2x IS Calmar, investigate before celebrating
- Track all state in strategies/{name}/OPTIMIZATION.md so any session can resume.
```

---

## Pre-requisites (must be done in a separate session first)

1. **Commit correct engine code:**
   - `engine/pipeline.py` from commit e7122a6 (original, with forward-fill, no instrument limits)
   - `engine/utils.py` from commit e7122a6 (original, with forward-fill)
   - `engine/signals/momentum_dip_quality.py` with signal gen optimizations (lazy exit_data, bisect, etc.)

2. **Create regression test script** (`scripts/regression_test.py`):
   - Runs each completed strategy's champion config
   - Asserts Calmar within ±2% of documented value
   - Prints PASS/FAIL per strategy

3. **Reset affected strategies in OPTIMIZATION_QUEUE.yaml:**
   - momentum_top_gainers: status → IN_PROGRESS, note regression
   - earnings_dip: status → IN_PROGRESS, note regression
   - eod_breakout: note data drift (0.516 → 0.419 due to dataset growth)

4. **Fix cloud execution for heavy strategies** (optional, separate effort):
   - See docs/CLOUD_EXECUTION_ISSUES.md
   - Key: fix `_safe_to_list` monkey-patch, fix dep-install reliability
   - Do NOT modify shared engine code — all fixes must be in signal generators or cloud_orchestrator

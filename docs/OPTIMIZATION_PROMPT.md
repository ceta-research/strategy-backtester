# Strategy Optimization Prompt

Use this prompt to optimize the next strategy in the queue. Run in a fresh session AFTER the metrics audit + engine cleanup is complete and regression tests pass.

**Baseline reference:** `fbcd36a` — the commit at which the metrics audit landed (2026-04-22). All drift checks below compare against this commit.

---

## Pre-flight checks

Before doing ANY work:

1. Run pinned regression snapshots. Three COMPLETE strategies have snapshots in `tests/regression/snapshots/`:
   - `eod_breakout_champion`
   - `enhanced_breakout_round2`  (NOTE: invalidated by P0 #10 — expect mismatch until re-optimized)
   - `momentum_cascade_champion`

   For each, re-run the champion config and compare:
   ```bash
   python run.py --config strategies/eod_breakout/config_champion.yaml --output /tmp/eod_check.json
   python -m tests.regression.snapshot compare eod_breakout_champion /tmp/eod_check.json
   ```
   Snapshot tolerance is `abs=1e-6, rel=1e-4` (tight). If a field exceeds tolerance, STOP unless the diff is explained by known data drift (nse.nse_charting_day grows over time) or a documented audit change.

2. Verify shared engine and metrics code have NOT been modified from the audit baseline:
```bash
git diff fbcd36a HEAD -- \
  engine/pipeline.py engine/utils.py engine/simulator.py \
  engine/ranking.py engine/charges.py engine/exits.py engine/order_key.py \
  lib/metrics.py lib/backtest_result.py lib/equity_curve.py | wc -l
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

Run the pinned regression snapshots FIRST (see Pre-flight checks above). If any strategy
exceeds tolerance and the diff isn't explained by known data drift or documented audit
changes, STOP and report the issue.

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
- LOCAL ONLY. Do NOT use CR compute / server execution / CloudOrchestrator. They are unreliable
  and per 2026-04-22 handover, all work runs locally with proper memory management.
- Command: `python run.py --config <path> --output <path>`
- Memory management playbook (see docs/SESSION_HANDOVER_2026-04-22.md §5):
  - Narrow the data window (date range + universe) per strategy before loading
  - Pre-filter the universe per-strategy in the signal generator
  - Chunk exit computation where feasible
  - Call `gc.collect()` at natural boundaries
  - Do NOT re-introduce the engine regression hacks (top-200 cap, min_epoch filter,
    forward-fill removal). Those are banned; they silently break results.
- Data fetching uses CR APIs internally (cr_client.py) — this is fine, no action needed.

CRITICAL RULES:
- NEVER modify shared engine or metrics code. Protected files:
    engine/pipeline.py, engine/utils.py, engine/simulator.py,
    engine/ranking.py, engine/charges.py, engine/exits.py, engine/order_key.py,
    lib/metrics.py, lib/backtest_result.py, lib/equity_curve.py
  These were verified correct during the metrics audit (see docs/AUDIT_FINDINGS.md).
  If you hit OOM or performance issues, fix them ONLY in the strategy's signal generator
  (see memory management playbook above).
- After each round completes, verify the result is plausible (post-audit thresholds,
  calibrated against the corrected CAGR formula — see docs/AUDIT_FINDINGS.md):
  - Calmar > 2.9 on NSE is suspicious (check for bugs)
  - CAGR > 50% on 16-year backtest is suspicious
  - If OOS Calmar > 2x IS Calmar, investigate before celebrating
  - REALITY CHECK: honest single-strategy CAGR typically lands 7–15% on NSE after
    the audit fixes. NIFTYBEES buy-and-hold ~12% is the bar to clear. A 20%+ CAGR
    should trigger a bias re-check (universe survivorship, same-bar entry, stale
    charges). See docs/SESSION_HANDOVER_2026-04-22.md §7.
- Track all state in strategies/{name}/OPTIMIZATION.md so any session can resume.
```

---

## Pre-requisites (status as of 2026-04-22)

1. **✓ Engine reverted & committed** — commit `e1d233f` on 2026-04-20 (then superseded by audit fixes through `fbcd36a`):
   - `engine/pipeline.py`, `engine/utils.py` restored from e7122a6 (forward-fill, no instrument caps, no min_epoch filter)
   - `engine/simulator.py` fixed (end_epoch authoritative, end-of-sim force-close)

2. **✓ Metrics audit complete** — 17/17 P0 + 49/50 P1 fixes landed. 1 P1 deferred (`_portfolio_metrics` fixture), 2 P2s + 6 P3s deferred. See `docs/AUDIT_FINDINGS.md`.

3. **✓ Audit baseline captured** — commit `fbcd36a`. All drift checks above reference this hash.

4. **✓ `docs/AUDIT_FINDINGS.md` written** — 1951-line authoritative log with every P0/P1/P2 entry.

5. **✓ Historical results migrated** — 214 files re-computed to `results_v2/*.json` via `scripts/recompute_metrics.py` using embedded equity curves and corrected CAGR formula.

6. **Regression tooling** — pinned snapshots for 3 COMPLETE strategies in `tests/regression/snapshots/` (eod_breakout, enhanced_breakout, momentum_cascade). Compare via `python -m tests.regression.snapshot compare <name> <result.json>`. See Pre-flight checks above. A one-shot `scripts/regression_test.py` runner was NOT built — the manual compare pattern is sufficient for a 3-strategy pre-flight.

7. **OPTIMIZATION_QUEUE.yaml state** — 3 COMPLETE with pinned snapshots (eod_breakout, enhanced_breakout, momentum_cascade), 1 IN_PROGRESS (earnings_dip — regression needs R2-R4 re-run), 2 AUDIT_BLOCKED (momentum_top_gainers, momentum_rebalance — pending full-NSE local A/B), 1 AUDIT_RETIRED (momentum_dip_quality), 22 PENDING. `enhanced_breakout` COMPLETE is invalidated by P0 #10 and must be re-optimized.

8. **Plausibility thresholds recalibrated** — Calmar >2.9, CAGR >50% (see CRITICAL RULES above). `docs/OPTIMIZATION_RUNBOOK.md` update deferred; the source of truth for plausibility is this prompt + `docs/SESSION_HANDOVER_2026-04-22.md` §7.

9. **Cloud execution** — NOT fixed. Not needed: all work is local per the 2026-04-22 decision. Memory-constrained strategies use the playbook in the Execution section above.

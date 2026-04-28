# Session handover — 2026-04-28 pt6 (inspection drill, Phases 0+1+2a complete)

**Predecessor:** [`2026-04-28_pt5_inspection_plan.md`](2026-04-28_pt5_inspection_plan.md) — revised drill plan covering eod_breakout + eod_technical at deep-audit depth.

This is a **progress checkpoint**, not a re-plan. The plan in pt5 is current.
This doc captures: what got done, where we stopped, plan refinements applied
mid-session, and exactly where to pick up.

---

## TL;DR

Phases 0, 1, and 2a of the drill are done and committed. All artifacts on disk.
Tree clean. Phase 2b (hooks in `engine/signals/eod_breakout.py`) starts next.

| Phase | Status | Commit | Key artifact |
|---|---|---|---|
| 0 — pin baselines | ✅ | `22efb42` | `results/<strategy>/champion_pre_audit_baseline.json` (gitignored, local) |
| 1 — pipeline map | ✅ | `22efb42` | `docs/inspection/PIPELINE_MAP.md` |
| 2a — audit_io module | ✅ | `2829a49` | `lib/audit_io.py` |
| 2b-2e — hooks + regression + audit run | pending | — | (next session) |
| 3 + 4 | pending | — | (later sessions) |

---

## Pinned baselines (Phase 0)

Engine state at pin: working tree at `3457840`. Baselines are reproducible.

| Strategy | CAGR | MDD | Calmar | Sharpe (canonical) | Vol | Trades | Win rate |
|---|---|---|---|---|---|---|---|
| eod_breakout | 17.68% | -26.75% | 0.661 | **1.183** | 13.25% | 1795 | 43.3% |
| eod_technical | 19.63% | -25.95% | 0.757 | **1.067** | 16.53% | 1303 | 41.8% |

**Sharpe discrepancy resolved:** engine canonical = 1.183 for eod_b. The 1.334
in `strategies/eod_breakout/config_champion.yaml` line 7 is stale doc text
(legacy CAGR/vol calc). Per STATUS line 17, docs were realigned 2026-04-28 but
this single comment was missed. Cosmetic; deferred (don't fix during the
drill — out of scope).

---

## Plan refinements applied mid-session (per user feedback)

Three clarifications from the user, all already applied to `pt5_inspection_plan.md`
and `PIPELINE_MAP.md`. Captured here to preserve reasoning:

### 1. `n_day_gain_threshold` framing

Original framing in PIPELINE_MAP: "DEAD CONFIG — `run_scanner` doesn't apply it."

User correction: under `top_gainer` ranking with `max_positions=15`, the *filter*
and the *ranker* converge on the same picked set whenever ≥15 positive-gain
candidates exist per day (typical for champion). The field is parsed-but-unused
at the scanner stage, but the ranker accomplishes the same effect.

**Applied:** PIPELINE_MAP.md downgraded from "FINDING / dead config" to
"observed redundancy". Phase 3 sanity check is to count days where filter-vs-
ranker would diverge (zero/few = redundant in practice; meaningful divergence
= revisit).

### 2. Multiprocessing nondeterminism in eod_t

Original framing: regression test must force `multiprocessing_workers=1`
to avoid spurious failures.

User correction: for single-config champion runs, `generate_order_df()`
sorts by `[instrument, entry_epoch, exit_epoch]` (line 153 of
`order_generator.py`), which uniquely identifies each row. Static config →
deterministic output expected.

**Applied:** Phase 2d regression test plan now says "try natural diff first;
apply `multiprocessing_workers=1` workaround only if natural test fails."
Documented in PIPELINE_MAP open-questions section.

### 3. Cross-strategy framing

Original framing: cross-strategy comparison would surface causal claims about
feature differences.

User correction: the strategies are intentionally different and don't need
apples-to-apples. Goal is to **understand each individually** so each can be
optimized on its own terms.

**Applied:** Plan section 3e renamed from "Cross-strategy comparison" to
"Cross-strategy notes (NOT comparison)". Findings here are descriptive only.
Optimization hypotheses (Phase 4) are per-strategy.

---

## What's on disk (canonical refs for next session)

| File | Purpose | Read-order priority |
|---|---|---|
| `docs/sessions/2026-04-28_pt5_inspection_plan.md` | Full Phase 0-4 plan | 1 |
| `docs/inspection/PIPELINE_MAP.md` | Branch enumeration; hook placement spec at the bottom | 2 |
| `lib/audit_io.py` | Phase 2a artifact: schemas + writers + helpers | 3 (skim docstrings) |
| `results/<strategy>/champion_pre_audit_baseline.json` | Pre-Phase-2 reference for byte-identical regression | (used by Phase 2d test) |
| (this doc) | Resume pointer + plan refinements | 0 |

---

## Phase 2b spec (next session opening)

Per `PIPELINE_MAP.md` "Audit-hook placement summary" section.

### Target file: `engine/signals/eod_breakout.py` (388 lines)

**4 hooks, ~60 lines total, all gated by `context.get("audit_mode", False)`:**

1. **HOOK 1** — line 52 (post-`run_scanner`): emit candidate-set snapshot
   (instrument × date × scanner_pass).
2. **HOOK 2** — lines 171-179 (entry_filter): break the monolithic AND into
   per-clause expressions; emit clause flags + all_clauses_pass into the
   audit collector. ~25 lines.
3. **HOOK 3** — lines 264-275 (post-walk-forward, append order row): emit
   per-trade row with at-entry context (regime_state, ds, n_day_high). ~15 lines.
4. **HOOK 3a** — `_walk_forward_tsl` (lines 316-385): augment to return
   `reason` string (audit_mode-only branch — no behavior change). ~10 lines.

**Audit collector pattern:**
- Pass collector via `context["audit_collector"]` (a dict with parquet-bound
  buffers). Initialize in the wrapper that calls `generate_orders` (or in a
  pipeline.py-level prelude — but pipeline.py is protected, so easier to do
  it inside the signal generator when audit_mode is set).
- Hooks append rows to in-memory list/dict; final write happens after
  signal-gen returns.

### Then Phase 2c (eod_technical legacy path)

Per PIPELINE_MAP: ~85 lines across `signals/eod_technical.py` +
`scanner.py` + `order_generator.py`. More complex due to multiprocessing;
defer single-process workaround unless regression test fails.

### Then Phase 2d (mandatory regression test)

`tests/test_audit_noninvasive.py`:
1. `audit_mode=False` on hooked code → byte-identical to
   `champion_pre_audit_baseline.json`.
2. `audit_mode=True` → byte-identical to (1).

**Do not proceed to Phase 3 if either diff is non-zero.**

### Then Phase 2e

Run both champions with `audit_mode=True`; produce `audit_drill_<ts>/`
artifacts.

---

## Open questions still un-decided

1. **Comparison baseline (pt5 plan, "Open question I'm NOT pre-deciding").**
   Whether to also instrument the prior champion (pre-regime, pre-holdout)
   for eod_b to attribute the +35.24pp 2025 improvement at trade level.
   Recommendation: skip unless Phase 3 surfaces a question this would answer.

2. **Phase 4 timing.** Plan says Phase 4 is a separate session. Confirmed at
   end of Phase 3.

---

## Time-box (carried from pt5 plan)

| Phase | Budget | Spent | Cumulative remaining |
|---|---|---|---|
| 0 — prep | 1 hr | ~30 min | (done) |
| 1 — map | 2 hrs | ~1 hr | (done) |
| 2a — audit_io | (in 2's budget) | ~1 hr | (done) |
| 2b — eod_b hooks | (within 10 hrs P2 total) | — | ~9 hrs Phase 2 budget remaining |
| 2c-2e | (within 10 hrs P2 total) | — | |
| 3 — inspect | 4 hrs | — | 4 hrs |
| **Hard stop** | 18.5 hrs | ~2.5 hrs spent | ~16 hrs remaining |

---

## Resume sequence (next session)

1. Read this doc (pt6).
2. Skim pt5 plan + PIPELINE_MAP "Audit-hook placement summary" section.
3. Begin Phase 2b: open `engine/signals/eod_breakout.py`, add the 4 hooks.
4. Smoke-test: run champion with `audit_mode=False` → must reproduce
   `champion_pre_audit_baseline.json` byte-identically before adding any
   `audit_mode=True` paths.
5. Once HOOK 1-3a are in and the no-audit-mode regression passes, build the
   audit collector wiring + parquet emission, then run Phase 2e for eod_b.
6. THEN Phase 2c (eod_t).

---

## What NOT to do (carried from pt5)

- No new parameter sweeps "while we're at it".
- No edits to protected files (pipeline.py, simulator.py, exits.py, etc.).
- No bias re-audit. Engine was audited at `fbcd36a`.
- No additions of features / filters during the drill.

---

## Commit list (pt5+pt6 sessions)

| Commit | Title |
|---|---|
| `22efb42` | Inspection drill 2026-04-28 pt5 (Phases 0+1): plan + pipeline map |
| `2829a49` | Inspection drill 2026-04-28 pt5 (Phase 2a): lib/audit_io.py writer module |

Plus all earlier pt2/pt3/pt4 commits unpushed: total **9 unpushed commits**.
Push when ready.

---

## Working state at end of session

- Tree clean (no uncommitted changes).
- Both baseline JSONs reproducible at `3457840` engine state.
- Test suite: 37/37 ensemble tests passing (last run pt2; not re-run this session
  since no engine code touched).
- Champion ensembles unchanged.
- `lib/audit_io.py` smoke-tested; all 5 writers round-trip; filter-marginal
  math validated on synthetic distribution.

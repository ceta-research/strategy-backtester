# Pipeline inspection drill — revised plan (eod_breakout + eod_technical)

**Predecessor:** [`2026-04-28_pt4_handover.md`](2026-04-28_pt4_handover.md) — original drill scope (eod_breakout only).
**Status:** This doc supersedes pt4's "Phase 0-4 plan" section. Scope expanded to both
champion strategies; depth choice for eod_technical settled (**deep audit**).

---

## Targets

Both at champion config, full period 2010-01-01 → 2026-03-19.

| | eod_breakout | eod_technical |
|---|---|---|
| Config | `strategies/eod_breakout/config_champion.yaml` | `strategies/eod_technical/config_champion.yaml` |
| Headline (per config comment) | CAGR 17.68% / MDD -26.75% / Sharpe 1.334 | CAGR 19.63% / MDD -25.90% / Sharpe ~1.07 |
| Signal entry point | `engine/signals/eod_breakout.py` (388 lines) | `engine/signals/eod_technical.py` (318 lines) |
| Underlying | self-contained polars expressions | wraps `engine/scanner.py` (162) + `engine/order_generator.py` (376) |
| Regime gate | enabled (NIFTYBEES SMA 100, force-exit) | code present, **disabled** in champion config |
| n_day_high / ma | 3 / 10 | 5 / 3 |
| direction_score | {5, 0.40} | {3, 0.54} |
| min_hold / TSL | 7 / 8% | 3 / 10% |
| Ranking window | 180 d top_gainer | 30 d top_gainer |
| Sizing | 4.5% of avg_txn | fixed 1B (effectively unbinding) |
| Universe filter | price ≥ 99, n_day_gain ≥ 0 | price ≥ 50, no gain filter |

**Sharpe discrepancy to resolve** (pt4 quoted 1.183 for eod_b; champion config comment
says 1.334). Phase 0 step 1: re-run champion fresh and adopt whatever the engine
emits today. Document the delta if any.

---

## Depth choice (settled per user direction)

**Deep audit for both, including edits to `engine/scanner.py` + `engine/order_generator.py`.**

These are signal-side dispatch targets, not on the pt4 protected list (which only
covers `pipeline.py`, `simulator.py`, `exits.py`, `utils.py`, `ranking.py`,
`charges.py`, `order_key.py`, plus `lib/metrics.py` / `backtest_result.py` /
`equity_curve.py`). They ARE shared by other strategies via the legacy wrapper, so
edits must be **observation-only** (no behavioral change) and **gated** by an
`audit_mode` flag so they're inert when off.

**Risk to manage:** `scanner.py` and `order_generator.py` are shared across many
signal modules. Any audit hook must:
1. Be opt-in via `context['audit_mode']` (default off → byte-identical to today).
2. Not mutate the dataframes it observes (only project-and-emit).
3. Have a regression test confirming `audit_mode=False` produces byte-identical
   trades+curve to a pinned baseline before-and-after the edits.

---

## Phase 0 — Prep & verification (~1 hr)

1. **Re-run both champions fresh.** Capture current CAGR/MDD/Sharpe for each.
   Resolve the eod_b Sharpe discrepancy.
2. **Pin baselines for byte-identical regression.** Save current
   `champion.json` for each as `champion_pre_audit_baseline.json`. After Phase 2
   edits with `audit_mode=False`, diff must be zero on trades + equity curve.
3. **Confirm no protected-file edits required.** Final check that the four
   target modules (`signals/eod_breakout.py`, `signals/eod_technical.py`,
   `engine/scanner.py`, `engine/order_generator.py`) cover the full needed surface.

**Phase 0 deliverables:**
- `results/<strategy>/champion_pre_audit_baseline.json` for both
- A 5-line note in this doc on the Sharpe discrepancy resolution

---

## Phase 1 — Pipeline map (~2 hrs, read-only)

Produce `docs/inspection/PIPELINE_MAP.md` — one row per branch / decision point.

**Files to map:**
- Read-only (protected, do not edit): `engine/pipeline.py`, `engine/simulator.py`,
  `engine/exits.py`, `engine/utils.py`, `engine/ranking.py`, `engine/order_key.py`
- Read + plan hooks (will edit in Phase 2): `engine/signals/eod_breakout.py`,
  `engine/signals/eod_technical.py`, `engine/signals/base.py`,
  `engine/scanner.py`, `engine/order_generator.py`

**Schema for PIPELINE_MAP.md** (one table per file):

| Line | Branch | Trigger condition | Expected behavior | Anomaly to watch for | Strategies affected |
|---|---|---|---|---|---|
| eod_breakout.py:149-152 | regime gate | NIFTYBEES below SMA(N) | nullify scanner_config_ids → no entries | gate flickers daily during chop | eod_b only |

Each strategy gets its own column where divergent branches exist (e.g., regime
gate present in eod_b enabled, present in eod_t but disabled).

**Phase 1 deliverable:** `docs/inspection/PIPELINE_MAP.md`.

---

## Phase 2 — Instrumentation harness (~8-12 hrs)

### 2a. Shared parquet writers (`lib/audit_io.py`, new sibling module)

NOT a "shared collector". Just typed writers + schema constants:

```python
# lib/audit_io.py (new, not protected)
ENTRY_AUDIT_SCHEMA = {...}           # post-scanner per (date, instrument)
TRADE_LOG_SCHEMA = {...}             # per actual trade with at-entry context
DAILY_SNAPSHOT_SCHEMA = {...}        # per simulation date
SCANNER_REJECT_SUMMARY_SCHEMA = {...}# per (date, scanner_clause) reject counts
FILTER_MARGINALS_SCHEMA = {...}      # per filter clause: pass-rate, conditional pass-rate

def write_entry_audit(df, out_dir): ...
def write_trade_log(df, out_dir): ...
def write_daily_snapshot(df, out_dir): ...
def write_scanner_reject_summary(df, out_dir): ...
def write_filter_marginals(df, out_dir): ...
def write_audit_readme(out_dir, strategy, config_path, run_metadata): ...
```

### 2b. Per-strategy collectors (different code paths, can't share)

**eod_breakout collector** (in `engine/signals/eod_breakout.py`):
- Hook 1 (post-scanner, pre-entry-filter): collect entry_audit candidate rows.
- Hook 2 (after entry_filter eval): mark `all_clauses_pass`, capture per-clause
  pass flags.
- Hook 3 (after rank + capacity): mark `final_picked`, `rank_in_day`.
- Hook 4 (in walk_forward_tsl): emit per-trade hold-time max-drawup/down +
  exit_reason taxonomy (TSL / regime / gap / last_bar / min_hold_violated).
- Hook 5 (per-config loop): aggregate filter-marginal stats.

**eod_technical collector** (across three files):
- `engine/signals/eod_technical.py`: top-level hook for per-config audit
  context object passed down through `context['audit_mode']` and
  `context['audit_collector']`.
- `engine/scanner.py:process()`: emit pre-scanner / post-scanner row counts
  and per-clause rejection counts (scanner_reject_summary).
- `engine/order_generator.py:process()`: emit per (date, instrument) entry
  decision rows with all entry-clause flags + capacity outcome.
- `engine/order_generator.py:generate_exit_attributes_for_instrument()`: emit
  per-trade exit context (exit_reason, max-drawup/down during hold).

**Constraint:** every hook must be guarded `if context.get('audit_mode'):`. With
flag off, byte-identical to current behavior. Test before merging.

### 2c. Daily portfolio snapshot

Cleanest place: post-simulation, by replaying the trade list against the equity
curve. Doesn't require simulator edits. Owned by `lib/audit_io.py` → a
`build_daily_snapshot(trades_df, equity_curve_df)` helper.

### 2d. Output location & naming

`results/<strategy>/audit_drill_<UTC-iso>/`:
- `entry_audit.parquet`
- `trade_log.parquet`
- `daily_snapshot.parquet`
- `scanner_reject_summary.parquet`
- `filter_marginals.parquet`
- `README.md` (config used, engine commit, row counts, run timestamps)

### 2e. Mandatory regression test

Before any inspection happens, prove the instrumentation is non-disturbing:

```
1. Run champion with audit_mode=False  → save trades + curve
2. Diff against pre-Phase-2 baseline   → must be byte-identical
3. Run champion with audit_mode=True   → save trades + curve
4. Diff against #1                     → must be byte-identical
```

If either diff is nonzero, the hooks have side effects → fix before proceeding.

**Phase 2 deliverables:**
- `lib/audit_io.py` (new)
- Hooks in `engine/signals/eod_breakout.py`
- Hooks in `engine/signals/eod_technical.py`, `engine/scanner.py`,
  `engine/order_generator.py`
- `tests/test_audit_noninvasive.py` (regression suite)
- `audit_drill_<ts>/` artifacts for both strategies

---

## Phase 3 — Inspect (~3-4 hrs)

### 3a. Build the OHLCV-fetch helper FIRST

Without this, Phase 3b spot-checks won't actually happen. Small script,
~30 lines:

```python
# scripts/fetch_ohlcv_window.py
def fetch_ohlcv_window(instrument, entry_date, exit_date, pad_days=30):
    """Returns polars df with date, open, high, low, close, volume for
    [entry-pad, exit+5] window. Uses same data_provider as champion."""
```

Output: prints raw rows + a tiny matplotlib/polars-plot for visual sanity.

### 3b. Per-strategy sanity checks

| Check | Query | Pass criterion |
|---|---|---|
| Scanner pass-rate per day | `entry_audit` group by date | within 50-95% range, no all-true / all-false |
| Regime transitions (eod_b only) | distinct regime_state by date | sensible — not flipping every other day |
| direction_score distribution | quantiles | mostly 0.4-0.7, no degenerate spikes |
| Cardinality of unique instruments entered | `trade_log` distinct | sensible vs universe size |

### 3c. Spot-check audits (manual)

- 20 random entered orders → trace from raw OHLCV using `fetch_ohlcv_window`,
  verify n_day_high actually crossed, ds plausible, regime green (eod_b only).
- 5 "should-have-entered but didn't" → confirm capacity binding via
  `rank_in_day > max_positions`, not silent filter drop.

### 3d. Pattern queries — IS/OOS split MANDATORY

Every Phase-3d query gets two output rows: 2010-2024 (IS for eod_b champion;
treat as IS for eod_t too even though eod_t didn't have a holdout selection)
and 2025+ (OOS).

| # | Query | What it answers |
|---|---|---|
| 1 | Top-20 trades by abs PnL | Which trades drove CAGR / MDD |
| 2 | MDD attribution | During worst DD window, top-10 contributing trades |
| 3 | Year-by-year CAGR + MDD contribution | Which years matter |
| 4 | Win-rate over time (rolling 60-trade) | Stable or trending |
| 5 | Hold-time distribution + by exit_reason | Stuck-in-losers tail? |
| 6 | DS-at-entry decile vs realized return | Conditional alpha? |
| 7 | Regime-at-entry vs realized return (eod_b) | Regime-edge effect |
| 8 | Sector / market-cap contribution | Concentration risks |
| 9 | "Should-have-entered" capacity-blocked count | How often is sizing the constraint |
| 10 | Filter clause conditional pass-rate | Which clauses are binding (definition: P(clause_i fails ∣ all others pass)) |

### 3e. Cross-strategy notes (NOT comparison)

Per user direction: the strategies are intentionally different. Goal is to
**understand each individually** so each can be optimized on its own terms,
not to force apples-to-apples. Three lightweight queries to surface
*observations* (not causal claims):

- Trade overlap: which (instrument, date) pairs do BOTH strategies enter? (curiosity, not optimization input)
- Exit-reason mix per strategy: what's the natural distribution for each?
- Ranking-window effect: 30d vs 180d → how does each strategy's picked set look at scale?

Findings here are descriptive. Optimization hypotheses (Phase 4) are
per-strategy.

### 3f. FINDINGS.md schema (define before queries run)

```markdown
# Inspection findings — <strategy>

## Verified observations
### Observation 1: <one-line summary>
- **Query:** <which Phase-3 query produced it>
- **Data:** <table snippet or numbers>
- **IS vs OOS:** <hold-up status>
- **Implication:** <what it suggests>

## Hypotheses (need follow-up)
### Hypothesis 1: ...

## Cross-strategy notes
...
```

**Phase 3 deliverables:**
- `scripts/fetch_ohlcv_window.py`
- `docs/inspection/FINDINGS_eod_breakout.md`
- `docs/inspection/FINDINGS_eod_technical.md`
- `docs/inspection/FINDINGS_cross.md`

---

## Phase 4 — Hypotheses (separate session)

Synthesize 2-4 concrete improvement candidates per strategy from Phase 3
verified observations. Each: mechanism, expected effect on CAGR/MDD/Sharpe,
implementation sketch (signal-side preferred), test plan. **Do not optimize
in this session.**

Candidates also include the free experiment from #14 in the review:
**enable the eod_t regime gate** — code is already present; turning it on
is a 2-line config change.

---

## Open question I'm NOT pre-deciding

**Comparison baseline (pt4 q3).** Whether to also instrument the prior champion
(pre-regime, pre-holdout) for eod_b to attribute the +35.24pp 2025 improvement
at trade level. Recommendation: skip unless Phase 3 surfaces a question this
would answer. The marginal value is high but the cost is ~1.5x.

---

## Hard time-box

| Phase | Budget | Cumulative |
|---|---|---|
| 0 — prep | 1 hr | 1 |
| 1 — map | 2 hrs | 3 |
| 2 — harness + tests | 10 hrs | 13 |
| 2-runs — fresh runs with audit | 1.5 hrs | 14.5 |
| 3 — inspect | 4 hrs | 18.5 |
| **Stop** | | |

If we hit 18.5 hrs and findings are partial, stop and write up partials. Phase 4
is a different session regardless.

---

## What NOT to do (carried from pt4)

- No new parameter sweeps "while we're at it". This is forensics, not
  optimization.
- No edits to protected files (`pipeline.py`, `simulator.py`, `exits.py`,
  `utils.py`, `ranking.py`, `charges.py`, `order_key.py`,
  `lib/metrics.py`/`backtest_result.py`/`equity_curve.py`).
- No bias re-audit. Engine was audited at `fbcd36a`. Inspection assumes audit
  is correct; if anomalies surface, escalate to a separate task.
- No additions of features / filters during the drill. Read-and-trace.
- No attempt to attribute everything in one pass. 2-3 strongest patterns per
  strategy is enough; stop.

---

## Files to read first when next session starts

1. **This doc** — full context.
2. `2026-04-28_pt4_handover.md` — original drill objective (still valid).
3. `strategies/eod_breakout/VOL_FILTER_2026-04-28.md` — what just failed, why.
4. `engine/signals/eod_breakout.py` (388 lines).
5. `engine/signals/eod_technical.py` (318 lines).
6. `engine/scanner.py` (162 lines).
7. `engine/order_generator.py` (376 lines).
8. `engine/signals/base.py` selectively.
9. `engine/pipeline.py` — read-only inspection.
10. `engine/simulator.py` — read-only inspection.

---

## Resume sequence

1. Read this doc.
2. Phase 0: re-run both champions fresh, capture baselines, resolve Sharpe
   discrepancy.
3. Phase 1: write `docs/inspection/PIPELINE_MAP.md`.
4. Phase 2: build `lib/audit_io.py`, then hooks (eod_b first, then eod_t legacy
   path), then regression tests, then run with `audit_mode=True`.
5. Phase 3: build OHLCV helper, run sanity / spot-check / pattern queries with
   IS/OOS split; write `FINDINGS_*.md`.
6. Stop at hard time-box. Phase 4 is a separate session.

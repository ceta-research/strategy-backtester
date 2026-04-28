# Session handover — 2026-04-28 pt9 (Phase 2 complete)

**Predecessor:** [`2026-04-28_pt8_handover.md`](2026-04-28_pt8_handover.md) — Phase 2c checkpoint.

Phase 2d (regression test) and Phase 2e (audit-drill runner + parquet
artifacts) are done. Phase 2 is complete. Phase 3 (inspection of audit
artifacts) is next.

---

## TL;DR

Audit hooks landed in eod_breakout (pt7) and the eod_technical legacy path
(pt8) are now backed by:
1. **`tests/test_audit_noninvasive.py`** — 9 tests; static guards always run,
   slow runtime regression gated by `STRATEGY_BACKTESTER_AUDIT_REGRESSION=1`.
2. **`scripts/run_audit_drill.py`** — runs both champions with `audit_mode=True`
   and emits 6-7 parquet artifacts per strategy under
   `results/<strategy>/audit_drill_<UTC-iso>/`.

Both champions audited successfully; artifacts on disk. Tree clean (drill
runs are gitignored under `results/`).

| Phase | Status | Commit | Key artifact |
|---|---|---|---|
| 0 — pin baselines | ✅ | `22efb42` | `champion_pre_audit_baseline.json` (×2) |
| 1 — pipeline map | ✅ | `22efb42` | `docs/inspection/PIPELINE_MAP.md` |
| 2a — audit_io module | ✅ | `2829a49` | `lib/audit_io.py` |
| 2b — eod_b hooks | ✅ | `f6ca734` | `engine/signals/eod_breakout.py` (4 hooks) |
| 2c — eod_t hooks | ✅ | `813a89d` | `engine/scanner.py` + `engine/order_generator.py` (5 hooks) |
| 2d — regression test | ✅ | `9c635ff` | `tests/test_audit_noninvasive.py` |
| 2e — audit run | ✅ | (this commit) | `scripts/run_audit_drill.py` + per-strategy `audit_drill_*/` dirs |
| 3 — inspect | pending | — | `docs/inspection/FINDINGS_*.md` |
| 4 — improvement hypotheses | pending | — | (separate session) |

---

## Phase 2d — what landed

`tests/test_audit_noninvasive.py` (~300 lines).

### Static guards (always run, fast)

- **`TestAuditHooksAreGated`** — 3 tests covering eod_breakout.py,
  scanner.py, order_generator.py. Asserts:
  - `audit_mode = bool(context.get("audit_mode", False))` setup line is
    present.
  - Each collector-emission line is preceded by an
    `if audit_mode and audit_collector is not None` (or `self.` variant)
    guard.
  - `_walk_forward_tsl` returns reason on each branch (anomalous_drop,
    trailing_stop, regime_flip).
  - HOOK F's at-entry context fields (`entry_close_signal`,
    `entry_n_day_high`) are present in source.

- **`TestAuditCollectorContract`** — 2 tests pinning the collector keys:
  - eod_breakout: `{scanner_snapshots, entry_audits, trade_log_audits}`.
  - eod_technical legacy path: `{scanner_reject_summaries, entry_audits,
    trade_log_audits}`.
  - Renaming silently breaks downstream → fail loud.

### Runtime regression (opt-in, slow)

- **`TestAuditModeOff`** — 2 tests, one per champion. Runs the pipeline
  with no audit context, diffs the result vs
  `champion_pre_audit_baseline.json` on summary, trades, equity_curve,
  monthly/yearly returns, costs.

- **`TestAuditModeOn`** — 2 tests, one per champion. Monkey-patches
  `generate_orders` to inject `audit_mode=True` + stub collector via
  context, diffs the same sections.

Gated by env var `STRATEGY_BACKTESTER_AUDIT_REGRESSION=1` so the standard
test loop stays fast.

### Verification

- `python3 -m unittest discover tests` → **435/435 OK** (4 skipped — slow
  tests; up from 426 with 9 new methods, took 1.29s total).
- `STRATEGY_BACKTESTER_AUDIT_REGRESSION=1 python3 -m unittest tests.test_audit_noninvasive`
  → **9/9 OK in 159s** (4 champion runs + 5 static asserts).

---

## Phase 2e — what landed

`scripts/run_audit_drill.py` (~330 lines) + two on-disk audit-drill output
directories (gitignored under `results/`).

### Runner design

Monkey-patches `generate_orders` on the strategy's signal generator to
inject `audit_mode=True` + a stub collector via context. After
`pipeline.run_pipeline` returns, the runner:

1. Concatenates collector lists into single DataFrames.
2. Tags each with `strategy` and `config_id` columns.
3. Adds `period` (IS/OOS) via `audit_io.add_period_column`.
4. Writes parquets (zstd) to `results/<strategy>/audit_drill_<UTC-iso>/`.
5. Computes filter_marginals via `audit_io.compute_filter_marginals`.
6. Writes the simulator's trade_log + equity_curve as separate parquets
   (different from collector trade_log_audit — see schema note).
7. Emits README.md + run_metadata.json via `audit_io.write_audit_readme`.

### Schema deviation from `lib/audit_io.py` (intentional)

The strict `ENTRY_AUDIT_SCHEMA` / `TRADE_LOG_SCHEMA` /
`DAILY_SNAPSHOT_SCHEMA` in `lib/audit_io.py` carry post-enrichment fields
(`regime_state`, `ds_at_entry`, `quantity`, `hold_days`, `final_picked`)
that the raw hook output doesn't include. This Phase-2e runner emits the
raw collector DataFrames *without* trying to conform to the strict schema;
strict-schema migration is left for Phase 3 if needed.

`compute_filter_marginals` and `add_period_column` work directly on the
raw output — those helpers are used.

`build_daily_snapshot` requires `quantity` on the trade log (only present
on the simulator's trade_log, not the collector's audit dicts) — deferred.

### Audit drill outputs

| Strategy | Run dir | Artifacts | Total size |
|---|---|---:|---:|
| eod_breakout | `results/eod_breakout/audit_drill_20260428T124754Z/` | 8 files | ~7.2MB |
| eod_technical | `results/eod_technical/audit_drill_20260428T124832Z/` | 8 files | ~6.3MB |

Each directory contains:
- `entry_audit.parquet` — per-row clause flags + `all_clauses_pass`.
- `trade_log_audit.parquet` — per (entry, exit) with at-entry context +
  exit_reason. Pre-simulator (full unconstrained candidate set).
- `scanner_snapshot.parquet` (eod_b) or `scanner_reject_summary.parquet`
  (eod_t) — universe-side scanner output.
- `filter_marginals.parquet` — per-clause pass-rate-alone, in-combination,
  and conditional_fail_rate (binding measure).
- `simulator_trade_log.parquet` — capacity-constrained trades from the
  simulator (the 1795 / 1303 actual fills).
- `equity_curve.parquet` — daily NAV.
- `README.md` — human-readable run summary.
- `run_metadata.json` — machine-readable metadata.

### Findings already surfaced from the artifacts

These came out of casual inspection of the parquets — Phase 3 will dig
deeper, but a few things are already clear:

#### 1. Scanner-pass dominates entry filtering on both strategies.

`conditional_fail_rate` (P(clause fails | all OTHERS pass)):

| Clause | eod_breakout | eod_technical |
|---|---:|---:|
| `clause_scanner_pass` | **71.93%** | **69.45%** |
| `clause_regime_bullish` | 21.90% | (gate disabled) |
| `clause_ds_gt_thr` | 15.90% | 41.64% |
| `clause_close_ge_ndhigh` | 13.92% | 31.27% |
| `clause_close_gt_open` | 15.87% | 15.30% |
| `clause_close_gt_ma` | 15.58% | **0.13%** |
| `clause_next_*_present` | 0.00% | (n/a) |

Implication: **scanner-side liquidity filter is doing most of the work**
on both strategies. Loosening or tightening it is the highest-leverage
parameter.

#### 2. eod_t's `close > n_day_ma` clause is essentially redundant.

`conditional_fail_rate = 0.13%` for eod_t means: when all other clauses
(`close ≥ n_day_high`, `close > open`, `scanner_pass`, `ds > thr`) pass,
the `close > n_day_ma` filter additionally rejects only 0.13%. The
`close ≥ n_day_high` clause already dominates this. **Phase-4-worthy
hypothesis:** drop the MA filter; expect similar or slightly broader
entry set.

This is materially different from eod_b (15.58%) — eod_b's MA window
(10-day) and high window (3-day) are different, so the MA isn't subsumed.

#### 3. IS/OOS regime mix flipped in 2025 (eod_b).

eod_b exit_reason × period:

| period | trailing_stop | regime_flip | anomalous_drop |
|---|---:|---:|---:|
| IS (2010-2024) | 132,722 (76.8%) | 39,543 (22.9%) | 450 (0.3%) |
| OOS (2025+) | 19,913 (57.4%) | 14,789 (42.6%) | 18 (0.05%) |

`regime_flip` share nearly doubled in 2025. Could be a 2025-specific
regime instability (Nifty volatility, repeated SMA crosses) or simply
fewer trades reaching TSL before the regime force-exit fired. Worth a
focused query in Phase 3.

#### 4. eod_t accumulates open positions at sim-end.

eod_t lacks regime force-exit, so positions still open at the last bar
get closed via `end_of_data`:

| period | trailing_stop | end_of_data | anomalous_drop |
|---|---:|---:|---:|
| IS | 159,111 (99.4%) | 259 (0.16%) | 653 (0.4%) |
| OOS | 31,338 (94.0%) | 1,904 (5.7%) | 69 (0.2%) |

OOS sees 7× more end_of_data than IS proportionally — last-bar tail is
heavy in 2025. Likely 2025 had a sustained rally without enough TSL
breaches to flush positions before sim end (2026-03-19).

#### 5. Scanner pass rate jumped from 24.1% (IS) to 41.2% (OOS).

eod_t scanner aggregates by period:

| period | candidates | passes | pass_rate |
|---|---:|---:|---:|
| IS (3817 days) | 5,029,492 | 1,212,120 | 24.10% |
| OOS (300 days) | 690,404 | 284,448 | 41.20% |

NSE universe got more liquid relative to the static price/turnover
thresholds in 2025. The thresholds were calibrated on older data and
have inflated in real-impact terms.

#### 6. `n_day_gain_rejects = 0` (confirmed already in pt8).

Champion's `n_day_gain_threshold:-999` rejects nothing — the filter is
parsed but inert. PIPELINE_MAP open question #1 settled.

---

## Phase 3 spec (next session)

### 3a — OHLCV-fetch helper (`scripts/fetch_ohlcv_window.py`)

For Phase 3c spot-checks (manual trace of 20 entries + 5 capacity-blocked
per strategy). Pulls OHLCV ± N bars around an event for cheap inspection.

Out of scope: data-pipeline integration. Just a thin wrapper around the
existing tick-data provider that takes (instrument, epoch, window_days)
and returns a small DataFrame.

### 3b — Per-strategy sanity checks

For each strategy, write SQL/polars queries that verify:
- entry-day clause-flag pass-rates match across (entry_audit, simulator
  trade_log, scanner_snapshot).
- exit_reason distribution matches what we already saw in pt7/pt8.
- Cross-foot: `passed_in_combination` ≈ count(can_enter==True).
- IS/OOS split is monotone in the obvious direction (more candidates per
  day in OOS for both strategies — already confirmed).

### 3c — Spot-check audits (manual trace)

20 entries + 5 capacity-blocked per strategy. Walk one trade end-to-end
through the pipeline, comparing the audit row to the simulator's trade
record. Document discrepancies.

### 3d — Pattern queries (10 queries, IS/OOS split)

Per-strategy, IS vs OOS partitioned. Examples:
- Top-10 instruments by trade count, alpha, drawdown contribution.
- Day-of-week / seasonality breakdown.
- Hold-time distribution by exit_reason.
- Direction-score histogram of entries.
- Regime-state distribution at entry (eod_b only — eod_t doesn't gate).
- Scanner-pass distribution by month (smooth or stepwise?).
- Trade-pnl distribution by exit_reason.
- Capacity-blocked entries (audit_mode entries with no simulator
  trade): how many, which instruments.

### 3e — Cross-strategy notes (NOT comparison)

Per pt6 framing: descriptive only. Surface where each strategy is
strong/weak on its own terms. Not apples-to-apples.

### 3f — FINDINGS docs

`docs/inspection/FINDINGS_eod_breakout.md`,
`FINDINGS_eod_technical.md`, `FINDINGS_cross.md`. Each summarizes the
phase-3 queries + tags improvement hypotheses for Phase 4.

---

## Resume sequence (next session)

1. Read this doc (pt9).
2. Read the audit drill READMEs:
   - `results/eod_breakout/audit_drill_20260428T124754Z/README.md`
   - `results/eod_technical/audit_drill_20260428T124832Z/README.md`
3. Begin Phase 3a: build `scripts/fetch_ohlcv_window.py`.
4. Then 3b/3c/3d in order; use the parquet artifacts directly via polars.
5. Land a draft `FINDINGS_eod_breakout.md` after 3d for that strategy.

---

## Time-box

| Phase | Budget | Spent | Cumulative remaining |
|---|---|---|---|
| 0+1+2a-2c | (covered earlier) | ~4.25 hrs | (done) |
| 2d — regression test | (in 2's budget) | ~30 min | (done) |
| 2e — audit run | (in 2's budget) | ~45 min | ~6 hrs Phase 2 budget remaining (unused — Phase 2 came in well under budget) |
| 3 — inspect | 4 hrs | — | 4 hrs |
| **Hard stop** | 18.5 hrs | ~5.5 hrs spent | ~13 hrs remaining |

---

## Working state at end of session

- Tree clean post-commit.
- `tests/test_audit_noninvasive.py`: 9 tests, all pass (4 slow gated).
- `scripts/run_audit_drill.py`: runs end-to-end for both champions in
  ~37 seconds each.
- Both audit drill output dirs on disk under `results/`.
- Test suite: 435/435 pass (4 slow skipped).
- 16 commits unpushed across pt2-pt9.

---

## Commit list (drill so far)

| Commit | Title |
|---|---|
| `22efb42` | Inspection drill 2026-04-28 pt5 (Phases 0+1): plan + pipeline map |
| `2829a49` | Inspection drill 2026-04-28 pt5 (Phase 2a): lib/audit_io.py writer module |
| `98d85da` | Session handover 2026-04-28 pt6: Phases 0+1+2a checkpoint |
| `f6ca734` | Inspection drill 2026-04-28 pt7 (Phase 2b): eod_b audit hooks |
| `813a89d` | Inspection drill 2026-04-28 pt8 (Phase 2c): eod_t legacy-path audit hooks |
| `9c635ff` | Inspection drill 2026-04-28 (Phase 2d): audit-noninvasive regression test |
| (this) | Inspection drill 2026-04-28 pt9 (Phase 2e): audit-drill runner + artifacts |

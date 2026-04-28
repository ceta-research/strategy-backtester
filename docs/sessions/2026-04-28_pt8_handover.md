# Session handover — 2026-04-28 pt8 (Phase 2c complete)

**Predecessor:** [`2026-04-28_pt7_handover.md`](2026-04-28_pt7_handover.md) — Phase 2b checkpoint.

Phase 2c (audit hooks across the eod_t legacy path) is done and verified.
Phase 2d (regression test) and 2e (audit run) are next.

---

## TL;DR

Phase 2c shipped. eod_t champion runs identically with hooks in
`engine/scanner.py` + `engine/order_generator.py` (both `audit_mode=False`
and `audit_mode=True` produce byte-identical trades and equity curve to the
pinned `champion_pre_audit_baseline.json`). 426/426 tests pass. eod_b also
re-verified byte-identical (it doesn't share these files but defensively
confirmed). Tree clean.

| Phase | Status | Commit | Key artifact |
|---|---|---|---|
| 0 — pin baselines | ✅ | `22efb42` | `results/<strategy>/champion_pre_audit_baseline.json` |
| 1 — pipeline map | ✅ | `22efb42` | `docs/inspection/PIPELINE_MAP.md` |
| 2a — audit_io module | ✅ | `2829a49` | `lib/audit_io.py` |
| 2b — eod_b hooks | ✅ | `f6ca734` | `engine/signals/eod_breakout.py` |
| 2c — eod_t hooks | ✅ | (this commit) | `engine/scanner.py`, `engine/order_generator.py` |
| 2d — regression test | pending | — | `tests/test_audit_noninvasive.py` |
| 2e — audit run | pending | — | `audit_drill_<ts>/` artifacts |
| 3 + 4 | pending | — | (later sessions) |

---

## Phase 2c — what landed

Two files modified, ~95 lines added across 5 hook sites. All hook bodies
gated by `audit_mode and audit_collector is not None`. Default behavior
unchanged.

### `engine/scanner.py`

#### audit_mode setup (top of `process`)

Reads `audit_mode` and `audit_collector` from context; per-scanner-config
loop branches on these for emission.

#### HOOK B — per-clause reject summary (after line 134 drop_nulls)

For each scanner_config iteration, computes per-row clause-pass booleans on
a sibling DataFrame (df_tick_data is unchanged) for the 3 filter clauses
(price, avg_txn, n_day_gain), then groups by `date_epoch` and emits one row
per (date, scanner_config) with: `candidate_count`, `price_rejects`,
`avg_txn_rejects`, `n_day_gain_rejects`, `pass_count`.

Collector key: `audit_collector["scanner_reject_summaries"]` → list of
polars DataFrames.

### `engine/order_generator.py`

#### `OrderGenerationUtil.__init__` — accept audit args

New constructor signature: `OrderGenerationUtil(df, audit_mode=False,
audit_collector=None)`. Stores both, plus an internal `_audit_at_entry`
dict used by HOOK F to build trade-level audit rows.

#### HOOK C — clause mirror cols in `add_entry_signal_inplace`

`can_enter` is now built from a named `can_enter_expr` rather than inline
inside `with_columns`. When `audit_mode=False`, the only column added is
`can_enter` and the resulting expression is identical to the pre-hook
version. When `audit_mode=True`, 5 additional clause boolean columns are
added in the same `with_columns` call (`clause_close_gt_ma`,
`clause_close_ge_ndhigh`, `clause_close_gt_open`, `clause_scanner_pass`,
`clause_ds_gt_thr`).

#### HOOK D — entry_audits + at-entry context capture in `update_config_order_map`

Before the `df_tick_data.filter(pl.col("can_enter"))` line, when
`audit_mode=True`:

1. Emits a slice of df with `[instrument, date_epoch, clause_*,
   all_clauses_pass, entry_config_id]` (where `all_clauses_pass = can_enter`)
   to `audit_collector["entry_audits"]`.
2. For rows with `can_enter==True`, captures at-entry context (close,
   n_day_high, direction_score) into `self._audit_at_entry`, keyed by
   `(instrument, next_epoch, entry_config_id)` (next_epoch = the actual
   entry epoch under MOC convention).

#### HOOK F — post-Pool join in `process`

After `generate_exit_attributes` returns (Pool aggregated), walks
`order_config_mapping` × `_audit_at_entry` and emits combined per-trade
audit rows to `audit_collector["trade_log_audits"]`. Each row carries:
`instrument, entry_epoch, exit_epoch, entry_price, exit_price, exit_reason`
(from order_config_mapping) and `entry_close_signal, entry_n_day_high,
entry_direction_score` (from at-entry context, joined on
(instrument, entry_epoch, first-entry-config-id)).

`exit_reason` comes from the existing `_record_exit` mechanism — no
worker-side instrumentation was needed.

### `engine/signals/eod_technical.py`

No changes. The wrapper passes context through unchanged; downstream
`scanner.process` and `order_generator.process` read `audit_mode` and
`audit_collector` directly from context. The slow path (`_run_per_config`)
is not exercised by the champion — its audit instrumentation is deferred
per PIPELINE_MAP guidance.

---

## Verification

### `audit_mode=False` smoke tests (mandatory)

| Strategy | summary | trades | equity_curve | monthly | yearly | costs |
|---|---|---|---|---|---|---|
| eod_technical | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| eod_breakout (defensive recheck) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

Trade counts: eod_t = 1303 / 1303, eod_b = 1795 / 1795.

### `audit_mode=True` smoke tests

| Strategy | summary | trades | equity_curve | monthly | yearly | costs |
|---|---|---|---|---|---|---|
| eod_technical | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| eod_breakout (already covered in pt7) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

Multiprocessing nondeterminism: per pt6 open-question #2, the natural diff
passed without forcing `multiprocessing_workers=1`. Champion is
single-config; `generate_order_df()` sort by `[instrument, entry_epoch,
exit_epoch]` is unique-per-row and produces deterministic output.

### Test suite

`python3 -m unittest discover tests` → **426/426 pass**.

### Spot-check distributions (eod_t audit_mode=True)

`exit_reason` over 193,334 unconstrained orders:

| Reason | Count | % |
|---|---:|---:|
| trailing_stop | 190,449 | 98.5% |
| end_of_data | 2,163 | 1.1% |
| anomalous_drop | 722 | 0.4% |
| regime_flip | 0 | 0.0% |

`regime_flip = 0` is **correct**: champion eod_t has regime disabled
(no `regime_instrument` set). Confirms force-exit path didn't fire.

Clause pass-rates over 5,637,686 candidate rows:

| Clause | % pass |
|---|---:|
| `clause_close_gt_ma` | 47.36 |
| `clause_close_gt_open` | 40.04 |
| `clause_ds_gt_thr` | 38.08 |
| `clause_scanner_pass` | 26.31 |
| `clause_close_ge_ndhigh` | 25.81 |
| **`all_clauses_pass`** | **3.44** |

Scanner reject summary aggregates (sum across 4117 daily rows):

| Field | Count | Notes |
|---|---:|---|
| candidate_count | 5,719,896 | Pre-trim universe |
| price_rejects | 1,749,004 | 30.6% rejected by price filter |
| avg_txn_rejects | 4,111,213 | 71.9% rejected by liquidity filter |
| **`n_day_gain_rejects`** | **0** | **Empirically confirms PIPELINE_MAP open question #1**: champion's `n_day_gain_threshold:-999` rejects nothing. |
| pass_count | 1,496,568 | 26.16% pass |

---

## Findings surfaced this phase

1. **`n_day_gain_threshold:-999` is empirically a no-op for eod_t champion.**
   Scanner reject summary: 0 rejects across the full sim range. Confirms
   PIPELINE_MAP open question #1 (parsed-but-disabled). The filter is
   deferred for Phase 3 sanity check.

2. **eod_t exit_reason has a non-trivial `end_of_data` tail (1.1% = 2,163
   trades).** eod_b champion had 0 `end_of_data` because its regime gate
   force-exits open positions before the final bar; eod_t lacks regime
   force-exit, so positions still open at the last bar are closed at close.
   This is expected and not a bug.

3. **`clause_scanner_pass` for eod_t at the order-gen step (26.31%) closely
   matches the scanner-side `pass_count / candidate_count` (26.16%).**
   The 0.15pp gap is from start_epoch trimming + last-bar `next_epoch IS NOT
   NULL` filter applied between scanner and order-gen — populations differ
   slightly but pass rates are consistent.

---

## Phase 2d spec (next session)

Write `tests/test_audit_noninvasive.py` codifying the regression we just
ran manually. Two tests per strategy:

1. `audit_mode=False` on the hooked code → diff vs
   `champion_pre_audit_baseline.json` must be zero on summary + trades +
   equity_curve.
2. `audit_mode=True` (stub collector) → diff vs (1) must be zero.

Run for both eod_breakout and eod_technical.

If either diff is non-zero, hooks have side effects → block Phase 3.

The eod_t test must use `if __name__ == "__main__":` guard / proper test
module structure because the worker pool uses spawn mode on Python 3.13 +
macOS (heredoc/stdin entry-points fail with `FileNotFoundError: <stdin>`
during re-import). Existing test files all follow unittest convention,
so this should be a non-issue.

## Phase 2e spec (after 2d)

Build a runner wrapper that:
- For each champion: instantiates an audit_collector dict, calls
  `pipeline.run_pipeline` with `audit_mode=True`, then calls the
  `lib/audit_io.py` writers to emit parquet artifacts.
- Output dir: `results/<strategy>/audit_drill_<UTC-iso>/` (per
  `lib/audit_io.py:make_audit_dir`).
- Artifacts: `entry_audit.parquet`, `trade_log.parquet`,
  `daily_snapshot.parquet`, `scanner_reject_summary.parquet`,
  `filter_marginals.parquet`, `audit_README.md`.

The runner can live at `scripts/run_audit_drill.py`. It plumbs audit_mode
into context before invoking pipeline, then collects + writes parquets.

---

## Open questions still un-decided

1. **Comparison baseline (carried from pt5).** Whether to also instrument
   the prior champion (pre-regime, pre-holdout) for eod_b to attribute the
   +35.24pp 2025 improvement. Recommendation: skip unless Phase 3 surfaces
   a question this would answer.

2. **Phase 4 timing.** Plan says Phase 4 is a separate session. Confirmed
   at end of Phase 3.

---

## Time-box

| Phase | Budget | Spent | Cumulative remaining |
|---|---|---|---|
| 0 — prep | 1 hr | ~30 min | (done) |
| 1 — map | 2 hrs | ~1 hr | (done) |
| 2a — audit_io | (in 2's budget) | ~1 hr | (done) |
| 2b — eod_b hooks | (in 2's budget) | ~1 hr | (done) |
| 2c — eod_t hooks | (in 2's budget) | ~45 min | ~7 hrs Phase 2 budget remaining |
| 2d-2e | (in 2's budget) | — | |
| 3 — inspect | 4 hrs | — | 4 hrs |
| **Hard stop** | 18.5 hrs | ~4.25 hrs spent | ~14 hrs remaining |

---

## Resume sequence (next session)

1. Read this doc (pt8).
2. Begin Phase 2d:
   a. Create `tests/test_audit_noninvasive.py` with 4 tests
      (`audit_mode_off_eod_b`, `audit_mode_off_eod_t`,
      `audit_mode_on_eod_b`, `audit_mode_on_eod_t`).
   b. Each test: load baseline JSON, run pipeline with appropriate
      audit_mode, diff `summary` + `trades` + `equity_curve`, fail on
      mismatch.
   c. Run the suite; confirm all 4 pass.
3. If 2d green, build `scripts/run_audit_drill.py` (Phase 2e runner) +
   produce audit parquets for both champions.
4. Then Phase 3 (inspection of audit artifacts).

---

## Working state at end of session

- Tree clean (post-commit).
- `engine/scanner.py` has HOOK B (per-clause reject summary).
- `engine/order_generator.py` has HOOK C/D (entry_audits + at-entry
  capture) and HOOK F (post-Pool join into trade_log_audits) plus
  audit-aware constructor.
- `engine/signals/eod_breakout.py` carries pt7 hooks (HOOK 1/2/3/3a).
- Both baselines reproducible with hooks present.
- Test suite: 426/426 pass.
- 14 commits unpushed across pt2-pt8.

---

## Commit list (drill so far)

| Commit | Title |
|---|---|
| `22efb42` | Inspection drill 2026-04-28 pt5 (Phases 0+1): plan + pipeline map |
| `2829a49` | Inspection drill 2026-04-28 pt5 (Phase 2a): lib/audit_io.py writer module |
| `98d85da` | Session handover 2026-04-28 pt6: Phases 0+1+2a checkpoint |
| `f6ca734` | Inspection drill 2026-04-28 pt7 (Phase 2b): eod_b audit hooks |
| (this) | Inspection drill 2026-04-28 pt8 (Phase 2c): eod_t legacy-path audit hooks |

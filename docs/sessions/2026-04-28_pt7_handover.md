# Session handover — 2026-04-28 pt7 (Phase 2b complete)

**Predecessor:** [`2026-04-28_pt6_handover.md`](2026-04-28_pt6_handover.md) — Phases 0+1+2a checkpoint.

This is a **progress checkpoint**. Plan in pt5 is still current. Phase 2b
(audit hooks in `engine/signals/eod_breakout.py`) is done and committed.
Phase 2c (eod_t legacy path) starts next.

---

## TL;DR

Phase 2b shipped. eod_b champion runs identically with hooks present (both
`audit_mode=False` and `audit_mode=True` produce byte-identical trades and
equity curve to the pinned `champion_pre_audit_baseline.json`). 426/426 tests
pass. Tree clean.

| Phase | Status | Commit | Key artifact |
|---|---|---|---|
| 0 — pin baselines | ✅ | `22efb42` | `results/<strategy>/champion_pre_audit_baseline.json` |
| 1 — pipeline map | ✅ | `22efb42` | `docs/inspection/PIPELINE_MAP.md` |
| 2a — audit_io module | ✅ | `2829a49` | `lib/audit_io.py` |
| 2b — eod_b hooks | ✅ | (this commit) | `engine/signals/eod_breakout.py` (4 hooks) |
| 2c — eod_t hooks | pending | — | (next session) |
| 2d — regression test | pending | — | `tests/test_audit_noninvasive.py` |
| 2e — audit run | pending | — | `audit_drill_<ts>/` artifacts |
| 3 + 4 | pending | — | (later sessions) |

---

## Phase 2b — what landed in `engine/signals/eod_breakout.py`

Total ~70 lines added; all observation-only. Default behavior unchanged.

### audit_mode setup (lines 51-57)

```python
audit_mode = bool(context.get("audit_mode", False))
audit_collector = context.get("audit_collector") if audit_mode else None
```

`audit_collector` is a dict supplied by the runner/wrapper. Hooks append into
well-known keys.

### HOOK 1 — post-scanner snapshot (lines 62-73)

Emits `(instrument, date_epoch, scanner_config_ids, scanner_pass)` from
`df_trimmed`. `scanner_config_ids IS NULL` ⟹ scanner-rejected.

Collector key: `audit_collector["scanner_snapshots"]` → list of polars
DataFrames.

### HOOK 2 — per-clause flags (lines 211-255)

Mirrors the entry_filter AND as separate boolean columns. Always emits the 7
core clauses; appends `clause_regime_bullish` when `use_regime`; appends
`clause_vol_below_cap` when `vol_filter_active`. Computes
`all_clauses_pass = AND(clause_*)` to avoid downstream re-derivation.

Collector key: `audit_collector["entry_audits"]` → list of polars DataFrames
with columns `[instrument, date_epoch, clause_..., all_clauses_pass]`.

### HOOK 3 — per-trade row with at-entry context (lines 349-368)

Captures: `entry_close_signal`, `entry_n_day_high`, `entry_direction_score`,
`entry_regime_bullish` (None when regime gate disabled), and `exit_reason`.
At-entry context is piggy-backed onto `entry_rows` via a conditional
`select_cols` extension at line 261-263 (audit-only columns: `close`,
`n_day_high`, `direction_score`).

Collector key: `audit_collector["trade_log_audits"]` → list of dicts.

### HOOK 3a — `_walk_forward_tsl` returns 3-tuple (lines 408-482)

Signature change (always returns `(exit_epoch, exit_price, exit_reason)`):

| Return site | Reason tag |
|---|---|
| `anomalous_drop` decision | `"anomalous_drop"` |
| Last bar | `"end_of_data"` |
| Regime flip (next-day open / same-bar fallback) | `"regime_flip"` |
| TSL trigger (next-day open / same-bar fallback) | `"trailing_stop"` |
| Loop exhaust | `"end_of_data"` |
| Empty data | `None` (with `(None, None)`) |

Caller at line 322 unpacks 3 values. The reason has no behavioral effect on
the returned epoch/price; it's a free-with-purchase audit hook.

---

## Verification (regression evidence)

### `audit_mode=False` smoke test (mandatory)

Ran `python3 run.py --config strategies/eod_breakout/config_champion.yaml`
with no audit context. Diff vs `champion_pre_audit_baseline.json`:

```
summary identical: True
trades identical: True
equity_curve identical: True
monthly_returns identical: True
yearly_returns identical: True
costs identical: True
benchmark identical: True
comparison identical: True

trade counts: 1795 1795
eq counts: 5922 5922
```

### `audit_mode=True` smoke test (additional)

Same config with a stub collector injected via monkey-patch on
`generate_orders`. Diff vs baseline:

```
summary identical: True
trades identical: True
equity_curve identical: True
monthly_returns identical: True
yearly_returns identical: True
costs identical: True
```

Hooks populate cleanly:

| Collector key | Shape |
|---|---|
| `scanner_snapshots` | 1 × DataFrame (5,640,140 × 4) |
| `entry_audits` | 1 × DataFrame (5,637,686 × 11) |
| `trade_log_audits` | 207,435 × dict |

### Test suite

`python3 -m unittest discover tests` → **426/426 pass**.

### Spot-check distributions

`exit_reason` distribution (across the 207,435 unconstrained orders, before
simulator capacity):

| Reason | Count | % |
|---|---:|---:|
| trailing_stop | 152,635 | 73.6% |
| regime_flip | 54,332 | 26.2% |
| anomalous_drop | 468 | 0.2% |
| end_of_data | 0 | 0.0% |

Matches PIPELINE_MAP expectation: heavy TSL, meaningful regime_flip given the
regime gate is on. `end_of_data=0` because regime force-exit triggers before
the loop exhausts for any open position.

Clause pass-rates over the 5.64M candidate (instr × date) rows:

| Clause | % pass |
|---|---:|
| `clause_next_epoch_present` | 100.00 |
| `clause_next_open_present` | 100.00 |
| `clause_regime_bullish` | 70.29 |
| `clause_ds_gt_thr` | 63.27 |
| `clause_close_gt_ma` | 47.99 |
| `clause_close_gt_open` | 40.04 |
| `clause_close_ge_ndhigh` | 36.11 |
| `clause_scanner_pass` | 23.62 |
| **`all_clauses_pass`** | **3.68** |

Independent sanity: `clause_scanner_pass` (23.62%) matches scanner_snapshot
pass rate (23.62%) exactly — two independent hooks see the same scanner
output. ✅

---

## Phase 2c spec (next session opening)

Per PIPELINE_MAP `Audit-hook placement summary` → eod_technical legacy path.
~85 lines spread across 3 files.

### Targets

1. **`signals/eod_technical.py:_run_no_regime`** — instantiate audit collector
   on context; write parquets after scanner.process + order_generator.process
   return. ~15 lines.
2. **`engine/scanner.py:process` (lines 134, 141-143, 161)** — per-clause
   reject counts (price / avg_txn / n_day_gain). Emit one summary row per
   (date, scanner_config_id) into `scanner_reject_summary` collector key.
   ~15 lines.
3. **`engine/order_generator.py:add_entry_signal_inplace` (lines 67-75)** —
   per-clause flag mirror columns alongside `can_enter`. ~20 lines.
4. **`engine/order_generator.py:update_config_order_map` (line 78)** — emit
   all candidates (passed + failed) before the `can_enter==True` filter.
   ~10 lines.
5. **`engine/order_generator.py:generate_exit_attributes_for_instrument`** —
   collect at-entry context per trade; return as a third element in the
   result tuple alongside `(instrument, instrument_order_config)`. ~15 lines.
6. **`engine/order_generator.py:generate_exit_attributes`** — aggregate audit
   tuples from Pool results. ~10 lines.

### Multiprocessing constraint

Per PIPELINE_MAP open-question #2: champion is single-config so the natural
diff is *expected* to pass even with `multiprocessing_workers > 1` (the
`generate_order_df()` sort at line 153 is unique-per-row). **Plan:** try
natural diff first. If it fails, force `multiprocessing_workers=1` for the
audit run only.

eod_t already emits `exit_reason` natively into `order_attributes` via
`_record_exit` (lines 290-319 of order_generator.py). No HOOK 3a equivalent
is needed — eod_t is *easier* on this axis than eod_b.

### Smoke-test rule (carried from pt6)

Any code change must reproduce `results/eod_technical/champion_pre_audit_baseline.json`
byte-identically with `audit_mode=False` before adding any `audit_mode=True`
paths. Then a second pass with `audit_mode=True` must also be byte-identical
on trades + equity_curve.

---

## Phase 2d preview (test file)

Once 2c lands, write `tests/test_audit_noninvasive.py`:

1. Run both champions with `audit_mode=False` on the hooked code → diff vs
   `champion_pre_audit_baseline.json` must be zero on summary + trades +
   equity_curve.
2. Run both champions with `audit_mode=True` (stub collector) → diff vs (1)
   must be zero.

If either diff is non-zero, hooks have side effects → block Phase 3.

---

## Time-box

| Phase | Budget | Spent | Cumulative remaining |
|---|---|---|---|
| 0 — prep | 1 hr | ~30 min | (done) |
| 1 — map | 2 hrs | ~1 hr | (done) |
| 2a — audit_io | (in 2's budget) | ~1 hr | (done) |
| 2b — eod_b hooks | (within 10 hrs P2 total) | ~1 hr | ~8 hrs Phase 2 budget remaining |
| 2c-2e | (within 10 hrs P2 total) | — | |
| 3 — inspect | 4 hrs | — | 4 hrs |
| **Hard stop** | 18.5 hrs | ~3.5 hrs spent | ~15 hrs remaining |

---

## Resume sequence (next session)

1. Read this doc (pt7).
2. Skim PIPELINE_MAP "Audit-hook placement summary → eod_technical legacy
   path" + the per-file rows for `scanner.py` and `order_generator.py`.
3. Begin Phase 2c. Suggested order:
   a. Add audit_mode/collector plumbing to
      `signals/eod_technical.py:_run_no_regime`.
   b. Hook scanner.py per-clause reject counts.
   c. Hook order_generator.py: clause mirror cols, pre-filter candidate
      emit, per-trade at-entry context return.
   d. Smoke-test `audit_mode=False` against
      `results/eod_technical/champion_pre_audit_baseline.json` byte-identically.
   e. Smoke-test `audit_mode=True` ditto.
4. If all green, proceed to Phase 2d (write the regression test) then 2e
   (run both champions with `audit_mode=True` and emit parquets via
   `lib/audit_io.py`).

---

## Working state at end of session

- Tree clean.
- `engine/signals/eod_breakout.py` has 4 audit hooks, gated by
  `audit_mode and audit_collector is not None`.
- Both baselines reproducible at `3457840` engine state with hooks present.
- Test suite: 426/426 pass.
- 11 commits unpushed across pt2-pt7.

---

## Commit list (pt5+pt6+pt7 sessions)

| Commit | Title |
|---|---|
| `22efb42` | Inspection drill 2026-04-28 pt5 (Phases 0+1): plan + pipeline map |
| `2829a49` | Inspection drill 2026-04-28 pt5 (Phase 2a): lib/audit_io.py writer module |
| `98d85da` | Session handover 2026-04-28 pt6: Phases 0+1+2a checkpoint |
| (this) | Inspection drill 2026-04-28 pt7 (Phase 2b): eod_b audit hooks |

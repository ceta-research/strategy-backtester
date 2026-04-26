# P2 Execution Plan — Strategy-Backtester Audit Sprint

**Created:** 2026-04-21
**Scope:** All 49 open `[ ] P2` items in `docs/AUDIT_CHECKLIST.md`.
**Assumes:** Phases 1-7 landed. Regression snapshots in `tests/regression/snapshots/` are authoritative.
**Guardrail (unchanged from P1 plan):** every fix must either (a) leave regression snapshots byte-identical or (b) ship an explicit snapshot-update commit documenting the delta in `docs/AUDIT_FINDINGS.md`.

---

## 1. Pre-work: Decisions (resolved 2026-04-21)

Four P2 items are semantic / methodology decisions. Resolutions:

### D1. Sharpe definition — **RESOLVED: emit both**
Ship `sharpe_geometric` (current `(cagr - rf) / vol`) and `sharpe_arithmetic` (annualized arithmetic mean excess / vol). Default `print_summary` to arithmetic (matches QuantStats / PyPortfolioOpt / textbooks). Keep geometric for backward-compat leaderboard continuity. Snapshot delta unavoidable; documented in `AUDIT_FINDINGS.md`. Applies to Batch 1.

### D2. Intraday v1 deprecation — **RESOLVED: deprecate**
Grep YAMLs for `pipeline_version: v1`. If zero active configs, emit `DeprecationWarning` at import of `engine/intraday_simulator.py` + `engine/intraday_sql_builder.py` v1 path; gate removal in next minor. No snapshot impact (all snapshots are EOD). Applies to Batch 8.

### D3. Margin-interest cost model — **RESOLVED: document only**
Not implemented this sprint. Add validator that warns when `order_value_multiplier > 1` combined with multi-day holds. Document "known systematic overestimate" in AUDIT_FINDINGS.md. Revisit in dedicated cost-model-realism sprint.

### D4. Dividend income model — **RESOLVED: document only**
Not implemented this sprint. Document impact in AUDIT_FINDINGS.md. Revisit in dedicated cost-model-realism sprint.

---

## 2. Batching overview

49 items → 8 batches. Earlier batches de-risk later ones. Each batch is one focused commit (with 1-3 follow-ups if snapshots must update).

| # | Theme | Items | Effort | Snapshot delta? |
|---|-------|-------|--------|-----------------|
| 1 | Metrics definitions & edge cases | 6 | M | **Yes** (D1) |
| 2 | Pipeline / simulator hygiene & config safety | 7 | M | No |
| 3 | Scanner & data-provider correctness | 6 | M | Maybe |
| 4 | Ranking & signal semantics | 13 | M | Small |
| 5 | Charges realism (per-exchange schedules, slippage) | 3 | L | **Yes** (cross-exchange) |
| 6 | Test hardening / property tests / coverage | 9 | M | No |
| 7 | Performance hotspots | 4 | L | No (must prove byte-identical) |
| 8 | Deprecation / cleanup | 7 | S | No |

**Dependencies:** 1 → 6 (property tests pin the new metric definitions). 2 → 4 (simulator/config fixes must land before signal re-examination). 3 → 5 (data drops affect charge totals). 7 runs last (must prove determinism against snapshots, so needs every earlier batch stable).

---

## 3. Batch details

### Batch 1 — Metrics definitions & edge cases

**Includes:**
- L41 `metrics.py:113` — `dd = (cumulative - peak) / peak if peak > 0 else 0` (total-wipeout returns 0, not -1).
- L42 `metrics.py:156-158` — VaR index vs `numpy.percentile`.
- L43 `metrics.py:203` — `max_dd_duration_periods` returns `None` instead of 0.
- L57 `backtest_result.py:_time_extremes` — empty-series handling.
- L58 `backtest_result.py:186` — `compact()` strips fields; verify no downstream silent fail.
- L230 `metrics.py:138` — Sharpe geometric vs arithmetic (**D1**).
- L236 `backtest_result.py:550-555` — `SweepResult._sorted` buries `None`-metric configs at bottom.

**Blast radius:** every `results_v2/*.json` once D1 lands.

**Tests:** `test_metrics_edge.py` (new), `test_sweep_result_sorting.py` (new), extend `test_metrics.py`.

**Snapshot impact:** **Yes** — rerun regression snapshot generation, ship a separate `*_post_p2.json` commit with per-strategy delta table.

**Effort:** M.

---

### Batch 2 — Pipeline / simulator hygiene & config safety

**Includes:**
- L74 `pipeline.py:145` — `sanitize_orders(max_return_mult=999.0)`.
- L75 `pipeline.py:64-69` — multi-exchange sweep mistagged.
- L93 `simulator.py:219-229` — `order_value` types.
- L100 `utils.py:32-44` — `_t` suffix stripping fragility.
- L133 `config_sweep.py` — compound-params counting.
- L285 `config_sweep.py:17-26` — empty param list silently produces zero-config sweep.
- L186 edge case: delisted instrument mid-sim.

**Blast radius:** `sanitize_orders` change may drop orders. Measure on 4 baselines first.

**Tests:** `test_sanitize_orders.py`, `test_simulator_order_value.py` (both new), extend `test_config_sweep.py`, `test_pipeline.py`.

**Snapshot impact:** conditional on threshold measurement.

**Effort:** M.

---

### Batch 3 — Scanner & data-provider correctness

**Includes:**
- L125 `data_provider.py` CR memory_mb=16384 tier verification.
- L126 BhavcopyDataProvider unadjusted prices.
- L174 Bhavcopy vs nse_charting close agreement.
- L175 Volume / `average_price` sanity.
- L281 `scanner.py:131` `drop_nulls()` → `drop_nulls(subset=["close"])`.
- L291 `data_provider.py:853-862` `HAVING AVG(CLOSE)` on unadjusted prices.

**Blast radius:** `drop_nulls` subset fix may retain previously-dropped rows. Propagates.

**Tests:** extend `test_scanner.py`, add `test_bhavcopy_universe.py`, `test_data_provider_memory.py`.

**Snapshot impact:** probable small delta. Measure and document.

**Effort:** M.

---

### Batch 4 — Ranking & signal semantics

**Includes:**
- L107 correctness verification of `calculate_daywise_instrument_score` (perf rewrite in Batch 7).
- L108 `sort_orders_by_deepest_dip` two code paths.
- L141 peak-recovery window.
- L142 TSL exit-price semantics.
- L153 `momentum_dip_quality.py` state leak audit.
- L154 second-tier verification (`enhanced_breakout`, `momentum_cascade`).
- L162 universe filter cadence.
- L163 symbol format normalization.
- L304 `momentum_dip_quality.py:233` hardcoded `avg_close > 50` INR-specific.
- L305 `earnings_dip.py:486` TypeError on None in slice.
- L274 `intraday_simulator_v2.py:417-422` `use_hilo=False` close-vs-stop-price.
- L275 `intraday_simulator_v2.py:370` `eod_buffer_bars=30` bar-scale assumption.

**Blast radius:** eod_breakout/enhanced_breakout unaffected. `momentum_dip_quality` will shift if `avg_close > 50` becomes config-derived.

**Tests:** extend `test_ranking.py`, `test_intraday_simulator_v2.py`; add `test_signals_universe_filter.py`, `test_earnings_dip_none_guard.py`.

**Effort:** M. Split into 4a (EOD) + 4b (intraday v2 docs).

---

### Batch 5 — Charges realism

**Includes:**
- L115 `charges.py` intraday vs delivery confirmation.
- L116 slippage rate realism.
- L114 per-exchange fee schedules (LSE/HKSE/etc.).

**Blast radius:** **every cross-exchange result** shifts 2-10%.

**Tests:** extend `test_charges.py` with per-exchange golden values; `test_charges_slippage.py` (new).

**Snapshot impact:** **major** for cross-exchange. Two commits: (5a) code + tests, (5b) snapshot-update with per-strategy delta.

**Effort:** L. Land late.

---

### Batch 6 — Test hardening / property tests / coverage

**Includes:**
- L28 Hypothesis property tests.
- L187 currency mismatch rejection test.
- L188 timezone/DST edge test.
- L372 regression snapshot expansion.
- L376 polars pin in `requirements.txt`.
- L377 determinism-across-runs test.
- L378 `group_by("instrument")` ordering audit.
- L296 `cr_client.py:184-198` retry/backoff unit test.
- L371 signal coverage spot-checks.

**Blast radius:** zero (tests + dep pin only).

**Tests:** `test_metrics_properties.py`, `test_determinism.py`, `test_cr_client_retry.py`, `test_timezone.py` (all new), extend `test_known_answer.py`.

**Effort:** M. Schedule early.

---

### Batch 7 — Performance hotspots

**Includes:**
- L107 `ranking.py:calculate_daywise_instrument_score` polars rewrite.
- L314 `signals/base.py:206-213 run_scanner` polars join (100× expected).
- L315 Per-instrument filter loops in `earnings_dip.py:410-414`, `momentum_dip_quality.py:408`.
- L316 `list.index(epoch)` → `bisect_left` across 8+ signal files.

**Blast radius:** must be byte-identical. Any float-ordering drift → back out, investigate.

**Tests:** regression snapshots primary oracle; `test_perf_equivalence.py` (new).

**Effort:** L. Per-file commits for bisect-ability. **Candidate to split into dedicated perf sprint.**

---

### Batch 8 — Deprecation / cleanup

**Includes:**
- L345 intraday v1 deprecation (**D2**).
- L176 delisted-stocks survivorship doc.
- L189 floating-point accumulation measurement.
- L344 `intraday_pipeline.py` chunk-boundary audit.
- L384 margin interest documentation (**D3**).
- L385 dividend income documentation (**D4**).
- L353 `intraday_sql_builder.py:131` v1 `LEAST` bug — covered by deprecation.

**Blast radius:** none.

**Tests:** `test_deprecation.py`, `test_intraday_pipeline_chunks.py` (both new).

**Effort:** S.

---

## 4. Out of scope / reclassification

**Dropped from sprint** (document only, per D3/D4):
- Real margin-interest model.
- Real dividend income model.

**Test-coverage debt folded into Batch 6** (not real bugs):
- L28, L371, L296, L378.

**Performance sprint candidates** (if bandwidth tight, split out of P2):
- L107, L314, L315, L316 → Batch 7.

**Likely invalidate historical results** (flag loudly):
- Batch 1 D1 — every result shifts. Ship `scripts/recompute_metrics.py` migration.
- Batch 5 — cross-exchange shifts 2-10%. Archive old results as `results_v2_preP2/`.
- Batch 3 `drop_nulls(subset=["close"])` — small delta.

---

## 5. Final deliverables

1. **8 commits** (1 per batch), all green `pytest tests/`.
2. **`docs/AUDIT_FINDINGS.md`** updated per batch with file:line, root cause, fix, measured delta.
3. **New test modules:**
   - `tests/test_metrics_edge.py`
   - `tests/test_metrics_properties.py`
   - `tests/test_sweep_result_sorting.py`
   - `tests/test_sanitize_orders.py`
   - `tests/test_simulator_order_value.py`
   - `tests/test_bhavcopy_universe.py`
   - `tests/test_signals_universe_filter.py`
   - `tests/test_earnings_dip_none_guard.py`
   - `tests/test_determinism.py`
   - `tests/test_cr_client_retry.py`
   - `tests/test_timezone.py`
   - `tests/test_deprecation.py`
   - `tests/test_intraday_pipeline_chunks.py`
   - `tests/test_perf_equivalence.py`
   - `tests/test_charges_slippage.py`
   - `tests/test_data_provider_memory.py`
   - Extensions to existing tests.
4. **Snapshot update commits** (Batch 1, Batch 3 partial, Batch 5). Per-strategy delta tables.
5. **`scripts/recompute_metrics.py`** extended for dual Sharpe derivation from stored equity curves.
6. **Intraday v1 deprecation PR** (`DeprecationWarning` + `docs/INTRADAY_V1_DEPRECATION.md`).
7. **`requirements.txt` pin** matching `lib/cloud_orchestrator.py`.
8. **`docs/P2_DECISIONS.md`** recording D1-D4 outcomes.
9. **Updated `docs/AUDIT_CHECKLIST.md`** with all 49 P2 items checked off, inline notes.

---

## 6. What NOT to do in this sprint

- No real margin-interest model (D3 is doc-only).
- No real dividend income model (D4 is doc-only).
- No rerunning `results/` historical backtests outside Batches 1/3/5.
- No touching P3 items (32 open).
- No signal refactors beyond specific lines.
- No data-driven fee-schedule framework mid-sprint (Batch 5 replaces hardcoded constants with a per-exchange dict; YAML-backed is P3).
- No skipping regression runs between batches.
- No running Batch 7 (perf) in parallel with any correctness batch.
- No amending Phase 1-7 snapshots. Ship `_post_p2.json` alongside; don't overwrite `_pre_fix.json`.

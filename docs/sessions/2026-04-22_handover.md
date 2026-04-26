# Session Handover — 2026-04-22

**Previous sessions:** 2026-04-20 (engine regression discovery) through 2026-04-22 (audit completion + Phase 8A/8B bias findings).
**Next session goal:** Unblock strategies, re-baseline expectations, and begin fresh optimization on the honest engine.
**Execution constraint:** **Local only.** No CR compute (run_remote.py, cloud_orchestrator). No remote server. Data fetch via `cr_client.py` is still fine — that's just NSE parquet pulls, not compute. If a strategy OOMs locally, fix via memory management in the signal generator, not via cloud offload.

---

## 1. What's committed

`git log --oneline -5`:

```
fbcd36a Drop NEXT_SESSION_HANDOVER.md — AUDIT_FINDINGS.md is now the canonical log
a159429 AUDIT_FINDINGS: append Phase 8B — eod_breakout regime port + sweep
e167e11 eod_breakout: optional regime filter (entries + force-exit) + sweep findings
1b572ce Pin regression snapshots for 3 COMPLETE strategies (pre-rework baseline)
ca0b5a5 Sync AUDIT_CHECKLIST banner with actual tick-mark counts
```

Audit status (authoritative log: `docs/AUDIT_FINDINGS.md`):

- 17/17 P0 closed
- 49/50 P1 closed (1 partial)
- 43/48 P2 closed (3 deferred with rationale)
- 26/32 P3 closed (6 hygiene deferred)
- Test suite: **440 passing**
- **214 files migrated** to `results_v2/` via `scripts/recompute_metrics.py`

Major artifacts now in the repo (didn't exist before the audit):
- `lib/equity_curve.py` — typed frozen dataclass + `Frequency` enum
- `engine/exits.py` — canonical exit primitives
- `engine/order_key.py` — fixes tier collision (P0 #7)
- `engine/charges.py` — per-exchange schedules (LSE, HKSE, XETRA, JPX, KSC, TSX, ASX)
- `scripts/measure_bias_impact.py` — A/B harness for honest vs biased modes
- `tests/regression/snapshots/` — 3 pinned snapshots (eod_breakout, enhanced_breakout, momentum_cascade)

## 2. What's uncommitted

```
 M docs/AUDIT_CHECKLIST.md  (banner text + 1 test-file clarification)
 M docs/AUDIT_FINDINGS.md   (2 cross-reference updates: snapshot re-pin, MTG universe decision reversal)
?? docs/SESSION_HANDOVER_2026-04-22.md  (this file)
```

These are harmless doc edits. Safe to commit together with this handover.

## 3. Calibration check — expectations have shifted

The previous plan assumed ~18% CAGR from a backtested "champion." Audit invalidated that:

| Strategy | Pre-audit claim | Honest number | Status |
|----------|----------------|---------------|--------|
| momentum_dip_quality | 22.71% / Cal 0.55 | **5.08% / Cal 0.14** | AUDIT_RETIRED (below NIFTYBEES buy-hold) |
| momentum_top_gainers | 27.3% / Cal 1.02 | unknown | AUDIT_BLOCKED |
| momentum_rebalance | n/a | 6.59% (fixture) | AUDIT_BLOCKED |
| enhanced_breakout | 11.9% / Cal 0.499 | 7.84% / Cal 0.184 (invalidated) | Champion needs re-run (P0 #10) |
| eod_breakout (regime sweep 2026-04-22) | 13.3% / Cal 0.52 | **in-sample best 19.0%, realistic 14-16% OOS** | Candidates saved, need WF validation |
| momentum_cascade | 9.7% / Cal 0.37 | unchanged (pinned) | COMPLETE |

**Working assumption for next session:** honest CAGRs on NSE tend to land 7-15%. Anything above 20% on in-sample needs immediate bias-audit scrutiny (full-period universe, same-bar entry, survivorship).

## 4. Open items

### 4.1 Critical path blockers

- [ ] **Unblock `momentum_top_gainers`** — run A/B (legacy `full_period` vs honest `point_in_time`) on full NSE universe 2010-2026, local compute. The 30-blue-chip fixture could not detect the bias. Expected: 4-10pp CAGR reduction. Decide: retire, re-optimize, or accept.
- [ ] **Unblock `momentum_rebalance`** — run A/B (legacy `moc_signal_lag_days=0` vs honest `=1`) on full NSE universe. Fixture showed -5.54pp on 30 stocks. Decide: retire or accept the drop as input to re-optimization.
- [ ] **Re-optimize `enhanced_breakout`** — P0 #10 invalidates the old champion (TSL never fired on red-close breakouts). Post-fix baseline is 7.84% CAGR. Need fresh R1-R4 on the honest engine.

### 4.2 Cross-exchange re-runs

- [ ] Every result file in `results_v2/` targeting LSE, HKSE, XETRA, JPX, KSC, TSX, ASX was produced before Phase 3 revisit charges landed. See `docs/CROSS_EXCHANGE_STALE_RATES.md` for the 49-file inventory. Affected strategies: enhanced_breakout, momentum_cascade, momentum_top_gainers, momentum_dip_quality. Re-run or tag stale.
- [ ] SHH/SHZ/SAO/TAI/PAR remain on the 0.05%/side fallback (warning logs once). If real local rates matter, add per-exchange helpers.

### 4.3 Infrastructure gaps

- [ ] **`scripts/regression_test.py` does not exist** but is referenced by `OPTIMIZATION_PROMPT.md`. Options:
  - (a) Build it (wraps `tests/regression/snapshot.py` to run each COMPLETE strategy's champion + diff against pinned fixture, ±2% tolerance).
  - (b) Replace reference in OPTIMIZATION_PROMPT with `pytest tests/regression/` — that path exists and does equivalent work.
  - Pick one before next optimization session claims a strategy.
- [ ] **`OPTIMIZATION_PROMPT.md` has `{POST_AUDIT_BASELINE}` placeholder** (2 occurrences). Replace with `fbcd36a` (current HEAD) before use.
- [ ] **`OPTIMIZATION_PROMPT.md` plausibility thresholds still calibrated pre-audit.** Raise `Calmar > 2.0` → `Calmar > 2.9`, `CAGR > 35%` → `CAGR > 50%` (audit doc cites these). Remove TODO-POST-AUDIT markers.
- [ ] **`OPTIMIZATION_PROMPT.md` protected-files list** is missing the new shared-infra files. Add: `engine/exits.py`, `engine/order_key.py`, `lib/equity_curve.py`.
- [ ] **`OPTIMIZATION_PROMPT.md` execution section** — strip server instructions; this environment is local-only. Keep cr_client data-fetch reference.
- [ ] **`docs/SESSION_PENDING_WORK.md`** is a stale 2026-04-15/20 doc describing the engine regression (since fixed). Retire or archive — it no longer reflects reality.

### 4.4 Strategy queue state

Current `strategies/OPTIMIZATION_QUEUE.yaml` snapshot:

| Priority | Strategy | Status | Notes |
|---------:|----------|--------|-------|
| 1 | eod_breakout | COMPLETE | Pinned snapshot; Phase 8B added regime sweep with 19% IS candidate (needs WF) |
| 2 | enhanced_breakout | COMPLETE (but invalidated by P0 #10) | Needs full re-run |
| 3 | momentum_cascade | COMPLETE | Pinned snapshot |
| 4 | momentum_top_gainers | AUDIT_BLOCKED | needs full-NSE A/B |
| 5 | momentum_dip_quality | AUDIT_RETIRED | honest CAGR below index buy-hold |
| 6 | earnings_dip | IN_PROGRESS | Cal 0.292 baseline; never completed re-optimization |
| 7-30 | quality_dip_buy, quality_dip_tiered, forced_selling_dip, factor_composite, trending_value, eod_technical, momentum_rebalance (BLOCKED), momentum_dip, low_pe, ml_supertrend, connors_rsi, ibs_mean_reversion, extended_ibs, bb_mean_reversion, squeeze, darvas_box, swing_master, gap_fill, overnight_hold, holp_lohp, index_breakout, index_dip_buy, index_green_candle, index_sma_crossover | PENDING | 22 strategies untouched |

Decision needed: **reset the 3 COMPLETE strategies to PENDING** (matches the prompt's "from scratch" intent), or **keep them as regression-test fixtures** (matches the snapshot-pinning work already done)? The audit chose the latter. If going fully from scratch, also drop `tests/regression/snapshots/*.json` or accept they become stale.

### 4.5 Deferred audit work

From `docs/AUDIT_CHECKLIST.md`, still `[ ]` open:

- 1 P1: `_portfolio_metrics` hand-computed fixture (partial coverage exists in `test_backtest_result.py`)
- 2 P2: `intraday_pipeline` spot-audit, synthetic regression snapshot fixture
- 6 P3: hygiene/perf items (`copy.deepcopy` profile, ranking perf rewrite, etc.)

None of these block next-session optimization work. Safe to defer.

### 4.6 Known systematic biases still in place

Documented in AUDIT_FINDINGS.md, NOT fixed (results still affected):

- **Margin interest** — `order_value_multiplier > 1` treated as free leverage. Overstates returns by `margin_rate × leverage × years`. Real NSE MTF is ~10% p.a.
- **Dividend income** — long-hold strategies on dividend-paying universes understate returns by ~yield × hold_years.
- **Survivorship** — NSE charting provider excludes delisted names; bhavcopy includes them. Cross-provider comparison has different universes.
- **momentum_rebalance same-bar entry** — kept as a behavioral default; honest mode is opt-in via `moc_signal_lag_days=1`.

These are known, documented, and non-blocking for strategy-level optimization as long as you're aware which direction each biases.

## 5. Memory management playbook for local runs

Since we're committing to local-only compute, signal generators that previously needed CR must work within laptop/desktop RAM. The audit already profiled the main cost:

- `epoch_wise_instrument_stats`: 2454 instruments × 5915 calendar days = **4.15 GB** in nested Python dict (~307 bytes/entry). This is the dominant memory consumer.
- Signal gen per-strategy: typically 1-4 GB during materialization (polars buffer + Python list copies).
- Simulator: <0.5 GB (position state + trade log).

**Local memory strategies (in order of preference):**

1. **Narrow the data.** Most strategies don't need the full 2454-instrument NSE universe. Use `scanner.instruments` with a symbol list or tighter liquidity thresholds to cut to 500-1000 stocks. Memory scales linearly.
2. **Narrow the date range.** R1-R3 sweeps can run on 2015-2025 (10 years) instead of 2010-2026 (16 years). Cuts memory ~40%. Rerun R4 validation on the full window only for the final champion.
3. **Use `df_tick_ranked` pre-filter.** The signal generator can pre-compute `df_tick_data.filter(instrument.is_in(universe))` once, pass that to simulator/ranking. Cuts stats dict memory proportionally. **But:** ensure this is per-strategy in the signal generator, NOT in shared engine code — the engine-regression incident was caused by exactly this kind of hack bleeding into shared code.
4. **Chunk by exit config.** R2 sweeps with multiple exit configs can be batched (1 entry × K exits per batch, write intermediate results). Simulator state isn't shared across exit configs, so this is safe.
5. **`del df_signals; gc.collect()`** at signal-gen boundaries — already done in momentum_dip_quality. Pattern works and is honest.

Do NOT:
- Add instrument-count caps or epoch-range filters to `engine/pipeline.py` or `engine/utils.py`. Those files are protected. Precedent: the "top-200 cap" and "epoch filter" in commit 38aad0e inflated results 1.5-3.6x.
- Swap forward-fill behavior in the engine.
- Skip the warmup prefetch window to save memory — it corrupts indicators.

## 6. Fast-start for the next session

1. Read `docs/AUDIT_FINDINGS.md` section-by-section (section 1 = Layer 0-1, section 6 = Phase 8A, section 8 = Phase 8B).
2. Read this handover.
3. Decide: unblock momentum_top_gainers A/B first, or jump to enhanced_breakout re-optimization, or tackle a PENDING strategy fresh?
4. Before claiming ANY strategy, fix the 4 `OPTIMIZATION_PROMPT.md` issues in §4.3 (placeholder, thresholds, protected files, server reference). A 10-minute doc edit, saves a regression later.
5. Commit the uncommitted doc edits (§2) so the next session starts clean.

## 7. Reality check on the 18% target

The "guaranteed 18% CAGR" plan came from pre-audit numbers. Post-audit evidence:

- The one strategy that cleanly held (momentum_cascade) is 9.7%.
- The highest honest in-sample is eod_breakout Phase 8B at 19% (unvalidated OOS).
- The three biased strategies either collapsed (mdq: 22% → 5%) or are pending honest measurement.

**Plan off 10-15% CAGR as the likely honest ceiling per-strategy.** Multi-strategy ensembles could push higher. The 18% target is reachable but requires either a successful ensemble or a PENDING strategy turning up a genuine outlier.

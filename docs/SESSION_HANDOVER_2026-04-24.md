# Session Handover — 2026-04-24

**Duration:** ~4 hours
**Focus:** Post-audit strategy optimization + benchmark recalibration
**Engine baseline:** commit `fbcd36a` (all audit fixes landed)
**Commits this session:** 8 (809fbf0..fe775c0)

---

## 1. What was done

### 1a. Infrastructure (commits 809fbf0)
- Fixed `docs/OPTIMIZATION_PROMPT.md`: resolved `{POST_AUDIT_BASELINE}` → `fbcd36a`, raised plausibility thresholds (Cal >2.9, CAGR >50%), added 3 new protected files (`engine/exits.py`, `engine/order_key.py`, `lib/equity_curve.py`), replaced server execution refs with local-only + memory playbook, replaced phantom `scripts/regression_test.py` with actual `tests/regression/snapshot.py` flow.
- Committed previous-session doc edits (AUDIT_CHECKLIST, AUDIT_FINDINGS, SESSION_HANDOVER_2026-04-22).

### 1b. Champion re-verification (commit fffc2e5)
All 3 previously-COMPLETE strategies re-run on post-audit engine with `nse.nse_charting_day`:

| Strategy | Pre-audit | Post-audit (honest) | Delta |
|---|---|---|---|
| enhanced_breakout | 11.85% / Cal 0.499 | **16.40% / Cal 0.656** | UP (ppy fix + exit/charges net positive) |
| eod_breakout | 13.3% / Cal 0.516 | **15.20% / Cal 0.446** | CAGR up, MDD deeper |
| momentum_cascade | 9.7% / Cal 0.366 | **13.75% / Cal 0.460** | UP |

All three clear NIFTYBEES buy-and-hold (10.45%, 2010-2026).

### 1c. Benchmark correction
Discovered that **NIFTYBEES buy-and-hold 2010-2026 is 10.45%** (not the ~12% shorthand used in prior sessions). This affected retirement decisions for two strategies.

### 1d. Retirements + reversals

| Strategy | Action | Reason |
|---|---|---|
| momentum_rebalance | AUDIT_RETIRED ✓ | Best 7.0% CAGR, genuinely below NIFTYBEES 10.45% |
| momentum_dip_quality | AUDIT_RETIRED ✓ | Honest 5.08% CAGR, retired in prior session |
| momentum_top_gainers | RETIRED → **COMPLETE** | 10.72% ties NIFTYBEES, but Cal 0.373 > NIFTYBEES ~0.27. Risk-adjusted edge. |
| earnings_dip | RETIRED → **COMPLETE** (caveat) | Full-period 5.46% dragged by pre-2018 data sparsity. 2020-2026: **13.80% / Cal 0.680**, beats NIFTYBEES 12.03%. Only valid 2018+. |

### 1e. New optimizations (R0-R4 complete)

**quality_dip_buy** (commit 5350a35):
- Champion: `yr=3, dip=5%, peak=63d, regime=NIFTYBEES>SMA200, tsl=15%, pos=15, top_gainer`
- Full: 11.63% / Cal 0.307. OOS: 17.20% / Cal 0.581.
- Walk-forward: 4/5 positive, **Std Cal 0.530 → FRAGILE** (one losing fold 2016-2019).
- ~385 configs across R0-R3. Non-obvious: `top_gainer` beats `top_dipper` in dip-buy universe.

**forced_selling_dip** (commit 9ce85cb):
- Champion: `slb=5, dip=2%, vol≥1.0×, tsl=15%, pos=8, regime=NIFTYBEES>SMA200`
- Full: **13.26% / Cal 0.431**. OOS: 4.66% / Cal 0.156 (regime-specific).
- Walk-forward: 4/5 positive, **Std Cal 0.218 → PASSES**.
- ~290 configs across R0-R3. Counter-cyclical to qdb (fsd's best fold = qdb's worst).

---

## 2. Current queue state

### COMPLETE (7 strategies)

| # | Strategy | CAGR | Cal | Walk-fwd | Caveat |
|---|---|---|---|---|---|
| 1 | enhanced_breakout | 16.40% | 0.656 | — | Best overall |
| 2 | eod_breakout | 15.20% | 0.446 | — | |
| 3 | earnings_dip | 13.80% | 0.680 | — | **2020+ only** (pre-2018 data sparse) |
| 4 | momentum_cascade | 13.75% | 0.460 | — | |
| 5 | forced_selling_dip | 13.26% | 0.431 | Std 0.218 ✓ | Counter-cyclical to qdb |
| 6 | quality_dip_buy | 11.63% | 0.307 | Std 0.530 ⚠️ | FRAGILE |
| 7 | momentum_top_gainers | 10.72% | 0.373 | — | Ties NIFTYBEES, better Cal |

### AUDIT_RETIRED (2)
- `momentum_rebalance` — 7.0% CAGR, genuinely below NIFTYBEES
- `momentum_dip_quality` — 5.08% CAGR, look-ahead bias retirement

### PENDING (20 strategies)

**Medium priority (known, never optimized):**

| # | Strategy | Notes | Signal gen exists? |
|---|---|---|---|
| 8 | quality_dip_tiered | Tiered entry variant of qdb. Similar family → expect similar ~11% CAGR. | ✓ |
| 10 | factor_composite | Multi-factor ranking (momentum + profitability + value). Novel mechanism. | ✓ |
| 11 | trending_value | O'Shaughnessy trending value. Long hold periods. | ✓ |
| 12 | eod_technical | Original ATO strategy. Similar to eod_breakout but different signal gen. | ✓ |
| 14 | momentum_dip | Simple momentum + RSI dip. | ✓ |
| 15 | low_pe | Fundamental value strategy. Uses FMP financial_ratios. | ✓ |
| 16 | ml_supertrend | SuperTrend indicator with quality gate. | ✓ |

**Lower priority (mean reversion / niche):**

| # | Strategy | Notes |
|---|---|---|
| 17 | connors_rsi | RSI(2) oversold + SMA trend filter |
| 18 | ibs_mean_reversion | Internal bar strength mean reversion |
| 19 | extended_ibs | Extended IBS with volatility filter |
| 20 | bb_mean_reversion | Bollinger Band mean reversion |
| 21 | squeeze | Bollinger/Keltner squeeze breakout |
| 22 | darvas_box | Darvas box breakout with volume confirmation |
| 23 | swing_master | SMA crossover swing trading |
| 24 | gap_fill | Gap-down fill strategy |
| 25 | overnight_hold | Overnight holding strategy |
| 26 | holp_lohp | High of low period / low of high period |
| 27-30 | index_* (4) | Index-level strategies (breakout, dip_buy, green_candle, sma_crossover) |

---

## 3. Deferred work from prior sessions

### Cross-exchange re-runs (from 2026-04-22 handover)
- 49 results files for LSE/HKSE/XETRA/JPX/KSC/TSX/ASX have stale charge schedules.
- See `docs/CROSS_EXCHANGE_STALE_RATES.md`.
- **Not blocking:** only affects cross-exchange validation (R4d), not NSE champions.

### Regression snapshots
- 3 pinned snapshots in `tests/regression/snapshots/` (eod_breakout, enhanced_breakout, momentum_cascade) are from pre-session runs and show drift vs fresh runs (data growth in nse.nse_charting_day). Need re-pinning decision.
- The snapshots were captured from different source configs than `config_champion.yaml` for each strategy, so they're not directly comparable.

### Deferred audit items
- 1 P1: `_portfolio_metrics` hand-computed fixture
- 2 P2: `intraday_pipeline` spot-audit, synthetic regression snapshot fixture
- 6 P3: hygiene/perf/edge cases
- See `docs/AUDIT_CHECKLIST.md`.

### Documentation cleanup
- Retire stale `docs/SESSION_PENDING_WORK.md` (superseded by this handover + OPTIMIZATION_QUEUE.yaml).
- Update `docs/OPTIMIZATION_RUNBOOK.md` plausibility thresholds (doc still references old numbers; this prompt has the correct ones).

---

## 4. Key learnings from this session

### Benchmark matters
NIFTYBEES buy-and-hold 2010-2026 = **10.45%**, not 12%. The 12% number was from a recent sub-period (2020-2026 = 12.03%). This affected two retirement decisions. All future comparisons should use the same-period NIFTYBEES CAGR, not a fixed number.

### Data-window effects
`earnings_dip` and `momentum_top_gainers` showed that full-period metrics can be misleading when the underlying data source (FMP earnings) has structural gaps. Modern-window (2018+) results should be checked alongside full-period.

### Post-audit CAGR direction
The ppy=252 bug UNDER-estimated CAGR on calendar-day equity curves. Three strategies went UP post-audit (eb, mc, eod), while momentum_top_gainers and momentum_rebalance went down (primarily from other audit fixes: exit logic, charges). The audit wasn't universally deflationary.

### Counter-cyclical strategies
`forced_selling_dip` and `quality_dip_buy` are natural complements: fsd thrives in volatile markets (2016-2019 Cal 0.525), qdb thrives in bull markets (2020-2025 Cal 0.863). An ensemble could smooth regime dependency.

### Common optimization patterns across strategies
- `tsl=15%` is the sweet spot across quality_dip_buy, forced_selling_dip, and earnings_dip.
- Fundamental overlays (ROE, PE, DE) consistently HURT by reducing trade count without improving quality.
- `top_gainer` sorting beats `top_dipper` even in dip-buy strategies (momentum within dips matters).
- `regime_instrument=NSE:NIFTYBEES, regime_sma_period=200` consistently helps Calmar (+10-15%) with minimal CAGR cost.
- Concentrated portfolios (8-15 positions) outperform diversified (20+).

---

## 5. Fast-start for next session

1. Read this handover + `strategies/OPTIMIZATION_QUEUE.yaml`.
2. All engine/metrics code is frozen at `fbcd36a`. Do NOT modify protected files.
3. Next strategy recommendation: **`factor_composite`** (priority 10) — novel multi-factor mechanism, different from the dip-buy family that dominates the current queue.
4. Alternative: **`quality_dip_tiered`** (priority 8) if you want to stay in the dip-buy family.
5. All runs are LOCAL with `data_provider: nse_charting` (CR API for data fetch only).
6. Follow `docs/OPTIMIZATION_RUNBOOK.md` and create `strategies/{name}/OPTIMIZATION.md`.
7. Benchmark is **NIFTYBEES same-period CAGR** (not a fixed number). Compute it for each strategy's date range.

---

## 6. Files modified/created this session

```
COMMITTED (8 commits, 809fbf0..fe775c0):

docs/
  AUDIT_CHECKLIST.md                    — banner sync
  AUDIT_FINDINGS.md                     — corrections + updates
  OPTIMIZATION_PROMPT.md                — 5 fixes (placeholder, thresholds, protected files, local-only, regression)
  SESSION_HANDOVER_2026-04-22.md        — prior session handover (new)
  audit_phase_8a/momentum_top_gainers.md — revised: COMPLETE (was RETIRED)
  audit_phase_8a/momentum_rebalance.md  — honest re-run with full NSE data

strategies/
  OPTIMIZATION_QUEUE.yaml               — 7 COMPLETE, 2 RETIRED, 20 PENDING
  eod_breakout/config_champion.yaml     — honest header update
  enhanced_breakout/config_champion.yaml — honest header update
  momentum_cascade/config_champion.yaml — honest header update
  momentum_top_gainers/config_champion.yaml  — nse_charting provider
  momentum_top_gainers/config_audit_ab.yaml  — A/B config (new)
  momentum_rebalance/config_nse.yaml    — nse_charting + moc_signal_lag_days=1
  momentum_rebalance/config_audit_ab.yaml — A/B config (new)
  earnings_dip/OPTIMIZATION.md          — COMPLETE with data-window caveat
  earnings_dip/config_r2_honest.yaml    — honest R2 re-sweep (new)
  earnings_dip/config_6yr.yaml          — 2020-2026 validation (new)
  quality_dip_buy/OPTIMIZATION.md       — full R0-R4 (new)
  quality_dip_buy/config_champion.yaml  — champion (new)
  quality_dip_buy/config_round{0,1,2,3}.yaml — sweep configs (new)
  quality_dip_buy/config_r4_oos.yaml    — OOS config (new)
  forced_selling_dip/OPTIMIZATION.md    — full R0-R4 (new)
  forced_selling_dip/config_champion.yaml — champion (new)
  forced_selling_dip/config_round{0,1,2,3}.yaml — sweep configs (new)

scripts/
  run_qdb_walkforward.py                — quality_dip_buy walk-forward harness (new)
  run_fsd_walkforward.py                — forced_selling_dip walk-forward harness (new)

GITIGNORED (results/, reproducible):
  results/quality_dip_buy/round{0-4}*.json
  results/forced_selling_dip/round{0-4}*.json
  results/earnings_dip/round2_honest.json
```

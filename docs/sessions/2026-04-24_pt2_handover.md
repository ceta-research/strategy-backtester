# Session Handover — 2026-04-24 (Part 2)

**Duration:** ~6 hours (continuation of morning session 2026-04-24_pt1)
**Focus:** Strategy optimization — work through PENDING queue in priority order
**Engine baseline:** commit `fbcd36a` (protected files unchanged, verified 0 diff)
**Commits this session:** 10 (6159495..47e08c5)
**Methodology:** `docs/OPTIMIZATION_PROMPT.md` + `docs/OPTIMIZATION_RUNBOOK.md` (Rounds 0-4)

---

## 1. What was done

4 strategies optimized to COMPLETE, 1 AUDIT_RETIRED. Each followed the standard flow:
Baseline → R1 sensitivity → R2 full cross → R3 robustness → R4a OOS + R4b walk-forward.

### Strategy results (added this session)

| # | Strategy | CAGR | Cal | Sharpe | Status | Commit |
|---|---|---|---|---|---|---|
| 8 | factor_composite | 14.78% | 0.319 | 0.55 | COMPLETE | 6159495 |
| 9 | quality_dip_tiered | **18.39%** | **0.388** | 0.76 | COMPLETE | 1f1e058 |
| 10 | trending_value | 16.89% | 0.481 | 0.75 | COMPLETE (fragile) | 110290f |
| 11 | eod_technical | 19.63% | **0.757** | **1.07** | COMPLETE (regime-dep) | 91c10e8 |
| — | momentum_dip | 8.62% | 0.205 | — | **AUDIT_RETIRED** | 47e08c5 |

### Per-strategy detail

**factor_composite** (priority 10): Multi-factor momentum+profitability+value with monthly rebalance.
- Champion: `lookback=350, skip=21, weights={0.6/0.2/0.2}, regime=0, top_n=30, sl=0.30, pos=15`
- 758 configs. OOS 18.43%/Cal 0.491 (> IS, robust). WF 5/5 positive, Std Cal 0.301 PASSES.
- Deflated Sharpe 0.317 (just clears 0.3).
- Finding: fundamentals add little on NSE, strategy ≈ long-horizon momentum with tilt.
- Deepest MDD among COMPLETE (-46%, 4.7yr duration).

**quality_dip_tiered** (priority 8): Multi-tier DCA dip-buy on quality universe.
- Champion: `yr=2, n_tiers=2, tier_mult=1.5, dip=4, peak=30, regime=NIFTYBEES>200, tsl=8, pos=15, ppi=3`
- 334 configs. OOS 27.18%/Cal 0.718 (1.85× IS). WF 5/5 positive, Std Cal 0.494 (borderline PASS).
- Deflated Sharpe 0.536 (strong).
- Finding: DCA thesis confirmed. Beats qdb (11.63%) decisively. Win rate 70.8% structural (require_peak_recovery), not bias.
- Post-hoc ppi sensitivity sweep: ppi=1 gives conservative Cal 0.314 @ MDD -40.7% as alt.

**trending_value** (priority 11): O'Shaughnessy quality+growth ranking.
- Champion: `dta=1.0, roe=0.10, lb=1, top_n=75, quarterly, mh=365, tsl=20, pos=15`
- 658 configs. OOS 23.49%/Cal 0.529 (~IS). WF 5/5 positive, **Std Cal 0.745 FAILS fragility**.
- Deflated Sharpe 0.508.
- Finding: **Regime-dependent, not overfitting.** Early folds Cal 0.17-0.34 (FMP sparse), late folds Cal 1.36-1.82. Effectively valid 2018+. Parallel to `earnings_dip`.
- Post-hoc alt-champion WF (roe=0.08, top_n=100): Std Cal 0.848 (worse) — confirms structural fragility.
- Very low turnover: 181 trades/16yr (~11/year).

**eod_technical** (priority 12): Legacy MA+nday_high+direction_score breakout.
- Champion: `ndma=3, ndh=5, ds={3,0.54}, mh=3, tsl=10, top_gainer, pos=15`
- 388 configs. OOS 38.31%/Cal 1.347. WF 5/5 positive, **Std Cal 0.723 FAILS**.
- Deflated Sharpe **0.805** (very strong).
- **#1 by CAGR, Calmar, Sharpe among COMPLETE.** Vol 16.5% < NIFTYBEES ~18%.
- Post-hoc pre-2019 standalone: **CAGR 8.62% only** (below Nifty ~11%). Full-period 19.63% is **heavily weighted by 2019+ mid-cap bull**.
- Uses legacy `engine/scanner.py` + `engine/order_generator.py` (not in protected list but feeds into protected simulator). Verified no same-bar bias (entry at `next_open`).
- Forward expectation: 10-13% CAGR if regime reverts.

**momentum_dip** (priority 14): Reddit-inspired RSI<30 in top-momentum. **RETIRED.**
- Best across 378 configs: 8.62%/Cal 0.205 — below NIFTYBEES 10.45%.
- Reddit's "81.6% WR / 31% in 2 months" confirmed overstated: post-audit (no same-bar bias, NSE charges) brings it below benchmark.
- Structural: RSI<30 in winners fires too frequently; 3-8% profit target can't overcome charges + deep losers.

### Methodology notes

- **Process followed:** `docs/OPTIMIZATION_PROMPT.md` (verified at session start). Protected files unchanged (0 diff vs fbcd36a).
- **Data:** `nse.nse_charting_day` for all Rounds 0-3. R4c (cross-data) and R4d (cross-exchange) deferred per qdb/fsd precedent.
- **Execution:** LOCAL only (`python run.py --config <yaml> --output <json>`).
- **R3 skipped for eod_technical** — R2 already had 243 configs with 10/10 robustness PASS.
- **Post-hoc checks added when concerning:** ppi sensitivity (qdt), alt-champion WF (tv), pre-2019 standalone (et). All confirmed champion selection.

---

## 2. Current queue state

### COMPLETE (11 strategies, ranked by CAGR)

| # | Strategy | CAGR | Cal | Sharpe | Caveat |
|---|---|---|---|---|---|
| 1 | eod_technical | **19.63%** | **0.757** | **1.07** | Regime-dep: pre-2019 only 8.62% |
| 2 | quality_dip_tiered | 18.39% | 0.388 | 0.76 | Deep MDD -47%, WF std borderline |
| 3 | trending_value | 16.89% | 0.481 | 0.75 | WF std 0.745 FAILS (data-window 2018+) |
| 4 | enhanced_breakout | 16.40% | 0.656 | — | — |
| 5 | eod_breakout | 15.20% | 0.446 | — | — |
| 6 | factor_composite | 14.78% | 0.319 | 0.55 | Deepest MDD -46%, 4.7yr DD |
| 7 | earnings_dip | 13.80% | 0.680 | — | **2020+ only** (FMP pre-2018 sparse) |
| 8 | momentum_cascade | 13.75% | 0.460 | — | — |
| 9 | forced_selling_dip | 13.26% | 0.431 | — | Std WF 0.218 ✓, counter-cyclical to qdb |
| 10 | quality_dip_buy | 11.63% | 0.307 | — | Std WF 0.530 ⚠ FRAGILE |
| 11 | momentum_top_gainers | 10.72% | 0.373 | — | Ties NIFTYBEES, better Cal |

### AUDIT_RETIRED (3)
- `momentum_rebalance` — 7.0% CAGR below NIFTYBEES
- `momentum_dip_quality` — 5.08% CAGR, look-ahead bias retired
- `momentum_dip` — 8.62% CAGR below NIFTYBEES (retired this session)

### PENDING (16 strategies)

**Medium priority:**

| # | Strategy | Notes |
|---|---|---|
| 15 | low_pe | Fundamental value. Uses FMP financial_ratios. |
| 16 | ml_supertrend | SuperTrend indicator with quality gate. |

**Lower priority (mean reversion / niche):**

| # | Strategy | Notes |
|---|---|---|
| 17 | connors_rsi | RSI(2) oversold + SMA trend filter |
| 18 | ibs_mean_reversion | Internal bar strength mean reversion |
| 19 | extended_ibs | Extended IBS with volatility filter |
| 20 | bb_mean_reversion | Bollinger Band mean reversion |
| 21 | squeeze | BB/Keltner squeeze breakout |
| 22 | darvas_box | Darvas box breakout + volume |
| 23 | swing_master | SMA crossover swing |
| 24 | gap_fill | Gap-down fill |
| 25 | overnight_hold | Overnight holding |
| 26 | holp_lohp | High of low period / low of high period |
| 27-30 | index_* (4) | Index-level: breakout, dip_buy, green_candle, sma_crossover |

---

## 3. Key findings / learnings this session

### Regime dependency is pervasive on NSE

Five of our COMPLETE strategies have strong 2019+ regime dependence:
- `eod_technical`: pre-2019 CAGR 8.62% vs post-2019 38%+
- `trending_value`: pre-2019 Cal 0.17-0.34, post-2019 Cal 1.36-1.82
- `quality_dip_tiered`: 2010-2013 Cal 0.23, later folds Cal 0.36-1.82
- `factor_composite`: 2010-2013 Cal 0.017, later folds Cal 0.23-0.76
- `earnings_dip`: pre-2018 effectively flat due to FMP data sparsity

**Implication:** "Full-period CAGR" numbers can be misleading when the strategy only works in one regime. Always check sub-period performance and flag in docs. Nifty Midcap 100 went roughly 17k → 60k+ in 2019-2025 (~20% CAGR just for the index). Any concentrated momentum strategy benefits disproportionately.

### WF Std Cal as fragility gate — useful but not sufficient

Three new COMPLETE strategies have Std Cal in 0.5-0.75 range (fail/borderline):
- trending_value Std 0.745 (FAILS)
- eod_technical Std 0.723 (FAILS)
- quality_dip_tiered Std 0.494 (borderline)

All are legitimate per other metrics (OOS > IS or OOS ≈ IS, deflated Sharpe >> 0.3). The fragility gate mostly detects regime dependency, not overfitting. Consider adding pre-2019 sub-period check as additional discipline.

### FMP NSE fundamentals sparsity is a recurring theme

Strategies relying on FMP `income_statement`, `balance_sheet`, `financial_ratios`, etc.:
- `earnings_dip`: only valid 2018+ (noted)
- `trending_value`: sparse pre-2018 drags early folds
- `factor_composite`: fundamentals add little on NSE (sparse + noisy)
- `low_pe`, `ml_supertrend` (pending): same concern expected

Consider adding a banner in `OPTIMIZATION_PROMPT.md` noting FMP NSE coverage is post-2015 reliable, post-2018 rich. Strategies depending on it should caveat accordingly.

### DCA thesis (qdt vs qdb)

`quality_dip_tiered` at n_tiers=2 (tier1=4%, tier2=6%) beats `quality_dip_buy` single-tier at 5%: 18.39% vs 11.63%. **DCA works on NSE for quality dip-buy** — lower average entry cost + more recovery opportunities. Win rate 70.8% is structural (require_peak_recovery holds losers until recovery), not look-ahead bias.

### Reddit strategies routinely overstated

`momentum_dip` (retired this session) is the second strategy where a popular Reddit claim (81.6% WR / 31% in 2 months) didn't survive post-audit scrutiny. The memory note from prior audit session was correct: "Reddit backtest claims ~50% overstated (close-entry bias, no costs)". Worth remembering for future Reddit-sourced strategies.

---

## 4. Deferred / still-pending work (carried over from part 1)

### Cross-exchange re-runs (from 2026-04-22)
- 49 results files for LSE/HKSE/XETRA/JPX/KSC/TSX/ASX have stale charge schedules.
- See `docs/CROSS_EXCHANGE_STALE_RATES.md`.
- **Not blocking** — only affects cross-exchange validation (R4d).

### Regression snapshots
- 3 pinned snapshots in `tests/regression/snapshots/` are from pre-session runs and show drift. Re-pinning decision pending.

### Deferred audit items
- 1 P1: `_portfolio_metrics` hand-computed fixture
- 2 P2: `intraday_pipeline` spot-audit, synthetic regression snapshot fixture
- 6 P3: hygiene/perf/edge cases
- See `docs/AUDIT_CHECKLIST.md`.

### Documentation cleanup
- Retire stale `docs/SESSION_PENDING_WORK.md`.
- Update `docs/OPTIMIZATION_RUNBOOK.md` plausibility thresholds.
- Consider adding "pre-2019 sub-period check" to runbook (from this session's regime dependency findings).

### R4c (cross-data-source) and R4d (cross-exchange)
Deferred for all 4 new COMPLETE strategies. Same precedent as qdb, fsd.

---

## 5. Fast-start for next session

1. Read this handover + `strategies/OPTIMIZATION_QUEUE.yaml`.
2. Protected engine files still frozen at `fbcd36a`. Do NOT modify.
3. **Next strategy: `low_pe` (priority 15)** — FMP-dependent (expect data-window caveat similar to earnings_dip/tv).
4. Alternative: `ml_supertrend` (priority 16) — SuperTrend indicator, NSE-native data.
5. Consider running a quick "pre-2019 sanity check" on each new champion to surface regime dependency before finalizing.
6. Use `scripts/run_*_walkforward.py` as template for walk-forward harness.
7. Commits land LOCAL only. No push unless requested.
8. Benchmark: NIFTYBEES 2010-2026 **10.45% CAGR, ~0.27 Cal**.

---

## 6. Files modified/created this session (part 2)

```
COMMITTED (10 commits, 6159495..47e08c5):

strategies/
  OPTIMIZATION_QUEUE.yaml                    — 11 COMPLETE, 3 RETIRED, 16 PENDING

  factor_composite/
    OPTIMIZATION.md                          — full R0-R4 tracker (new)
    config_baseline.yaml                     — new
    config_round1a_weights_topn.yaml         — new
    config_round1b_lookback.yaml             — new
    config_round1c_long_lookback.yaml        — new
    config_round1d_exit_sim.yaml             — new
    config_round2.yaml                       — new
    config_round3.yaml                       — new
    config_r4_oos.yaml                       — new
    config_champion.yaml                     — new

  quality_dip_tiered/
    OPTIMIZATION.md                          — full R0-R4 tracker (new)
    config_baseline.yaml                     — new
    config_round1a.yaml                      — new
    config_round1b.yaml                      — new
    config_round1c.yaml                      — new
    config_round2.yaml                       — new
    config_round3.yaml                       — new
    config_r4_oos.yaml                       — new
    config_champion.yaml                     — new
    config_ppi_check.yaml                    — post-hoc ppi sensitivity (new)

  trending_value/
    OPTIMIZATION.md                          — full R0-R4 tracker (new)
    config_baseline.yaml                     — new
    config_round1a.yaml                      — new
    config_round1b.yaml                      — new
    config_round2.yaml                       — new
    config_round3.yaml                       — new
    config_r4_oos.yaml                       — new
    config_champion.yaml                     — new
    config_alt_champion.yaml                 — post-hoc alt-champion (new)

  eod_technical/
    OPTIMIZATION.md                          — full tracker (new)
    config_baseline.yaml                     — new
    config_round1a.yaml                      — new
    config_round1b.yaml                      — new
    config_round2.yaml                       — new
    config_r4_oos.yaml                       — new
    config_champion.yaml                     — new
    config_pre2019.yaml                      — post-hoc regime check (new)

  momentum_dip/
    OPTIMIZATION.md                          — retirement doc (new)
    config_baseline.yaml                     — new
    config_round1.yaml                       — new
    config_round2.yaml                       — new

scripts/
  run_fc_walkforward.py                      — factor_composite WF harness (new)
  run_qdt_walkforward.py                     — quality_dip_tiered WF harness (new)
  run_tv_walkforward.py                      — trending_value WF harness (new)
  run_tv_alt_walkforward.py                  — tv alt-champion WF (new)
  run_et_walkforward.py                      — eod_technical WF harness (new)

docs/
  SESSION_HANDOVER_2026-04-24_pt2.md         — this file (new)

GITIGNORED (results/, reproducible):
  results/factor_composite/round{0-4}*.json
  results/quality_dip_tiered/round{0-4}*.json
  results/trending_value/round{0-4}*.json
  results/eod_technical/round{0-4}*.json
  results/momentum_dip/round{0-2}*.json
```

---

## 7. Summary stats (session 2)

- **Strategies worked:** 5 (4 COMPLETE + 1 RETIRED)
- **Configs run:** ~2515 total (758 FC + 334 QDT + 658 TV + 388 ET + 378 MD)
- **Commits:** 10
- **New champions ranking:** #1 CAGR (ET 19.63%), #1 Calmar (ET 0.757), #1 Sharpe (ET 1.07), #2 CAGR (QDT 18.39%)
- **Queue progress:** 4 PENDING moved to COMPLETE, 1 to AUDIT_RETIRED → 16 PENDING remaining

---

## 8. Combined session (pt1 + pt2) totals

- **Duration:** ~10 hours total (pt1 ~4h + pt2 ~6h)
- **Commits across both parts:** 18 (809fbf0..47e08c5)
- **Strategies COMPLETE (pt1 + pt2):** 11 total, 4 added in pt2
- **Strategies RETIRED:** 3 total, 1 added in pt2
- **Best strategy found:** `eod_technical` (19.63% / Cal 0.757 / Sharpe 1.07) — though with regime dependency caveat
- **Most robust strategy found:** `forced_selling_dip` (WF Std Cal 0.218 PASSES cleanly, counter-cyclical to qdb)

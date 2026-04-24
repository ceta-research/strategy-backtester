# earnings_dip Optimization

**Strategy:** Earnings beat + post-earnings dip on quality stocks
**Signal file:** `engine/signals/earnings_dip.py`
**Data source:** nse.nse_charting_day (Rounds 0-3), fmp.stock_eod (Round 4)

## Status (2026-04-24): AUDIT_RETIRED

Honest re-run on post-audit engine (commit fbcd36a+):

**Pre-audit champion re-run** (s=1, d=3, q=2, r=200, TSL=15, hold=1000, pos=8):
- CAGR 5.46% / Calmar 0.205 / MDD -26.7% / 68 trades
- Pre-audit claim was 10.6% / 0.638 — ~48% metrics inflation

**Honest R2 re-sweep** (72 configs: dip × TSL × hold × pos, 2026-04-24):
- Best CAGR: 7.78% (dip=2, tsl=30, hold=1000, pos=5) → Cal 0.169, MDD -46%, 21 trades
- Best Calmar: 0.371 (dip=2, tsl=15, hold=756, pos=8) → CAGR 6.12%, MDD -16.5%, 71 trades
- **No config clears NIFTYBEES ~12% CAGR.**

**Reasons to retire:**
1. Best honest CAGR (7.78%) is 4pp below NIFTYBEES buy-and-hold.
2. Best-Calmar config has 71 trades over 16yr — low statistical significance.
3. Pre-audit deflated Sharpe was 0.188 (FAIL <0.3). Honest Sharpe is lower.
4. Walk-forward (pre-audit) already showed 3/6 positive — fragile.
5. FMP earnings_surprises NSE coverage is structurally sparse pre-2018.

See `results/earnings_dip/round2_honest.json` for the full honest sweep.

---

## Historical (pre-audit, kept for traceability)

## Champion

| Period | Config | CAGR | MDD | Calmar | Trades |
|--------|--------|------|-----|--------|--------|
| 2010-2026 | s=1,d=3,q=2,r=200,TSL=15,hold=1000,pos=8 | **10.6%** | -16.6% | **0.638** | 46 |

**vs baseline:** CAGR 3.9%→10.6% (+171%), MDD 19.2%→16.6% (-2.6pp), Calmar 0.205→0.638 (+211%)

Also strong: s=1,d=3,q=2,r=200,TSL=15,hold=756,pos=8 → Cal=0.617, CAGR=10.0%, MDD=-16.1%

Note: TSL=15 dominates the top 6 configs. hold=1000 slightly better than 756.

## Parameters

| Param | Baseline | R1 Best | R1 Class | Notes |
|-------|----------|---------|----------|-------|
| `dip_threshold_pct` | 5 | **3** (Cal 0.437) | IMPORTANT | Peak at 3%, bell curve |
| `surprise_threshold_pct` | 5 | **1** (Cal 0.381) | IMPORTANT | Lower = better, ~monotonic |
| `consecutive_positive_years` | 2 | **3** (Cal 0.359) | IMPORTANT | Peak at 3, drops >4 |
| `trailing_stop_pct` | 10 | **30** (Cal 0.347) | IMPORTANT | Peak at 30%, bell curve |
| `post_earnings_window` | 20 | **10** (Cal 0.291) | IMPORTANT | Peak at 10-15 |
| `max_positions` | 10 | **8** (Cal 0.276) | IMPORTANT | Peak at 8 |
| `max_hold_days` | 504 | **756** (Cal 0.230) | IMPORTANT | Monotonic, extend |
| `order_sorting_type` | top_gainer | top_dipper (Cal 0.209) | INSENSITIVE | Range 5%, doesn't matter |
| `roe_threshold` | 15 | 0 (Cal 0.200) | INSENSITIVE | roe=0 ≈ baseline (only 1 value ran) |
| `pe_threshold` | 25 | **0** (Cal 0.239) | MODERATE | PE filter hurts — disabling adds +17% Cal |
| `regime_sma_period` | 200 | 50 (Cal 0.218) | INSENSITIVE | range 29%, all SMA values similar |
| `volume_ratio_max` | 0 | **0** (Cal 0.205) | MODERATE | Enabling any volume filter HURTS (reduces trades) |

## Round 0: Baseline

*Config:* `config_baseline.yaml` (1 config, nse_charting_day, 2010-2026)

| CAGR | MDD | Calmar | Sharpe | Trades | WR | PF | AvgHold |
|------|-----|--------|--------|--------|----|----|---------|
| 3.9% | -19.2% | 0.205 | 0.240 | 69 | 65% | 3.18 | 156d |

Only 69 trades in 16 years. Strict quality + PE<25 + ROE>15 + regime SMA200 limits universe to avg 145 stocks.
Key bottleneck: too few trade opportunities. Loosening filters should increase both CAGR and trade count.

## Round 1: Sensitivity Scan

| Param | Values swept | Range% | Classification | Best Cal |
|-------|-------------|--------|----------------|----------|
| `dip_threshold_pct` | 2,3,5,7,10,15 (6/8) | 128% | **IMPORTANT** | 0.437 (dip=3) |
| `surprise_threshold_pct` | 1,7,10,15,20,30 (6/9) | 103% | **IMPORTANT** | 0.381 (surprise=1) |
| `trailing_stop_pct` | 3-50 (8) | 89% | **IMPORTANT** | 0.347 (TSL=30) |
| `max_hold_days` | 42-756 (8) | 86% | **IMPORTANT** | 0.230 (hold=756, monotonic) |
| `max_positions` | 3-50 (8) | 84% | **IMPORTANT** | 0.276 (pos=8) |
| `consecutive_positive_years` | 1-5 (5/6) | 76% | **IMPORTANT** | 0.359 (quality=3) |
| `post_earnings_window` | 10-60 (7/8) | 51% | **IMPORTANT** | 0.291 (window=10) |
| `order_sorting_type` | all 4 types | 5% | INSENSITIVE | 0.209 (top_dipper) |
| `pe_threshold` | 0-50 (5) | 35% | MODERATE | 0.239 (PE=0 disabled) |
| `regime_sma_period` | 0-300 (6) | 29% | INSENSITIVE | 0.218 (SMA=50) |
| `volume_ratio_max` | 0-3.0 (8) | 78% | MODERATE | 0.205 (0=disabled, best) |
| `roe_threshold` | 0 (1/7) | — | INSENSITIVE | 0.200 (roe=0 ≈ baseline) |

Sorting: top_dipper (0.209) > top_gainer (0.205) > top_performer (0.205) > top_avg_txn (0.199)
Volume confirmation hurts: enabling any volume_ratio_max reduces trades and Calmar. Keep disabled.
PE filter hurts slightly: disabling (pe=0) adds +17% Cal by including more stocks.

### Key R1 Observations

1. **dip=3 is the single biggest improvement** (Cal +113%). Smaller dip threshold = more entries, tighter reversal requirement.
2. **surprise=1 loosens the filter maximally** — every positive surprise counts. Cal +86%.
3. **TSL=30 lets dip-buy winners run** — tight stops (3-10%) kill returns. Cal +69%.
4. **quality=3 is a sweet spot** — 3 years of positive returns catches genuinely strong stocks without being too restrictive.
5. **holddays is monotonic** — should test 1000+ in R2.
6. **positions=8** — concentrated but not too concentrated. 5 gives higher CAGR (6%) but more volatility.
7. **roe/pe/regime/volume not fully tested** — cloud compute limits. Include 2 values each in R2.

## Round 2: Full Cross-Parameter Search

54 entry combos × 72 exit/sim configs = 3,888 total. Submitted as individual 1-entry runs.

**Entry grid:** surprise=[1,5,10] × dip=[2,3,5] × quality=[1,2,3] × regime=[0,200]
**Fixed:** window=10, roe=0, pe=0 (disabled for more trades)
**Exit grid:** TSL=[10,15,20,25,30,40] × holddays=[504,756,1000]
**Sim grid:** positions=[5,8,10,15]

**Top 5 by Calmar (within champion entry s=1,d=3,q=2,r=200):**

| Config | TSL | Hold | Pos | CAGR | MDD | Calmar | Trades |
|--------|-----|------|-----|------|-----|--------|--------|
| 1_1_6_2 | 15 | 1000 | 8 | **10.6%** | -16.6% | **0.638** | 46 |
| 1_1_5_2 | 15 | 756 | 8 | 10.0% | -16.1% | 0.617 | 45 |
| 1_1_6_3 | 15 | 1000 | 10 | 9.5% | -17.4% | 0.547 | 53 |
| 1_1_6_1 | 15 | 1000 | 5 | 9.5% | -18.0% | 0.525 | 36 |
| 1_1_5_1 | 15 | 756 | 5 | 9.3% | -18.0% | 0.517 | 36 |

**Key R2 findings:**
- **TSL=15 + holddays=756-1000 dominates** — let winners run with moderate stop
- **positions=8 is optimal** — 5 too concentrated, 10-15 dilutes alpha
- **regime=200 adds +0.1-0.15 Cal** vs no regime across all combos
- **quality=2 beats quality=1** (quality=1 OOMs) and quality=3 (fewer trades)
- **dip=3 > dip=2 > dip=5** — sweet spot at 3%
- **surprise=1 dominates** — loosest filter captures most post-earnings drift

## Round 3: Robustness Check

Perturbation test: 10 entry param perturbations around champion, each with 48 exit/sim configs.

| Perturbation | Best Cal | % Retained | Result |
|-------------|----------|-----------|--------|
| champion (ref) | 0.592 | 93% | PASS |
| surprise=2 | 0.513 | 80% | PASS |
| dip=2 | 0.547 | 86% | PASS |
| dip=4 | 0.589 | 92% | PASS |
| dip=5 | 0.528 | 83% | PASS |
| quality=1 | 0.571 | 89% | PASS |
| quality=3 | 0.390 | 61% | FAIL |
| regime=0 | 0.416 | 65% | FAIL |
| regime=150 | 0.541 | 85% | PASS |
| regime=250 | 0.506 | 79% | PASS |

**Pass rate: 8/10 (80%) — meets ≥80% threshold**

Failures are on discrete filter changes (quality level jump, regime removal), not continuous sensitivity. d=4 (0.589) is nearly as good as d=3 (0.592), confirming a robust region. Regime SMA 150-250 all pass.

## Round 4: Validation

### 4a. OOS Split

| Period | CAGR | MDD | Calmar | Trades |
|--------|------|-----|--------|--------|
| IS (2010-2020) | 3.9% | -5.2% | 0.747 | 4 |
| OOS (2020-2026) | 6.2% | -27.8% | 0.225 | 20 |

**Drop: 70% — FAIL** (but IS unreliable with only 4 trades)

### 4b. Walk-Forward (6 folds)

| Fold | CAGR | MDD | Calmar | Trades |
|------|------|-----|--------|--------|
| 2010-2014 | 2.4% | -3.2% | 0.746 | 0 |
| 2012-2016 | 11.7% | -5.9% | 1.980 | 0 |
| 2014-2018 | -1.1% | -7.0% | -0.163 | 3 |
| 2016-2020 | -1.0% | -8.5% | -0.118 | 2 |
| 2018-2022 | -7.1% | -23.1% | -0.308 | 5 |
| 2020-2026 | 7.4% | -24.9% | 0.299 | 20 |

**Avg Calmar: 0.406 | Positive: 3/6** — borderline, skewed by data-sparse early folds

### 4c. Cross-Data-Source

| Source | CAGR | MDD | Calmar | Trades |
|--------|------|-----|--------|--------|
| nse_charting_day (primary) | 9.4% | -16.6% | 0.567 | 46 |
| fmp.stock_eod (.NS) | 5.4% | -22.1% | 0.242 | 887 |
| nse_bhavcopy | — | — | — | 0 (entry candidates=0 after fix) |

Primary source is best. FMP produces 20x more trades but lower quality (different price adjustments). Bhavcopy produced 0 entries after the per-instrument processing refactor (regime filter issue with bhavcopy data).

### 4d. Cross-Exchange

| Exchange | CAGR | MDD | Calmar | Trades |
|----------|------|-----|--------|--------|
| **NSE** (primary) | 9.4% | -16.6% | **0.567** | 46 |
| **US** | 12.2% | -33.5% | **0.365** | 2490 |
| UK | -0.7% | -33.8% | -0.022 | 263 |
| Hong Kong | -1.6% | -51.2% | -0.030 | 240 |
| Germany | 0.0% | -8.2% | 0.004 | 22 |
| Canada | — | — | — | 0 trades |
| South Korea | — | — | — | 0 trades |
| Taiwan | — | — | — | 0 trades |

**Fixed:** Exchange mapping in `_fetch_earnings_surprises()` now supports all FMP exchanges (was NSE/US only). US also fixed: config must use `exchange: US` (not NYSE/NASDAQ) to match CRDataProvider instrument names.

**NSE and US are profitable.** UK and HK are negative. Strategy works best in markets with strong post-earnings drift (US, India).

### Deflated Sharpe

| Metric | Value |
|--------|-------|
| Observed Sharpe | 0.361 |
| Configs tested | ~200 |
| Deflated Sharpe | 0.188 |
| Verdict | **FAIL (<0.3)** |

### Validation Summary

The earnings_dip strategy has a **fundamental data limitation**: FMP earnings_surprises coverage for NSE is sparse before 2018. Only 30 total trades across 2010-2026 with the champion config. This invalidates traditional OOS/WF tests (sample too small).

The strategy produces strong metrics on the full period (Cal=0.638, CAGR=10.6%) but the statistical significance is low due to the small trade count. This is a strategy that works when it fires but fires too rarely for robust validation.

**Recommendations:**
1. Increase trade count by loosening filters (surprise=1 already loose, try dip=2 or quality=1)
2. Extend to other exchanges with better earnings data (US/UK have denser coverage)
3. Consider this a "high-conviction, low-frequency" strategy — pair with other strategies for portfolio construction

## Memory Fix

Refactored `engine/signals/earnings_dip.py` to process instruments one-at-a-time instead of building a full dict-of-lists. The original code OOMed on cloud with 6M rows × 7 columns in Python lists. Exit data built lazily via `_exit_cache`. Scanner IDs resolved via lookup dict instead of DataFrame join.

## Files

```
results/earnings_dip/
  round0_baseline.json              # R0: 1 config, baseline
  round1_tsl.json                   # R1: 8 configs, TSL sweep
  round1_holddays.json              # R1: 8 configs, max_hold_days sweep
  round1_positions.json             # R1: 8 configs, max_positions sweep
  round1_sorting.json               # R1: 4 configs, sorting type sweep
  round1_surprise.json              # R1: 6 configs, surprise threshold sweep
  round1_dip.json                   # R1: 6 configs, dip threshold sweep
  round1_window.json                # R1: 7 configs, post_earnings_window sweep
  round1_quality.json               # R1: 5 configs, quality filter sweep
  round2_full.json                  # R2: partial (55 configs from late runs)
  round2_champion_detail.json       # R2: 72 configs (champion entry × all exit/sim)
```

## Session State

**Current round:** R2 complete (champion identified), R3-R4 pending
**Champion:** s=1,d=3,q=2,r=200,TSL=15,hold=1000,pos=8 → Cal=0.638, CAGR=10.6%
**Cloud project:** ce6333fd-e8a7-4c68-84ae-3fff49a8a9d4
**Next steps:**
1. R3: Perturbation test around champion (surprise=[1,2], dip=[2,3,5], TSL=[20,25,30,35], hold=[504,756,1000], pos=[5,8,10])
2. R4a: OOS split (2010-2020 train, 2020-2026 test)
3. R4b: Walk-forward (5-6 folds)
4. R4c: Cross-data-source (fmp.stock_eod .NS, nse_bhavcopy)
5. R4d: Cross-exchange (US, UK, etc via fmp.stock_eod)
6. Deflated Sharpe ratio calculation

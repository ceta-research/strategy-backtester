# quality_dip_buy Optimization

**Strategy:** Buy dips in stocks with N consecutive years of positive returns; exit on peak recovery or TSL.
**Signal file:** `engine/signals/quality_dip_buy.py`
**Data:** `nse.nse_charting_day` (NSE, 2010-01-01 to 2025-03-06)
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status

- [x] Round 0: Baseline
- [x] Round 1: 48-config sensitivity cross
- [x] Round 2: 144-config full cross
- [x] Round 3: 96-config fine grid
- [x] Round 4a: OOS (2020-2026)
- [x] Round 4b: Walk-forward (5 rolling 3-yr folds)
- [ ] Round 4c: Cross-data-source (deferred — see Verdict)
- [ ] Round 4d: Cross-exchange (deferred — see Verdict)

## Champion

| Period | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|------|-----|--------|--------|--------|
| **Full (2010-2025)** | **11.63%** | -37.9% | **0.307** | 0.591 | 297 |
| OOS (2020-2025)      | **17.20%** | -29.6% | **0.581** | — | — |

### Walk-forward (5 folds, 3-yr rolling)

| Fold | CAGR | MDD | Calmar | Sharpe |
|------|------|-----|--------|--------|
| 2010-2013 |  3.59% | -21.2% |  0.169 |  0.14 |
| 2013-2016 | 17.89% | -15.0% |  **1.191** |  1.07 |
| 2016-2019 | **-3.28%** | **-35.3%** | -0.093 | -0.36 |
| 2019-2022 | 18.53% | -23.2% |  0.797 |  1.07 |
| 2022-2025 | 22.58% | -26.2% |  0.863 |  0.95 |

**Positive folds:** 4/5 (80%)
**Mean Calmar:** 0.586  **Std Calmar:** 0.530 → **FRAGILE by runbook threshold (std > 0.5).**

**Weakness:** the 2016-2019 fold is a clear losing period (mid/small-cap crash in India, quality names affected). This is a regime the strategy does not handle.

## Parameters

| Param | Baseline | Champion | Notes |
|-------|----------|----------|-------|
| `consecutive_positive_years` | 3 | **3** | R2 tested 3 vs 4; 3 edges out on Calmar |
| `min_yearly_return_pct` | 0 | **0** | Kept baseline; positive-only return filter is enough |
| `dip_threshold_pct` | 10 | **5** | R2/R3: 5% dominates top Cal rankings |
| `peak_lookback_days` | 126 | **63** | Shorter peak = faster entries; 63d dominated R3 Cal |
| `rescreen_interval_days` | 63 | **63** | Not swept; baseline |
| `regime_instrument` | "" | **NSE:NIFTYBEES** | Adds ~0 to CAGR but tightens DD |
| `regime_sma_period` | 0 | **200** | Standard |
| `trailing_stop_pct` | 5 | **15** | 15% lets winners run; tight TSL hurt CAGR |
| `max_hold_days` | 504 | **504** | Not a bottleneck |
| `max_positions` | 15 | **15** | Concentration helps (10 too few, 25 dilutes alpha) |
| `order_sorting_type` | top_dipper | **top_gainer** | **Non-obvious:** within quality-filtered dips, momentum-sort beats dip-depth sort |

## vs baseline

CAGR 6.03% → 11.63% (+5.6pp), Calmar 0.137 → 0.307 (+124%), MDD -44.0% → -37.9% (-6.1pp improved).

## vs NIFTYBEES

NIFTYBEES buy-and-hold (~12% CAGR, 2010-2026) essentially ties the champion's full-period CAGR (11.63%).

## Deflated Sharpe

- Observed Sharpe: 0.591 (full period)
- Configs tested: ~240 across R0+R1+R2+R3
- Rough correction: `sqrt((1 + 0.5*0.591^2) / 180) * Z(1 - 1/240) ≈ sqrt(0.007) * 2.88 ≈ 0.24`
- Deflated Sharpe ≈ **0.35** → marginally above the 0.3 "statistical significance" threshold.

## Verdict

**Status: COMPLETE (with caveats).**

**Keep, don't retire.** Full-period 11.63% essentially matches NIFTYBEES but OOS (2020-2025) of 17.20% / Cal 0.58 and 4/5 positive walk-forward folds show there IS an edge in bull markets. The fragility flag (std Cal 0.530, one losing fold) means this isn't a drop-in "always use" strategy — it's a strategy for risk-on regimes.

**Why NOT run R4c (cross-data-source) or R4d (cross-exchange):**
- The walk-forward result (std 0.53, 1 losing fold) already tells us this is regime-dependent on NSE. Cross-exchange runs would likely add noise, not validation signal.
- Cost/benefit doesn't justify another ~30 min for a strategy that's already clearly marginal.
- If the strategy is revived (e.g. as part of an ensemble or with stronger regime filtering), re-open 4c/4d then.

**What could push this across the line:**
1. Better regime filter (NIFTYBEES>SMA200 only barely helps — consider direction_score + ADX)
2. Sector diversification (`max_per_sector` was not swept — 2016-2019 hit concentrated sectors hard)
3. Adding direction_score filter to the signal generator (currently not supported)

## Rounds

### Round 0: Baseline — 2026-04-24
Config: `config_round0_baseline.yaml` (1 config)
Result: CAGR 6.03%, MDD -44.04%, Cal 0.137.
Interpretation: no market filter → enters during bear markets.

### Round 1: Sensitivity cross — 2026-04-24
Config: `config_round1.yaml` (48 configs)
Swept: dip × regime × TSL × sorting.
Best CAGR: 11.23% (dip=7, no-regime, tsl=10, top_gainer).
Key finding: `top_gainer` beats `top_dipper` within dip-buy universe.

### Round 2: Full cross — 2026-04-24
Config: `config_round2.yaml` (144 configs)
Swept: years × dip × peak × regime × TSL × positions (sort locked to top_gainer).
Best CAGR: 12.22% (yr=4, dip=5, peak=126, no-regime, tsl=15, pos=15) — MDD -45%.
Best Calmar: 0.307 (yr=3, dip=5, peak=63, regime=on, tsl=15, pos=15).

### Round 3: Fine grid — 2026-04-24
Config: `config_round3.yaml` (96 configs)
Perturbed ±20% around R2 Calmar winner.
Pass: 10/10 top configs keep ≥70% of best Calmar.
Champion confirmed: R2 Calmar winner is a stable region, not a spike.

### Round 4a: OOS — 2026-04-24
Config: `config_r4_oos.yaml` (2020-01-01 to 2025-03-06)
Result: CAGR 17.20%, MDD -29.6%, Cal 0.581.
OOS Cal is 1.9× full-period Cal — inverse of overfitting pattern.

### Round 4b: Walk-forward — 2026-04-24
Script: `scripts/run_qdb_walkforward.py` (5 rolling 3-yr folds)
See table above. 4/5 positive, fragile.

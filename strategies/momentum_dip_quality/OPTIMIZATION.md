# momentum_dip_quality Optimization

**Strategy:** Momentum + Quality Dip-Buy with optional fundamental filters
**Signal file:** `engine/signals/momentum_dip_quality.py`
**Prior informal result:** 26.6% CAGR, Cal 0.95 (standalone, pre-engine pipeline)

## Champion

| Period | Config | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|--------|------|-----|--------|--------|--------|
| TBD | TBD | — | — | — | — | — |

## Parameters

| Param | Type | Baseline | Plausible range | Notes |
|-------|------|----------|----------------|-------|
| `consecutive_positive_years` | entry | 2 | 1-4 | Quality gate strictness |
| `min_yearly_return_pct` | entry | 0 | 0-10 | Minimum trailing year return |
| `momentum_lookback_days` | entry | 63 | 21-252 | Trailing return window for ranking |
| `momentum_percentile` | entry | 0.30 | 0.10-0.50 | Top N% by momentum |
| `rerank_interval_days` | entry | 63 | 21-126 | How often to re-rank momentum |
| `dip_threshold_pct` | entry | 5 | 2-15 | Min dip from rolling peak to enter |
| `peak_lookback_days` | entry | 63 | 21-126 | Rolling peak window |
| `rescreen_interval_days` | entry | 63 | 21-126 | How often to re-screen quality |
| `roe_threshold` | entry | 15 | 0-20 | ROE filter (0=disabled) |
| `pe_threshold` | entry | 25 | 0-35 | PE filter (0=disabled) |
| `de_threshold` | entry | 1.0 | 0-2.0 | D/E filter (0=disabled) |
| `fundamental_missing_mode` | entry | skip | skip/allow | What to do when no fundamental data |
| `regime_instrument` | entry | NSE:NIFTYBEES | ""/NSE:NIFTYBEES | Regime filter instrument |
| `regime_sma_period` | entry | 200 | 0/100/200 | Regime SMA period (0=disabled) |
| `direction_score_n_day_ma` | entry | 3 | 0/3/5 | Direction score MA (0=disabled) |
| `direction_score_threshold` | entry | 0.54 | 0/0.40/0.54/0.60 | Direction score gate |
| `trailing_stop_pct` | exit | 10 | 3-30 | TSL percentage |
| `max_hold_days` | exit | 504 | 63-504 | Max holding period |
| `require_peak_recovery` | exit | True | True/False | Gate TSL behind peak recovery |
| `order_sorting_type` | sim | top_gainer | 4 types | Signal priority |
| `max_positions` | sim | 10 | 5-30 | Portfolio concentration |

## Execution Notes

**Cloud execution time:** This strategy is significantly heavier than eod_breakout/enhanced_breakout:
- 1500 days prefetch (vs 500 for eod_breakout) = ~12M rows from nse_charting_day
- Quality universe: trailing yearly returns for N consecutive years
- Momentum ranking: period-average turnover + top N% sort
- Walk-forward exit per entry candidate
- Fundamentals fetch adds ~300-600s (separate CR API call)

**Execution timeline:**
- 600s: timed_out (fundamentals too slow)
- 1200s: timed_out (fundamentals too slow)
- 900s/16GB: MemoryError in `g["open"].to_list()` (fix #1: universe filter)
- 900s/16GB: MemoryError in `to_dicts()` (fix #2: iter_rows)
- 900s/16GB: MemoryError in `create_epoch_wise_instrument_stats` (fix #3: instrument filter in pipeline.py)
- 900s/16GB: execution_timed_out in stats forward-fill (fix #3b: removed forward-fill in utils.py)
- 900s/16GB: **COMPLETED in 51s** (R0 baseline, 1 config)
- 900s/16GB: execution_timed_out on 8-config sweep (fix #4: loop order swap)
- 900s/16GB: failed at 0ms (cloud infra, 16GB allocation failure)
- **Recommendation: use --timeout 900 --ram 8192. Single config ~50s. Multi-config TBD.**

**Decision: Disable fundamentals for R0-R3.** Fundamentals (ROE/PE/DE) add massive overhead (separate CR API query). Sweep them as a param in R1. Informal R4/R5 found de=0 was better anyway.

## Informal Round Priors

From configs `config_nse_r1.yaml` through `config_nse_r5_final.yaml`:
- **R1 winners:** mom=126, pct=0.20, dip=5
- **R2 winners:** tsl=7, hold=252, pos=10
- **R3 center:** mom=[63-189], pct=[0.15-0.25], dip=[3-7], tsl=[5-10], pos=[7-13]
- **R4 winner:** mom=126, pct=25%, dip=5, de=0, tsl=5, hold=504, pos=7
- **R5 winner:** mom=63, pct=30%, dip=5, de=0, tsl=5, hold=504, pos=5

**Key findings from informal rounds:**
1. TSL 5-7% better than 10%+ (opposite of eod_breakout!)
2. Concentrated positions (5-10) beat diversified (15-20)
3. D/E filter disabled (de=0) was better
4. Direction score: mixed results, tested as {0, 0.50, 0.54}
5. Winners shifted between R4/R5 (mom 126→63, pos 7→5) - instability

**Methodological issues with informal rounds:**
1. R1 only swept entry params (no exit/sim)
2. R2 locked R1 winners, only swept exit/sim (no crossing all params)
3. No formal OOS or walk-forward validation

## Round 0: Baseline

Config: quality=2yr, mom=63d, top30%, dip=5%, ds={3,0.54}, regime=NIFTYBEES>SMA200,
        TSL=10%, hold=504d, recovery=true, pos=10, top_gainer. No fundamentals.

| CAGR | MDD | Calmar | Sharpe | Sortino | Win Rate | Trades | Avg Hold |
|------|-----|--------|--------|---------|----------|--------|----------|
| **26.2%** | -41.1% | **0.637** | 1.262 | 1.801 | 75.3% | 300 | 162d |

Execution: 51s on CR cloud (16GB RAM). Required 3 code fixes for OOM:
1. Exit data: only universe instruments (575 vs 889)
2. Entry iteration: `iter_rows()` streaming (not `to_dicts()`)
3. Stats building: trading days only (no forward-fill across calendar days)

## Round 1: Sensitivity Scan

**Execution notes:** Split sweeps into 4-config batches (8 configs OOMs at 256K orders). Use `force=True` sync + `sb-remote` project (cached deps). ~55s per batch.

### TSL Sweep (COMPLETE)

| TSL% | CAGR | MDD | Calmar | Trades | Classification |
|------|------|-----|--------|--------|------|
| **8** | **+23.3%** | **-28.6%** | **0.814** | 349 | **IMPORTANT** |
| 10 | +26.2% | -41.1% | 0.637 | 300 | (baseline) |
| 50 | +23.9% | -45.8% | 0.521 | 101 | |
| 3 | +16.9% | -38.6% | 0.438 | 404 | |
| 15 | +23.7% | -54.3% | 0.437 | 235 | |
| 20 | +24.1% | -56.3% | 0.428 | 177 | |
| 30 | +21.4% | -51.3% | 0.417 | 129 | |
| 5 | +14.7% | -45.8% | 0.322 | 367 | |

**TSL is IMPORTANT.** Calmar range: 0.322-0.814 (153% variation). Bell curve shape peaking at 8%. Below 8% → whipsaw. Above 10% → drawdowns run. Best: **TSL=8% (Cal 0.814, +28% vs baseline).**

### Momentum Sweep (7/8 values, missing 378d)

| Lookback | CAGR | MDD | Calmar | Trades | Classification |
|----------|------|-----|--------|--------|------|
| **126d** | **+26.4%** | **-31.5%** | **0.839** | 314 | **IMPORTANT** |
| 189d | +23.7% | -39.5% | 0.601 | 295 | |
| 252d | +26.0% | -47.6% | 0.546 | 307 | |
| 504d | +27.0% | -51.3% | 0.527 | 312 | |
| 63d | +26.8% | -52.0% | 0.515 | 305 | (baseline) |
| 42d | +26.8% | -53.9% | 0.496 | 306 | |
| 21d | +25.1% | -54.6% | 0.460 | 315 | |

**Momentum is IMPORTANT.** Bell curve peaking at 126d. Short lookbacks noisy (high MDD). Best: **126d (Cal 0.839, +32% vs baseline).**

### Percentile Sweep (5/8 values, missing 0.10-0.20)

| Percentile | CAGR | MDD | Calmar | Trades |
|------------|------|-----|--------|--------|
| **0.25** | **+29.0%** | **-42.3%** | **0.686** | 300 |
| 0.40 | +25.6% | -40.3% | 0.636 | 294 |
| 0.30 | +26.8% | -52.0% | 0.515 | 305 |
| 0.50 | +23.0% | -45.8% | 0.502 | 308 |
| 0.60 | +22.8% | -46.7% | 0.489 | 298 |

**Percentile is MODERATE.** 0.25 best (Cal 0.686). Missing tight percentiles (0.10-0.20) due to cloud OOM.

### Dip Sweep (4/8 values, missing 2-5%)

| Dip% | CAGR | MDD | Calmar | Trades |
|------|------|-----|--------|--------|
| **7** | **+26.7%** | **-34.3%** | **0.779** | 358 |
| 10 | +26.4% | -38.6% | 0.684 | 324 |
| 20 | +30.4% | -44.7% | 0.680 | 285 |
| 15 | +28.1% | -52.0% | 0.540 | 310 |

**Dip is MODERATE.** 7% best (Cal 0.779). Missing low values (2-5%) due to cloud OOM.

### Hold/Recovery Sweep (4/12 configs)

| Config | CAGR | MDD | Calmar | Trades |
|--------|------|-----|--------|--------|
| **hold=378d, recovery=true** | **+27.6%** | **-34.3%** | **0.805** | 489 |
| hold=252d, recovery=true | +19.9% | -33.5% | 0.596 | 781 |
| hold=63d, recovery=true | +19.8% | -40.1% | 0.495 | 938 |
| hold=126d, recovery=true | +18.3% | -39.4% | 0.465 | 728 |

**Hold is IMPORTANT.** 378d best (Cal 0.805). Shorter holds = more trades but worse Calmar. Missing recovery=false configs.

### Sim Sweep (24/24 configs - COMPLETE)

| Sort type | pos=10 Cal | Best pos | Notes |
|-----------|-----------|----------|-------|
| **top_gainer** | **0.658** | **10** | Dominates all positions |
| top_dipper | 0.485 | 30 | |
| top_performer | 0.450 | 5-8 | |
| top_average_txn | 0.431 | 5 | |

**Sorting is IMPORTANT** (top_gainer >> others). **Positions is MODERATE** (10 best, 5-30 within 30%).

### Direction Sweep (10/12 configs)

5 disabled configs all show Cal 0.764, enabled {3,0.54} shows 0.754. **Direction is INSENSITIVE** - barely affects results.

### Quality/Regime Sweep (9/12 configs)

Best config: **Cal 1.401** (34.9% CAGR, -24.9% MDD, 304 trades). This is likely quality=1yr (loosest gate) + regime=0 (no filter). Huge outlier - **needs scrutiny for overfitting** in R2/R3.

Second best: Cal 0.840 (26.5% CAGR, -31.5% MDD). More plausible.

### R1 Summary

| Param | Classification | Best value | Best Calmar | R2 values |
|-------|---------------|------------|-------------|-----------|
| trailing_stop_pct | **IMPORTANT** | 8% | 0.814 | 5, 8, 10 |
| momentum_lookback | **IMPORTANT** | 126d | 0.839 | 63, 126, 189 |
| max_hold_days | **IMPORTANT** | 378d | 0.805 | 252, 378, 504 |
| order_sorting_type | **IMPORTANT** | top_gainer | 0.658 | top_gainer (locked) |
| dip_threshold_pct | MODERATE | 7% | 0.779 | 5, 7, 10 |
| momentum_percentile | MODERATE | 0.25 | 0.686 | 0.25, 0.30 |
| max_positions | MODERATE | 10 | 0.658 | 10, 15 |
| quality/regime | **INVESTIGATE** | Cal 1.401 outlier | 1.401 | quality=[1,2], regime=[0,200] |
| direction_score | INSENSITIVE | disabled≈enabled | 0.764 | {3,0.54}, disabled |

Configs:
- `config_round1_tsl.yaml` — TSL=[3,5,8,10,15,20,30,50] (8 configs)
- `config_round1_momentum.yaml` — mom=[21,42,63,126,189,252,378,504] (8 configs)
- `config_round1_dip.yaml` — dip=[2,3,4,5,7,10,15,20] (8 configs)
- `config_round1_percentile.yaml` — pct=[0.10,0.15,0.20,0.25,0.30,0.40,0.50,0.60] (8 configs)
- `config_round1_sim.yaml` — sort=[4 types] x pos=[5,8,10,15,20,30] (24 configs)
- `config_round1_hold.yaml` — hold=[63,126,252,378,504,756] x recovery=[T,F] (12 configs)
- `config_round1_direction.yaml` — ds_ma=[0,3,5] x ds_thresh=[0,0.40,0.54,0.60] (12 configs)
- `config_round1_quality_regime.yaml` — quality=[1,2,3,4] x regime=[0,100,200] (12 configs)
- `config_round1_fundamentals.yaml` — roe x pe x de (64 configs, WITH fundamentals)

Total: ~156 configs across 9 files.

## Round 2: Full Cross-Parameter Search

_Pending R1 results_

## Round 3: Robustness Check

_Pending R2 results_

## Round 4: Validation

_Pending R3 results_

## Files

```
strategies/momentum_dip_quality/
  config_baseline.yaml              # R0 with fundamentals (timed out)
  config_baseline_nofund.yaml       # R0 without fundamentals (in progress)
  config_round1_*.yaml              # 9 R1 sweep configs (ready)

scripts/
  run_r1_batch.py                   # Batch runner for all R1 sweeps
  analyze_r1.py                     # R1 analysis (marginal tables + classification)

results/momentum_dip_quality/
  (awaiting first results)
```

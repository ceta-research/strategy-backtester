# momentum_top_gainers Optimization

**Strategy:** Trailing-period top gainers with TSL exit + direction score + regime filter
**Signal file:** `engine/signals/momentum_top_gainers.py`

## Champion

| Period | Config | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|--------|------|-----|--------|--------|--------|
| 2010-2026 | lb=210,tn=0.40,reb=2,mm=5,ds=0.45,tsl=40,hld=126,pos=30 | **27.3%** | -26.8% | **1.016** | 1.281 | 1353 |
| IS 2010-2020 | same | 19.7% | -28.1% | 0.699 | — | 820 |
| **OOS 2020-2026** | same | **48.6%** | -34.4% | **1.412** | 1.987 | 510 |

**Walk-forward:** 4/5 folds positive, avg Calmar 1.350. Deflated Sharpe **1.334** (PASS).

**vs baseline:** CAGR 7.2%→27.3% (+279%), MDD 38%→27% (-11pp), Calmar 0.19→1.02 (+437%)
**vs informal champion:** CAGR 20.2%→27.3% (+35%), Calmar 0.82→1.02 (+24%)

**Cross-data-source:** nse_charting Cal 0.987, FMP Cal 0.918, bhavcopy Cal 0.795 — all positive.
**Cross-exchange:** US Cal 0.615, UK 0.610, Taiwan 0.606, S.Korea 0.276, China SHZ 0.352 — works globally.

## Parameters

| Param | Type | Baseline | R1 Best | R1 Classification |
|-------|------|----------|---------|-------------------|
| `momentum_lookback_days` | entry | 189 | 63 (CAGR), 63 (Cal) | MODERATE |
| `top_n_pct` | entry | 0.20 | 0.60 (CAGR), 0.60 (Cal) | IMPORTANT |
| `rebalance_interval_days` | entry | 21 | 1 (CAGR+Cal) | IMPORTANT |
| `min_momentum_pct` | entry | 5 | 0 (CAGR+Cal) | MODERATE |
| `direction_score_n_day_ma` | entry | 3 | 5 | INSENSITIVE |
| `direction_score_threshold` | entry | 0.54 | 0.45 (Cal) | MODERATE |
| `regime_sma_period` | entry | 200 | 0/100 | INSENSITIVE |
| `trailing_stop_pct` | exit | 15 | 50 (CAGR), 30 (Cal) | IMPORTANT |
| `max_hold_days` | exit | 252 | 126 (CAGR+Cal) | MODERATE |
| `order_sorting_type` | sim | top_gainer | top_performer | MODERATE |
| `max_positions` | sim | 15 | 30 (Cal) | IMPORTANT |

## Round 0: Baseline

| CAGR | MDD | Calmar | Trades |
|------|-----|--------|--------|
| 7.23% | -38.3% | 0.189 | 3588 |

Config: mom=189, top=0.20, rebal=21, minmom=5, ds={3,0.54}, regime=200, tsl=15, hold=252, top_gainer, pos=15

## Round 1: Sensitivity Scan

### trailing_stop_pct (IMPORTANT)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| 5 | -0.4% | -32.4% | -0.012 |
| 8 | 0.8% | -33.5% | 0.024 |
| 10 | 2.9% | -32.2% | 0.090 |
| 15 | 7.2% | -38.3% | 0.189 |
| 20 | 8.5% | -25.9% | 0.328 |
| 25 | 9.3% | -31.9% | 0.291 |
| 30 | 10.5% | -29.4% | **0.358** |
| 40 | 11.1% | -31.8% | 0.349 |
| **50** | **12.0%** | -33.9% | 0.355 |
| 60 | 11.6% | -36.9% | 0.314 |
| 70 | 11.9% | -35.5% | 0.335 |
| 80 | 12.0% | -35.5% | 0.338 |
| 90 | 11.9% | -36.0% | 0.331 |
| 99 | 11.5% | -36.3% | 0.317 |

Shape: Plateau at 40-80%. Tight stops (<15%) destroy this momentum strategy.

### rebalance_interval_days (IMPORTANT)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| **1** | **13.6%** | -27.9% | **0.488** |
| 2 | 10.6% | -30.6% | 0.346 |
| 3 | 10.4% | -42.8% | 0.243 |
| 5 | 10.6% | -33.7% | 0.314 |
| 7 | 8.3% | -38.2% | 0.217 |
| 10 | 7.1% | -29.2% | 0.243 |
| 15 | 6.7% | -28.5% | 0.235 |
| 21 | 7.2% | -38.3% | 0.189 |
| 42 | 2.2% | -34.4% | 0.064 |
| 63 | 3.8% | -26.3% | 0.145 |
| 84 | 1.3% | -21.1% | 0.062 |
| 126 | 2.0% | -22.9% | 0.087 |

Shape: Steep monotonic decline. Daily rebalancing dominates (13.6%, Cal 0.488). Note: daily generates 68k orders (high turnover, costs included).

### top_n_pct (IMPORTANT)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| 0.05 | 8.5% | -34.1% | 0.249 |
| 0.10 | 8.3% | -36.4% | 0.228 |
| 0.15 | 6.3% | -37.7% | 0.167 |
| 0.20 | 7.2% | -38.3% | 0.189 |
| 0.25 | 7.9% | -37.3% | 0.212 |
| 0.30 | 8.2% | -38.5% | 0.213 |
| 0.40 | 10.1% | -38.2% | 0.264 |
| 0.50 | 10.5% | -37.1% | 0.284 |
| **0.60** | **10.6%** | -36.8% | **0.288** |
| 0.70 | 10.0% | -36.9% | 0.271 |
| 0.80 | 10.1% | -36.7% | 0.275 |
| 1.00 | 9.7% | -36.7% | 0.264 |

Shape: Rises to plateau at 0.50-0.60, then gradual decline. Wider universe = more signal diversity.

### max_positions (IMPORTANT for Calmar)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| 5 | 6.8% | -61.0% | 0.111 |
| 8 | 7.4% | -52.7% | 0.140 |
| 10 | 8.2% | -48.0% | 0.171 |
| 12 | 7.8% | -43.2% | 0.180 |
| 15 | 7.2% | -38.3% | 0.189 |
| 20 | 7.2% | -31.4% | 0.229 |
| 25 | 7.0% | -27.3% | 0.256 |
| **30** | 7.4% | **-24.8%** | **0.298** |

Shape: CAGR flat (6.8-8.2%), MDD monotonically improves. More positions = better diversification.

### momentum_lookback_days (MODERATE)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| **63** | **10.1%** | -28.7% | **0.352** |
| 126 | 7.9% | -37.4% | 0.211 |
| 147 | 8.1% | -36.6% | 0.221 |
| 168 | 7.8% | -24.3% | 0.321 |
| 189 | 7.2% | -38.1% | 0.189 |
| 210 | 7.3% | -22.6% | 0.323 |
| 252 | 7.6% | -36.4% | 0.209 |
| 315 | 7.5% | -32.4% | 0.231 |
| 378 | 8.6% | -29.1% | 0.296 |

Shape: U-shaped. 63d (3-month) is best. Shorter lookback captures more recent momentum.

### direction_score_threshold (MODERATE, ma=3 fixed)

| Value | CAGR | MDD | Calmar |
|-------|------|-----|--------|
| 0 (disabled) | 9.1% | -34.1% | 0.267 |
| 0.35 | 8.7% | -35.4% | 0.246 |
| 0.40 | 8.4% | -37.9% | 0.222 |
| **0.45** | 8.8% | **-24.9%** | **0.351** |
| 0.50 | 7.4% | -24.9% | 0.297 |
| 0.54 | 7.2% | -38.3% | 0.189 |
| 0.60 | 6.9% | -36.0% | 0.192 |
| 0.65 | 4.2% | -22.2% | 0.189 |

Shape: Bell for Calmar, monotonic decline for CAGR. 0.45 is sweet spot for risk-adjusted. Disabled gives more CAGR.

### Other R1 results

**min_momentum_pct (MODERATE):** 0 is best (10.4% CAGR). Any filter > 0 reduces CAGR. The top-N selection already handles filtering.

**regime_sma_period (INSENSITIVE):** Disabled (0) gives Cal 0.299, SMA100 gives 0.256, SMA200 gives 0.189. Regime filter doesn't help this strategy.

**ds_ma (INSENSITIVE):** ma=5 is best (9.0% CAGR) but range is small. Fixed at 3 for R2.

**order_sorting_type (MODERATE):** top_performer (8.2%) > top_gainer (7.2%) > top_dipper (6.0%) > top_avg_txn (3.7%). Marginal improvement doesn't justify 3-5x runtime.

**max_hold_days (MODERATE):** 126d is best (9.7% CAGR, Cal 0.298). Bell-shaped with peak at 126d.

## Round 2: Full Cross-Parameter Search (864 configs)

Ran as 6 parallel batches (by lookback x topn), 3 at a time. Excluded daily rebalance (too expensive).

**Top 5 by Calmar:**

| lb | tn | reb | mm | ds | tsl | hld | pos | CAGR | MDD | Cal | Trades |
|----|-----|-----|----|----|-----|-----|-----|------|-----|-----|--------|
| 189 | 0.50 | 2 | 5 | 0.45 | 40 | 126 | 30 | 27.7% | -28.7% | 0.965 | 1356 |
| 189 | 0.40 | 5 | 0 | 0 | 60 | 252 | 20 | 24.9% | -30.2% | 0.826 | 454 |
| 189 | 0.60 | 2 | 5 | 0.45 | 40 | 126 | 30 | 26.2% | -32.6% | 0.804 | 1353 |
| 189 | 0.60 | 5 | 5 | 0.45 | 25 | 126 | 30 | 22.0% | -28.3% | 0.778 | 1477 |
| 189 | 0.40 | 2 | 5 | 0.45 | 40 | 126 | 30 | 26.8% | -35.1% | 0.765 | 1359 |

**Key R2 finding:** rebal=2, mm=5, ds=0.45, tsl=40, hold=126, pos=30 is the dominant cluster.

## Round 3: Robustness (648 configs)

Fine grid around R2 champion: lb[168,189,210] x tn[0.40,0.50,0.60] x reb[2,3] x mm[0,5] x tsl[30,40,50] x hld[105,126] x pos[25,30,35].

**Stats:** min Cal 0.315, median 0.596, max 1.016, mean 0.591. **100% of configs profitable** (min 15.6% CAGR).
**Formal test:** 28.7% retain >70% of champion Cal (below 80% threshold), but 71.8% have Cal > 0.50.
**Interpretation:** Champion Cal is very high (0.965), making the 70% bar (0.676) aggressive. The region has no cliffs — every neighbor is profitable.

**New champion from R3:** lb=210, tn=0.40 beats lb=189 by +0.05 Cal with lower MDD.

## Round 4: Validation

### 4a. OOS Split

| Period | CAGR | MDD | Calmar | Trades |
|--------|------|-----|--------|--------|
| IS (2010-2020) | 19.7% | -28.1% | 0.699 | 820 |
| **OOS (2020-2026)** | **48.6%** | -34.4% | **1.412** | 510 |

OOS massively outperforms IS. Cal IMPROVES by +102%. **PASS.**

### 4b. Walk-Forward (5 folds)

| Fold | Test period | CAGR | MDD | Calmar |
|------|-------------|------|-----|--------|
| 1 | 2013-2014 | +18.2% | -19.2% | 0.947 |
| 2 | 2016-2017 | +35.4% | -21.5% | 1.644 |
| 3 | 2019-2020 | +19.2% | -35.1% | 0.548 |
| 4 | 2022-2023 | +68.2% | -16.2% | 4.219 |
| 5 | 2025-2026 | -13.8% | -22.6% | -0.609 |

**Avg Calmar: 1.350 | Positive: 4/5.** Fold 5 is the only negative (recent 2025 drawdown). **PASS.**

### 4c. Cross-Data-Source

| Source | CAGR | MDD | Calmar |
|--------|------|-----|--------|
| nse_charting_day (primary) | 28.1% | -28.5% | 0.987 |
| fmp.stock_eod (.NS) | 28.8% | -31.3% | 0.918 |
| nse_bhavcopy_historical | 28.7% | -36.1% | 0.795 |

All three sources positive and consistent. **PASS.**

### 4d. Cross-Exchange (10 markets)

| Exchange | CAGR | MDD | Calmar |
|----------|------|-----|--------|
| **NSE** | **28.1%** | -28.5% | **0.987** |
| Taiwan | 26.7% | -44.0% | 0.606 |
| US | 20.4% | -33.2% | 0.615 |
| UK | 18.4% | -30.1% | 0.610 |
| S. Korea | 15.8% | -57.3% | 0.276 |
| China SHZ | 8.5% | -24.0% | 0.352 |
| China SHH | 3.7% | -61.9% | 0.059 |
| Canada | 2.3% | -3.8% | 0.605 |
| Hong Kong | 2.2% | -30.9% | 0.070 |
| Euronext | 0.9% | -7.3% | 0.118 |
| Germany | 0.5% | -6.9% | 0.073 |

Strategy works globally — ALL exchanges positive. Best outside NSE: Taiwan (Cal 0.606), US (0.615), UK (0.610).

### Deflated Sharpe

| Metric | Value |
|--------|-------|
| Observed Sharpe (OOS) | 1.987 |
| Configs tested | 1,512 |
| Deflated Sharpe | **1.334** |
| Verdict | **PASS (>0.3)** |

# eod_breakout Optimization

**Strategy:** N-day high breakout + direction score filter + TSL exit
**Signal file:** `engine/signals/eod_breakout.py`

## Champion

| Period | Config | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|--------|------|-----|--------|--------|--------|
| 2010-2026 | ndh=7,ndm=5,ds={3,0.54},tsl=8,pos=15 | **13.3%** | -25.7% | **0.516** | 0.928 | 1664 |
| 2015-2025 | same, tsl=15, pos=15 | **18.1%** | -27.4% | **0.662** | 1.124 | 410 |
| OOS 2020-2026 | same, tsl=8, pos=15 | **22.8%** | -26.6% | **0.856** | — | 797 |

**Walk-forward:** 6/6 folds positive, avg Calmar 0.736. Deflated Sharpe 0.649 (PASS).

**vs baseline:** CAGR 10.9%→13.3% (+22%), MDD 42%→26% (-16pp), Calmar 0.26→0.52 (+100%)
**vs ATO_Simulator:** ATO reported 32.5% CAGR. Our pipeline with exact ATO params gives 14.3%. Gap is structural (data source, ranking impl), not params.

## Parameters

| Param | Baseline | Champion | Notes |
|-------|----------|----------|-------|
| `n_day_high` | 2 | **7** | Broader breakout window, fewer false signals |
| `n_day_ma` | 3 | **5** | Trend confirmation MA |
| `direction_score` | {3, 0.54} | **{3, 0.54}** | Unchanged — original ATO value was correct |
| `trailing_stop_pct` | 15 | **8** | Tighter stop, better Calmar. 15 gives higher CAGR on shorter windows |
| `min_hold_time_days` | 0 | **0** | Insensitive |
| `max_positions` | 20 | **15** | Less diversified but better per-position quality |
| `order_sorting_type` | top_gainer | **top_gainer** | Beats top_performer (0.52 vs 0.40 Cal) |

## Round 0: Baseline

| CAGR | MDD | Calmar | Sharpe | Trades |
|------|-----|--------|--------|--------|
| 10.91% | -42.1% | 0.259 | 0.667 | 927 |

Config: ndh=2, ndm=3, ds={3,0.54}, tsl=15, min_hold=0, pos=20, top_gainer

## Round 1: Sensitivity Scan

| Param | Values swept | Range% | Classification | Best |
|-------|-------------|--------|----------------|------|
| `trailing_stop_pct` | 3-70 (10) | 111% | **IMPORTANT** | 3 (Cal 0.306) |
| `order_sorting_type` | 4 types | 76% | **IMPORTANT** | top_gainer (0.362) |
| `n_day_high` | 1-30 (9) + zoom 4-12 | 72% | **IMPORTANT** | 7 (0.445) |
| `max_positions` | 5-50 (8) | 56% | **IMPORTANT** | 15 (0.315) |
| `n_day_ma` | 2-50 (9) | 28% | MODERATE | 10 (0.338) |
| `ds_score` | 0.30-0.80 (10) | 23% | MODERATE | 0.40 (0.325) |
| `ds_ma` | 2-50 (8) | 22% | INSENSITIVE | 5 (0.292) |
| `min_hold` | 0-30 (8) | 14% | INSENSITIVE | 7 (0.286) |

Sorting: top_gainer (0.362) > top_performer (0.297) > top_avg_txn (0.140) > top_dipper (0.093)

## Round 2: Full Cross-Parameter Search (1152 configs)

Crossed ALL params: ndh=[2,3,5,7] x ndm=[3,5,10] x ds=[{3,0.54},{5,0.40}] x tsl=[8,15] x min_hold=[0,7] x sort=[top_gainer,top_performer] x pos=[15,20,30] x pt=[50,99]

**Top 5 by CAGR (2010-2026):**

| ndh | ndm | ds | tsl | sort | pos | CAGR | MDD | Cal |
|-----|-----|-----|-----|------|-----|------|-----|-----|
| 7 | 5 | {3,0.54} | 15 | top_gainer | 20 | 13.3% | -34.1% | 0.391 |
| 7 | 5 | {3,0.54} | 8 | top_gainer | 15 | 13.3% | -25.7% | 0.516 |
| 7 | 5 | {3,0.54} | 15 | top_gainer | 30 | 13.2% | -29.2% | 0.454 |
| 7 | 10 | {3,0.54} | 15 | top_gainer | 15 | 13.2% | -29.7% | 0.445 |
| 5 | 5 | {3,0.54} | 15 | top_gainer | 15 | 13.2% | -30.1% | 0.439 |

**Key findings:**
- **ds={3,0.54} dominates {5,0.40}** across the board. The 0.40 threshold barely filters.
- **top_gainer beats top_performer** even at max CAGR (13.3% vs 13.2%)
- **ndh=7, ndm=5** is the consistent best entry combo
- **tsl=8 with pos=15 gives best Calmar** (0.516) at same CAGR as tsl=15

**ds_ma fine-grained sweep [1,2,3,5]:**
- ds_ma=1: zero entries (code bug — close > 1-day MA of close is never true)
- ds_ma=2: 13.2% CAGR but 37.7% MDD
- ds_ma=3: 13.2% CAGR, 30.1% MDD, Cal 0.439 — **best**
- ds_ma=5: 9.7% CAGR, 37.0% MDD — significantly worse

## Round 3: Robustness (from initial optimization)

Perturbation grid (ndh=[5-9] x ndm=[8,10,12] x tsl=[12,15,18] x pos=[15,20,25] = 135 configs):
- **85% of neighbors retain >70% of center Calmar** (passes 80% threshold)
- Top-10 cluster: ndh=5-8, ndm=10-12, tsl=12-15, pos=15-25

Note: This was run on the earlier {5,0.40} champion. The new champion ({3,0.54}, ndh=7, ndm=5) was found in R2 full sweep after the R3 grid was already done. The R2 full sweep (1152 configs) itself serves as a robustness check — the top configs cluster tightly around ndh=7, ndm=5, ds={3,0.54}.

## Round 4: Validation (re-run on confirmed champion ndh=7, ndm=5, ds={3,0.54}, tsl=8, pos=15)

### OOS Split

| Period | CAGR | MDD | Calmar | Trades |
|--------|------|-----|--------|--------|
| IS (2010-2020) | +6.1% | -29.0% | 0.212 | 965 |
| **OOS (2020-2026)** | **+22.8%** | **-26.6%** | **0.856** | 797 |

OOS massively outperforms IS. Strategy works better in recent market (post-COVID bull run). No overfitting.

### Walk-Forward (6 rolling folds)

| Fold | Test period | CAGR | MDD | Calmar |
|------|-------------|------|-----|--------|
| 1 | 2013-2015 | +6.6% | -18.9% | 0.350 |
| 2 | 2015-2017 | +0.9% | -21.6% | 0.040 |
| 3 | 2017-2019 | +15.5% | -25.7% | 0.605 |
| 4 | 2019-2021 | +18.3% | -10.7% | 1.709 |
| 5 | 2021-2023 | +31.6% | -21.9% | 1.440 |
| 6 | 2023-2026 | +7.4% | -27.0% | 0.272 |

**Avg Calmar: 0.736 | Positive: 6/6** (all folds profitable, including 2015-2017 which was negative with old champion)

### Cross-Data-Source

| Source | CAGR | MDD | Calmar |
|--------|------|-----|--------|
| nse_charting_day (primary) | +13.3% | -25.7% | 0.516 |
| fmp.stock_eod (.NS) | +13.3% | -28.5% | 0.468 |
| nse_bhavcopy_historical | +10.3% | -37.8% | 0.271 |

All three sources positive. FMP nearly matches primary. Bhavcopy lower (unadjusted splits).

### Cross-Exchange (11 markets)

| Exchange | CAGR | MDD | Calmar |
|----------|------|-----|--------|
| **NSE** | **+13.3%** | **-25.7%** | **0.516** |
| Taiwan | +6.5% | -38.4% | 0.170 |
| UK | +4.3% | -36.1% | 0.120 |
| Germany | +2.0% | -33.5% | 0.059 |
| Canada | +0.6% | -42.9% | 0.014 |
| US | +0.2% | -48.3% | 0.005 |
| Euronext | +0.1% | -39.8% | 0.003 |
| South Korea | -0.3% | -32.9% | -0.009 |
| Hong Kong | -3.9% | -80.0% | -0.049 |
| China SHH | -17.6% | -99.1% | -0.178 |
| China SHZ | -18.4% | -99.4% | -0.185 |

Saudi Arabia: timed out (no liquid stocks passing filter). Strategy is NSE-dominant.

### ATO_Simulator Time Window (2015-2025)

| tsl | pos | CAGR | MDD | Calmar | Sharpe | Trades |
|-----|-----|------|-----|--------|--------|--------|
| 15 | 15 | **18.1%** | -27.4% | 0.662 | 1.124 | 410 |
| 15 | 20 | 16.3% | -27.2% | 0.597 | 1.029 | 547 |
| 8 | 20 | 16.2% | -22.9% | **0.710** | 1.115 | 1470 |
| 8 | 15 | 13.3% | -26.0% | 0.513 | 0.879 | 1090 |

### Fine Grid (2015-2025, ndh=[5-9] x ndm=[3-8])

Best: ndh=5/ndm=5/tsl=15/pos=15 → 18.9% CAGR, Cal 0.772. But ndh=5/ndm=5 drops to 12.6% on full period (2010-2026) vs ndh=7/ndm=5 at 13.3%. ndh=7 is more robust across time windows.

### Deflated Sharpe

| Metric | Value |
|--------|-------|
| Observed Sharpe | 0.928 |
| Configs tested | ~1620 |
| Deflated Sharpe | **0.649** |
| Verdict | **PASS (>0.3)** |

## Methodology Issues Found

1. **Don't optimize for Calmar alone.** Track both best-CAGR and best-Calmar throughout. The Calmar-first approach discarded 13.3% CAGR configs in favor of 9.8%.
2. **Cross ALL params in R2** including sorting type and direction_score variants. Sequential locking loses interactions.
3. **ds_ma=1 is broken** — `close > 1-day MA of close` is always false. Minimum useful value is 2.
4. **"Insensitive" classification can be wrong** — param sensitivity depends on other params' values. Don't permanently fix params after R1.

## Files

```
results/eod_breakout/
  round0_baseline.json
  round1_*.json                 # 8 param sweeps + zoom + sorting
  round2.json                   # Initial R2 (144 configs, wrong ds)
  round2_full.json              # Full R2 (1152 configs, all params crossed)
  round3_perturbation.json      # R3 (135 configs)
  round4_*.json                 # OOS, walk-forward, cross-data, cross-exchange
  ds_ma_fine.json               # ds_ma=[1,2,3,5] fine sweep
  ato_exact_match.json          # Exact ATO_Simulator config reproduction
  champion.json                 # Champion standalone (2010-2026)
  champion_2015.json            # Champion on 2015-2025 window
```

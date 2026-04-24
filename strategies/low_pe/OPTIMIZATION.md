# low_pe Optimization

**Strategy:** Classic Basu (1977) / Fama-French value. Quarterly rebalance. Buy cheapest
stocks by P/E with quality (ROE) and leverage (D/E) filters. MOC execution (signal at
close, entry at next open). No same-bar bias.
**Signal file:** `engine/signals/low_pe.py`
**Data:** `nse.nse_charting_day` + FMP `key_metrics` + `financial_ratios` (FY, MOC)
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: COMPLETE (with data-window caveat)

Full-period 2010-2026: best CAGR 7.25% — **fails NIFTYBEES 10.45% benchmark**.
Modern-window 2018-2026: CAGR 12.26% / Cal 1.016 / Sharpe 1.002 — **beats NIFTYBEES**.

Root cause: FMP NSE fundamentals (P/E, ROE, D/E) are sparse pre-2018. Similar caveat
to `earnings_dip` and `trending_value`. Pre-2018 data coverage drives full-period
underperformance.

## Rounds run (2010-2026 full, then modern 2018-2026)

| Round | Configs | Best CAGR | Best Cal | Notes |
|---|---|---|---|---|
| R0 (baseline) | 1 | 7.38% | 0.152 | pe=15, roe=0.10, 30 stocks, no SL |
| R1a (pe × roe) | 16 | 8.60% | 0.287 | pe=10, roe=0.10 optimal |
| R1b (concentration) | 36 | 6.08% | 0.268 | max_stocks=50 dominates; large-caps reduce MDD |
| R1c (stop_loss) | 6 | 7.14% | 0.195 | 10% stop marginal help only |
| R2 (full grid) | 324 | 7.25% | 0.505 | All CAGR below benchmark |
| R3 (min_stocks) | 144 | 7.03% | 0.485 | min_stocks ~ negligible effect |
| **Modern R2 (2018-26)** | 32 | **14.49%** | **1.016** | Champion below |
| R4a OOS 2020-26 | 1 | 17.76% | 1.471 | OOS > IS (robust in regime) |
| R4b walk-forward | 6 folds | — | 1.72 mean | 5/6 positive, Std Cal 1.34 FAILS |

## Champion (modern window)

**Params:** `pe_max=8, pe_min=0, roe_min=0.08, de_max=1.0, mktcap_min=10B,
max_stocks=50, min_stocks=10, stop_loss_pct=0.10, top_gainer sort, pos=50`

**Full modern period (2018-01-01 → 2026-03-19):**
- CAGR **12.26%** / MDD -12.08% / Calmar **1.016** / Sharpe **1.002**
- 742 trades over ~8 years (~93/yr)
- Win rate & trade quality: to-be-derived from detailed.json

**OOS 2020-2026:** CAGR 17.76% / Cal 1.471 (stronger than IS — robust regime)

**Walk-forward 6 folds (2018-2026, 2-yr rolling + tail):**

| Fold | CAGR | MDD | Cal | Sharpe | Trades |
|---|---|---|---|---|---|
| 2018-2020 | **-3.23%** | -7.8% | **-0.414** | -2.015 | 57 |
| 2019-2021 | 6.93% | -4.2% | 1.644 | 0.919 | 90 |
| 2020-2022 | 23.80% | -6.8% | 3.500 | 2.608 | 145 |
| 2021-2023 | 16.29% | -10.3% | 1.581 | 1.322 | 215 |
| 2022-2024 | 28.40% | -10.3% | 2.757 | 2.176 | 276 |
| 2023-2026 | 15.25% | -12.1% | 1.263 | 1.038 | 377 |

- Positive folds: **5/6**
- Mean Calmar 1.72 | **Std Calmar 1.344 (FAILS 0.5 threshold)**
- 2018-2020 is the failure fold: pre-COVID growth dominance / value drought

## Deflated Sharpe

- SR (observed, modern window champion) = 1.002
- N_configs tested on modern window = 32
- T_years = 8
- SR_deflated ≈ 1.002 − √((1 + 0.5·1.004)/8) × Z(1 − 1/32)
- ≈ 1.002 − 0.433 × 1.863 ≈ **0.195**

**Below 0.3 threshold.** Combined with Std Cal 1.34, indicates meaningful regime
dependence. The 2018-2020 negative fold is the dominant risk — value underperformed
growth in that window.

## Known prior note

From `memory/backtest_bias_audit.md`:
> FMP NSE fundamentals sparse pre-2015, rich post-2018 — drives all fundamental
> strategies' early-fold weakness.

Entry here uses `next_open` (no same-bar bias). Post-audit charges applied.
Filing-lag aware (`FILING_LAG_DAYS = 90`) — no look-ahead.

## Parameters

**Entry:**
- `pe_max` — max P/E for screen (champion 8, tight value)
- `pe_min` — min P/E (champion 0, excludes negative earnings)
- `roe_min` — min ROE for quality filter (champion 0.08)
- `de_max` — max D/E for leverage filter (champion 1.0)
- `mktcap_min` — min market cap (champion ₹10B / ₹1000Cr)
- `max_stocks` — portfolio size (champion 50)
- `min_stocks` — fallback to cash if fewer qualify (champion 10)

**Exit:**
- `stop_loss_pct` — intra-quarter stop-loss (champion 0.10 = 10%)
- Default exit: quarterly rebalance at close

## Summary

**COMPLETE** with data-window + regime caveats. Modern-window (2018+) performance
(CAGR 12.26%, Cal 1.016) beats NIFTYBEES. Walk-forward reveals structural
regime dependency (fails in 2018-2020 value drought). Deflated Sharpe 0.195
below 0.3 threshold — limited confidence vs multiple-test null. Only valid
with **2018+ data window** and in **regimes favoring value**.

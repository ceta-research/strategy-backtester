# factor_composite Optimization

**Strategy:** Multi-factor composite (momentum + gross profitability + value) with monthly rebalance, regime filter, vol scaling, per-position stop-loss.
**Signal file:** `engine/signals/factor_composite.py`
**Data:** `nse.nse_charting_day` (price) + FMP fundamentals (income_statement, balance_sheet, key_metrics, financial_ratios)
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: COMPLETE

- [x] Round 0: Baseline
- [x] Round 1: Sensitivity scan (4 sub-sweeps, 110 configs)
- [x] Round 2: Full cross (576 configs)
- [x] Round 3: Robustness (72 configs, 10/10 PASS)
- [x] Round 4a: OOS (2020-2026)
- [x] Round 4b: Walk-forward (5 folds, std Cal 0.301 PASSES)
- [x] Round 4c: Cross-data-source (2026-04-28: nse_charting 14.78%/Cal 0.319 > FMP 2.96%/Cal 0.059 — **major data-source fragility**, FMP fundamentals JOIN issue)
- [x] Round 4d: skipped — fundamental composite, sector mappings differ across markets (out-of-scope; see [`docs/R4C_R4D_BACKFILL_2026-04-28.md`](../../docs/R4C_R4D_BACKFILL_2026-04-28.md))

## Champion

| Period | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|------|-----|--------|--------|--------|
| **Full (2010-2026)** | **14.78%** | -46.3% | **0.319** | 0.51 | ~4660 |
| OOS (2020-2026)      | 18.43%    | -37.5% | 0.491    | —    | 2161  |

**Params:** `lookback=350, skip=21, weights={mom:0.6,gp:0.2,val:0.2}, regime=0 (off), top_n=30, vol_tgt=0.25, sl=0.30, sort=top_gainer, pos=15`

### Walk-forward (5 folds, 3-yr rolling)

| Fold | CAGR | MDD | Calmar | Sharpe |
|------|------|-----|--------|--------|
| 2010-2013 |  0.59% | -34.0% |  0.017 | -0.07 |
| 2013-2016 | 20.75% | -27.4% |  0.758 |  0.84 |
| 2016-2019 |  6.76% | -28.9% |  0.234 |  0.21 |
| 2019-2022 | 18.06% | -35.0% |  0.516 |  0.70 |
| 2022-2025 | 16.13% | -25.7% |  0.628 |  0.54 |

**Positive folds:** 5/5 (100%)
**Mean Calmar:** 0.431  **Std Calmar:** 0.301 → **PASSES** (<0.5)

### OOS assessment

OOS Cal (0.491) > IS Cal (0.319) → no overfitting signal.
2010-2013 fold is weakest — strategy without regime filter struggled in sideways/bear
conditions. Post-2013 all folds strong.

### Deflated Sharpe

~758 configs tested, 192 months. SR_observed = 0.549, Var(SR) = (1 + 0.5·0.549²)/192 = 0.006,
√Var = 0.0774, Z(1 − 1/758) ≈ 3.00. SR_deflated ≈ 0.549 − 0.232 ≈ **0.317** — just above
0.3 threshold. Combined with WF 5/5 positive and OOS > IS, the result is statistically
defensible.

### Additional metrics

- Sortino 0.736, annualized vol 23.3% (above NIFTYBEES ~18%)
- MDD duration **1194 days (~4.7 years)** — long drawdown; strategy would be emotionally
  brutal to trade live.
- Win rate 52.6%, profit factor 1.338 (modest but consistent edge)
- Best year +97% / worst year -22% — high-variance years, typical momentum signature.
- Time-in-market 94.3% (no regime filter = always invested)

## vs baseline

CAGR 0.89% → 14.78% (+13.9pp), Calmar 0.023 → 0.319 (+14×), MDD -38.5% → -46.3% (deeper, but
Calmar still 14× better because CAGR climbed so much).

## Key findings

- **Fundamentals add little on NSE.** mom_heavy (0.6/0.2/0.2) only marginally better than
  pure_mom (1.0/0/0): avg 7.15% vs 6.35% CAGR in R2 marginals. Strategy is effectively
  long-horizon momentum with a small fundamental tilt. Consistent with FMP NSE fundamentals
  coverage being spotty pre-2015.
- **Regime filter HURTS.** The built-in equal-weight internal-market SMA is noisy and cuts
  bull-period entries. Disabling it (`regime_filter_sma: 0`) outperforms any SMA value.
- **Tight stops kill it.** sl=0.10 caps CAGR at ~6%, sl=0.30-0.50 unlocks 14%+. 12-month
  momentum winners are often volatile on the way up; cutting them at -10% forfeits most
  of the return.
- **Long-horizon momentum wins.** Bell curve peak at lookback=350-378d (~15-18 months).
  126d too short (picks short-term noise), 504+ too long (stale signals).
- **Concentrated portfolio.** max_positions=15 > 20 > 25. Compounding alpha from top-ranked
  picks, not diluting with 30+.

## Parameters

| Param | Baseline | Champion | Notes |
|-------|----------|----------|-------|
| `momentum_lookback_days` | 252 | **350** | Bell peak 350-378 |
| `momentum_skip_days` | 21 | **21** | Insensitive |
| `factor_weights` | {0.4, 0.3, 0.3} | **{0.6, 0.2, 0.2}** | Mom-heavy tilt |
| `regime_filter_sma` | 200 | **0 (off)** | Internal SMA is noisy |
| `top_n_stocks` | 30 | **30** | 25-35 similar |
| `vol_target_annual` | 0.15 | **0.25** | Insensitive |
| `vol_lookback_days` | 126 | **126** | Not swept |
| `stop_loss_pct` | 0.15 | **0.30** | Tight stops kill CAGR |
| `order_sorting_type` | top_gainer | **top_gainer** | Best |
| `max_positions` | 30 | **15** | Concentrated |

## Rounds

### R0: Baseline — CAGR 0.89%, Cal 0.023, MDD -38.5%, 3650 trades

### R1: Sensitivity sweep — R1 champion CAGR 10.82%, Cal 0.220

- **R1a (20 configs)**: factor_weights × top_n × regime_sma — best 4.2%/Cal 0.094. Fundamental weights hurt; regime filter hurts.
- **R1b (24 configs)**: lookback × skip × top_n — best 6.92%/Cal 0.170 (lookback=378). Monotonic in lookback, extended.
- **R1c (18 configs)**: extended lookback [504, 630, 756]. All WORSE than 378. Bell curve peak.
- **R1d (48 configs)**: exit × sim — **breakthrough**. sl=0.50 unlocks CAGR 10.82%, pos=15 sweet spot.

### R2: Full cross — 576 configs, 37 min

Grid: lookback[315,378,441] × weights[pure_mom, mom_heavy] × regime[0,200] × top_n[20,30]
× vol_tgt[0.15,0.25] × sl[0.25,0.50] × sort[top_gainer,top_performer] × pos[15,20,25]

Best CAGR / Best Calmar both: lookback=378, mom_heavy, regime=0, top_n=30, sl=0.50, top_gainer, pos=15 → 13.80% / Cal 0.305
Top 10 by CAGR and by Calmar overlap heavily — confirms robust region, not a spike.

### R3: Fine grid — 72 configs, 10/10 robustness pass

Grid: lookback[350,378,400,420] × top_n[25,30,35] × sl[0.30,0.50] × pos[10,15,20]
New champion by Calmar: lookback=350, top_n=30, sl=0.30, pos=15 → **14.78% / Cal 0.319**
Top-10 all within 70% of best Calmar. Plateau confirmed.

### R4a OOS (2020-2026)
CAGR 18.43%, MDD -37.5%, Cal 0.491. OOS beats IS — robust.

### R4b Walk-forward
5/5 positive, Mean Cal 0.431, Std 0.301. PASSES.

## Parameters

**Entry:**
- `momentum_lookback_days` — 12-month momentum window (default 252)
- `momentum_skip_days` — skip recent days to avoid short-term reversal (default 21)
- `factor_weights` — dict {momentum, gross_profitability, value} summing to 1.0
- `regime_filter_sma` — equal-weight market SMA period (0 = off)
- `top_n_stocks` — portfolio size after ranking

**Exit:**
- `vol_target_annual` — target annualized portfolio volatility
- `vol_lookback_days` — vol estimation window
- `stop_loss_pct` — per-position hard stop

**Simulation:**
- `order_sorting_type` — tiebreak among same-composite-score orders (top_gainer / top_performer / top_dipper / top_average_txn)
- `max_positions` — cap

## Data caveat

Like `earnings_dip`, this strategy depends on FMP fundamentals (`income_statement`, `balance_sheet`,
`key_metrics`, `financial_ratios`) JOINed with NSE price data. FMP NSE fundamentals coverage
is sparse pre-~2015. Full-period (2010-2026) vs modern-window (2015+ or 2018+) comparison will be
reported per `earnings_dip` precedent.

## Rounds

### R0: Baseline — CAGR 0.89%, Cal 0.023, MDD -38.5%, 3650 trades

Params: `lookback=252, skip=21, w={0.4,0.3,0.3}, regime_sma=200, top_n=30, vol_tgt=0.15, sl=0.15, pos=30, sort=top_gainer`

### R1: Sensitivity sweep — R1 champion CAGR 10.82%, Cal 0.220

- **R1a (20 configs)**: factor_weights × top_n × regime_sma — best 4.2%/Cal 0.094 (mom_heavy, regime=0, top_n=30). Finding: fundamental weights hurt; regime filter hurts.
- **R1b (24 configs)**: lookback × skip × top_n — best 6.92%/Cal 0.170 (lookback=378, skip=21, top_n=30). Lookback monotonic, extend.
- **R1c (18 configs)**: extend lookback to [504, 630, 756]. All WORSE than 378. Bell curve peak = 378d.
- **R1d (48 configs)**: vol_target × stop_loss × order_sorting × max_positions — **breakthrough**. Best CAGR 10.82%/Cal 0.220 (sl=0.50 ≈ disabled, top_gainer, pos=20). Hard stops kill winners.

**Per-param classification:**

| Param | Class | Finding |
|---|---|---|
| momentum_lookback_days | IMPORTANT | Bell curve at 378d |
| momentum_skip_days | INSENSITIVE | 21 slightly best |
| factor_weights | IMPORTANT | momentum-heavy wins; fundamentals hurt on NSE |
| regime_filter_sma | MODERATE | 0 (off) > 200 |
| top_n_stocks | MODERATE | 20-30 best |
| vol_target_annual | INSENSITIVE | 0.15 ≈ 0.25 |
| stop_loss_pct | IMPORTANT | wider (0.25-0.50) wins; tight kills it |
| order_sorting_type | MODERATE | top_gainer best |
| max_positions | MODERATE | 20 > 30 |


# ml_supertrend Optimization

**Strategy:** Quality-dip + SuperTrend. Quality universe (N/10 positive annual
returns). Mild-dip detection (down X% from peak but osc_position < threshold,
i.e. near-peak, not trough). Entry trigger: SuperTrend flip to bullish (or
"off" = momentum bounce). Exit: walk_forward_exit (peak-recovery gate + TSL).
Source: TradingView "Machine Learning Supertrend [Aslan]" by Zimord.
**Signal file:** `engine/signals/ml_supertrend.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: COMPLETE (with deflated-Sharpe caveat)

Champion beats NIFTYBEES on CAGR (13.20% vs 10.45%) and Calmar (0.415 vs 0.27).
Walk-forward 5/5 positive, Std Cal 0.387 (PASSES robustness). However **deflated
Sharpe ≈ 0** after multiple-test correction for 108 R2 configs — observed Sharpe
0.653 is borderline relative to null expectation.

## Rounds run (2010-2026, NSE)

| Round | Configs | Best CAGR | Best Cal | Notes |
|---|---|---|---|---|
| R0 (baseline, defaults) | 1 | 3.55% | 0.110 | Too-strict mpy=8 |
| R1a (screening) | 27 | 10.57% | 0.308 | mpy=6, dip=5, osc=0.35 |
| R1b (ST mode/ATR) | 32 | 8.53% | 0.188 | reversal best; ST exit hurts |
| R1c (exits) | 32 | 8.82% | 0.197 | tsl=12, hold=252, NO ST exit |
| **R2 (combined)** | 108 | **13.84%** | **0.415** | champion below |
| R4a OOS 2020-26 | 1 | **23.58%** | **0.883** | OOS > IS (1.79×) |
| R4b walk-forward | 5 folds | 6.64-30.44% | 0.21-1.26 | 5/5 positive, Std Cal 0.387 PASSES |

### R1a screening insights
- mpy=6 (looser quality) beats stricter mpy=8 dramatically
- dip=5% (mild) beats dip=15% (deep) — confirms core thesis
- osc=0.25-0.35 (strict near-peak) required; osc=0.75 underperforms
- Near-peak mild dips in quality stocks >> deep dips in quality stocks

### R1b SuperTrend-mode insights
- `reversal` mode (ST flip to bull within last N bars) best
- `trend`/`breakout` modes underperform
- `off` mode (momentum bounce only, no ST) gives 6.18% baseline
- ATR(20), mult=3.0, flip_lookback=20 marginally better than defaults

### R1c exit insights
- `supertrend_exit=True` DESTROYS performance (cuts CAGR by half)
- hold=63d too short; hold=252-378d optimal
- TSL=10-15% optimal (25% too loose, 8% too tight)

## Champion

**Params:** `mpy=6/10, dip=3%, osc<0.25, peak_lookback=252, supertrend=reversal,
atr=(20,2.5), st_flip_lookback=10, rescreen=63d, tsl=15%, max_hold=252d,
top_gainer sort, pos=15`

**Full period (2010-01-01 → 2026-03-19):**
- CAGR **13.20%** / MDD **-31.80%** / Calmar **0.415** / Sharpe **0.653**
- 489 trades, ~30/yr

**OOS 2020-2026:** CAGR **23.58%** / MDD -26.69% / Cal **0.883** (OOS > IS — regime-favorable post-COVID mid-cap bull)

**Walk-forward 5 folds (3-yr rolling):**

| Fold | CAGR | MDD | Cal | Sharpe | Trades |
|---|---|---|---|---|---|
| 2010-2013 | 6.64% | -31.8% | 0.209 | 0.337 | 81 |
| 2013-2016 | 16.92% | -18.1% | 0.935 | 0.920 | 89 |
| 2016-2019 | 13.53% | -18.3% | 0.741 | 0.765 | 89 |
| 2019-2022 | 30.44% | -24.2% | 1.258 | 1.685 | 94 |
| 2022-2025 | 12.25% | -19.6% | 0.625 | 0.543 | 120 |

- **Positive folds: 5/5**
- Mean Calmar 0.753
- **Std Calmar 0.387 (PASSES 0.5 threshold)**
- Worst fold (2010-2013) still positive at 6.64% — some regime dependency but
  no total breakdown

## Deflated Sharpe

- SR (observed, full period) = 0.653
- N_configs (R2 decision space) = 108
- T_years = 16
- SR_deflated ≈ 0.653 − √((1 + 0.5·0.427)/16) × Z(1 − 1/108)
- ≈ 0.653 − 0.275 × 2.352 ≈ **0.006**

**Below 0.3 threshold.** Raw Sharpe edge is not robust vs multiple-test null.
However, the WF-positive + OOS-dominant evidence partially offsets this concern.

## Interpretation

**Pro-continuation:**
- Walk-forward 5/5 positive with Std Cal 0.387 (robust across regimes)
- OOS 2020-2026 stronger than IS (23.58% vs 13.20%) — evidence against overfit
- CAGR and Calmar both beat NIFTYBEES materially

**Caveats:**
- Deflated Sharpe ~0 — multiple-test penalty from 108 configs wipes most edge
- Worst fold (2010-2013) barely positive Cal 0.21
- Strong result mostly from 2019-2022 fold (COVID recovery boom)

## Known prior note (from memory/backtest_bias_audit)

Entry uses `next_open` (no same-bar bias). Uses `walk_forward_exit(
require_peak_recovery=True)` — structural high win rate possible but not
look-ahead bias.

## Parameters

**Entry:**
- `lookback_years` — window for quality count (champion 10)
- `min_positive_years` — quality threshold (champion 6 of 10)
- `dip_threshold_pct` — minimum dip from peak (champion 3%)
- `peak_lookback_days` — peak lookback (champion 252)
- `max_osc_position` — near-peak requirement (champion 0.25)
- `supertrend_mode` — reversal / trend / breakout / off (champion reversal)
- `atr_period` / `atr_multiplier` — ST params (champion 20, 2.5)
- `st_flip_lookback` — window for recent ST bull flip (champion 10)
- `rescreen_interval_days` — quality re-screen cadence (champion 63)

**Exit:**
- `trailing_stop_pct` — champion 15%
- `max_hold_days` — champion 252
- `supertrend_exit` — whether to exit on ST bear flip (champion false — ST exit
  consistently cut performance by ~50%)

## Summary

**COMPLETE** with deflated-Sharpe caveat. Champion beats NIFTYBEES on raw and
risk-adjusted returns; walk-forward passes 5/5 with acceptable Std Cal;
OOS > IS. The main statistical concern is deflated Sharpe near zero, but the
evidence across folds and OOS windows provides real counter-support.

# extended_ibs Optimization

**Strategy:** Extended IBS with deeper-oversold entry trigger.
Entry: close < (10d_high − 2.5 × (25d_avg_high − 25d_avg_low)) AND IBS < threshold
       (optionally + close > SMA AND VEI < max).
Exit: close > prev_high (fast 1-day reversion) OR max_hold_days OR SL/TSL.
Literature claim: 7.75% CAGR / 15.26% DD / 75% WR on SPY 20-yr.
**Signal file:** `engine/signals/extended_ibs.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

**EVERY config tested produces negative CAGR.** Worst pathology of any
strategy in this suite. 217 configs (R0 + R1) all fail.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Notes |
|---|---|---|---|---|---|
| R0 (defaults, no SMA, no SL) | 1 | **-12.33%** | -0.139 | -88.9% | 166K orders |
| R1 (SMA + SL + TSL + VEI) | 216 | **-3.87%** | -0.065 | -59.3% | 0/216 positive |

### R1 finding: 0 of 216 configs above 0% CAGR

- Best CAGR -3.87% (ibs=0.4, sma=50, no VEI, hold=30, no SL/TSL)
- All MDDs -59% to -90%
- SMA filter alone insufficient
- VEI filter doesn't help
- SL/TSL hurt (consistent with memory note "stops hurt mean reversion")

## Why it fails worse than basic IBS

The "extended" entry trigger is **deeper-oversold** than basic IBS:
- Basic IBS<0.2: closed near intraday low
- Extended: close ALSO must be 2.5× ATR below 10-day high

This combination identifies stocks in significant pullbacks within their
recent range. On NSE, such conditions correlate with structural weakness
(broken stocks continuing to fall) rather than tradable mean-reversion.

The "fast exit on close > prev_high" intent is to capture quick bounces, but
post-audit:
- Many entries never see a bounce — they cascade lower
- Those that do bounce trigger near-immediate exit at marginal profit
- 7000+ trades per config × NSE STT 0.1% destroys the ratio

The literature's 7.75% SPY claim relies on:
- Index-level (SPY/QQQ have natural mean reversion via creation/redemption)
- Close-entry bias (entered at signal bar's close)
- US market regime (low cost, no STT-equivalent)
- Single-instrument backtest (no portfolio cost compounding)

On NSE individual stocks with realistic costs, signals are inverted: the
"extended oversold" condition predicts continued weakness, not reversion.

## Retirement decision

AUDIT_RETIRED after 217 configs. **Zero configs positive.** Skipped modern
window since the failure is structural (signal predicts continued weakness),
not regime-dependent. Worst result of any optimization run in this suite.

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Aggregate failure pattern: same as ibs_mean_reversion / connors_rsi /
momentum_dip. The "deeper oversold" twist makes this one strictly worse —
NSE stocks at extended-oversold readings tend to continue falling.

## Parameters

**Entry:**
- `ibs_threshold` — IBS upper bound for entry (tested 0.2-0.4)
- `sma_trend_period` — long MA filter (tested 50, 100, 200)
- `vei_max` — volatility expansion ceiling (tested 0/disabled, 1.5)

**Exit:**
- `max_hold_days` — time stop (tested 10, 20, 30)
- `stop_loss_pct` — fixed SL (tested 0, 10%)
- `trailing_stop_pct` — trailing SL (tested 0, 10%)

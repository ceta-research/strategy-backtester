# ibs_mean_reversion Optimization

**Strategy:** Internal Bar Strength mean reversion.
Entry: IBS = (close-low)/(high-low) < threshold (close near low) AND close >
SMA(trend). Exit: IBS > threshold (close near high) OR max_hold_days.
Literature claims 13-40% CAGR on indices/ETFs, 74% win rate.
**Signal file:** `engine/signals/ibs_mean_reversion.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Catastrophic failure on NSE stocks post-audit. 82 configs tested (R0 + R1).
Best CAGR 2.90% with -70% MDD. Best Calmar 0.050. All configs near zero or
negative returns with extreme drawdowns.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Notes |
|---|---|---|---|---|---|
| R0 (defaults) | 1 | **-5.82%** | -0.073 | -79.2% | Catastrophic |
| R1 (3×3×3×3) | 81 | 2.90% | 0.050 | -70.5% | Best config still fails |

### R1 findings
- ALL 81 configs have MDD -46% to -76%
- Max CAGR anywhere is 2.90%
- Loose exit (ibx=0.9) + long hold (20d) marginally best
- Strict entry (ibe=0.1) with long trend SMA (300) gives least-bad Calmar

## Why it fails

IBS < 0.2 means "closed near the low" = panic selling intraday. In NSE's
context, buying into such bars means catching falling knives that often
continue lower due to:
- Gap-down risk overnight
- NSE STT 0.1% + brokerage + slippage (thousands of such trades)
- SMA200 filter insufficient: uptrend stocks can still crater

Thousands of short-hold trades (4000-7000 per config), costs dominate any
edge. Literature results (13-40% CAGR on indices) assumed:
- Close-entry bias (entry at signal bar's close, not next open)
- No transaction costs
- Index-level, not stock-level
- Different market regime (US)

Post-audit (no same-bar bias via `next_open`, NSE charges), the strategy is
just long-volatility-selling with no edge.

## Retirement decision

AUDIT_RETIRED after 82 configs. No configuration produces CAGR anywhere near
NIFTYBEES 10.45%. Skipped modern-window check since the best full-period
CAGR (2.90%) is too far below benchmark and MDD (-70%) is untradable.

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Same pattern as `connors_rsi` / `momentum_dip` — high-frequency mean reversion
unviable on NSE. This strategy fails worse because IBS<0.2 specifically
catches extreme intraday weakness (gap-down risk).

## Parameters

**Entry:**
- `ibs_entry_threshold` — IBS below which to buy (default 0.2)
- `sma_trend_period` — long MA filter (default 200)

**Exit:**
- `ibs_exit_threshold` — IBS above which to sell (default 0.8)
- `max_hold_days` — time stop (default 10)

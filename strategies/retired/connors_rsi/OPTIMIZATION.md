# connors_rsi Optimization

**Strategy:** Classic Larry Connors RSI(2) mean reversion.
Entry: RSI(n) < threshold AND close > SMA(trend). Exit: close > SMA(exit)
OR close < trend SMA (safety) OR held > max_hold_days.
**Signal file:** `engine/signals/connors_rsi.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Full-period 2010-2026 best CAGR **9.36%** — below NIFTYBEES 10.45%.
Modern-window 2018-2026 best CAGR **11.85%** barely above benchmark but Cal
0.269 matches NIFTYBEES (no risk-adjusted edge). Transaction costs dominate.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Notes |
|---|---|---|---|---|
| R0 (baseline) | 1 | 4.12% | 0.071 | 106K orders, MDD -58% — classic cost blowout |
| R1 (sensitivity) | 162 | **9.36%** | 0.674 | None cross NIFTYBEES meaningfully |
| Modern 2018-2026 | 108 | 11.85% | 1.007 | Marginal above benchmark; no Cal edge |

### R1 findings

- Best CAGR config: rp=2, rt=2, sma=300, ex_sma=10, hold=20 → 9.36% / -50% MDD / Cal 0.187
- Best Calmar config: rp=4, rt=2, sma=100, ex_sma=20, hold=10 → 6.52% / -9.7% MDD / Cal 0.674
- All configs generate thousands of trades (minimum 1402)
- Cal 0.674 is above NIFTYBEES 0.27 but CAGR 6.52% is way below

### Modern 2018-2026 findings

- Best CAGR (11.85%) with Cal 0.269 = tied with NIFTYBEES risk-adjusted
- Best Calmar (1.007) at CAGR 9.96% — **below modern NIFTYBEES** (~13% in this
  window driven by post-COVID bull)
- No parameter combination simultaneously beats benchmark on CAGR and Cal

## Why it fails

From `memory/backtest_bias_audit.md`:
> NSE STT (0.1%) is the dominant trading cost; makes high-frequency strategies unviable
> Stop losses HURT mean reversion strategies (confirmed via 36-config sweep)

This strategy generates thousands of short-hold (5-20d) trades. RSI(2)<5 fires
frequently in any uptrending name, producing high turnover. Post-audit NSE
charges (STT 0.1% + STT on sell + brokerage + slippage) compound across trades
and wipe out the mean-reversion edge. The baseline 106K-order run illustrates
the pathology.

The close-entry bias audit does not apply (this uses `next_open` entry), but
the cost structure does.

## Retirement decision

AUDIT_RETIRED after 270 configs (R0 + R1 + modern). Best honest CAGR 11.85%
only in modern window with no Cal edge over NIFTYBEES. Similar to momentum_dip
— mean reversion on NSE doesn't clear transaction costs.

## Parameters

**Entry:**
- `rsi_period` — RSI lookback (default 2)
- `rsi_entry_threshold` — oversold level (default 5)
- `sma_trend_period` — long MA trend filter (default 200)

**Exit:**
- `exit_sma_period` — short MA for mean-reversion complete (default 5)
- `max_hold_days` — time stop (default 20)

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
No bias found; the strategy is simply unprofitable after realistic costs on
NSE.

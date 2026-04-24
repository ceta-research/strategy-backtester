# bb_mean_reversion Optimization

**Strategy:** Bollinger Band mean reversion + SMA(200) trend filter.
Entry: close < lower BB(20, 2σ) AND close > SMA(200).
Exit: close > upper BB OR max_hold_days.
Literature claim (r/algotrading): SPY 20y 10% CAGR / 10.7% DD / Cal ~1.0 / 85% WR.
**Signal file:** `engine/signals/bb_mean_reversion.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best honest CAGR 8.05% across 193 configs — below NIFTYBEES 10.45%.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Notes |
|---|---|---|---|---|
| R0 (defaults BB(20,2), SMA200, hold=400) | 1 | 4.44% | 0.109 | 24K orders |
| R1 (BB×std×SMA×hold) | 192 | **8.05%** | 0.195 | 0/164 above NIFTYBEES |

### R1 findings

- Best CAGR config: bbp=10, bbs=2.5, sma=100, hold=120 → 8.05%/Cal 0.175 (574 trades)
- Best Calmar: bbp=100, bbs=3.0, sma=200, hold=250 → 0.99%/Cal 0.195 (21 trades — too few to be meaningful)
- Wider BB stds (2.5-3.0) generally produce higher CAGR — fewer but better trades
- Longer hold (250-400d) helps capture upper-BB recovery
- 150/164 configs have positive CAGR but none clear NIFTYBEES

## Why it fails

Same NSE pattern as connors_rsi, ibs_mean_reversion, extended_ibs:
- BB lower-band breaks on NSE stocks frequently signal **continued weakness** rather than reversion
- SMA200 filter helps (vs no trend filter) but insufficient
- Even longer holds (400 days) and wider bands (3σ) cap CAGR around 7-8%
- Hundreds-to-thousands of trades × NSE STT 0.1% drag

The literature 10% SPY CAGR claim relies on:
- Index-level mean reversion (creation/redemption + composition stability)
- Close-entry bias
- US cost structure
- Multi-ETF diversification (claim was on ETF basket, not single stocks)

On NSE individual stocks, the strategy's edge is too thin to overcome costs.

## Retirement decision

AUDIT_RETIRED after 193 configs. Best CAGR 8.05% / Cal 0.175 cannot match
NIFTYBEES 10.45% / Cal 0.27. Fifth NSE mean-reversion strategy retired in
this session (after momentum_dip, connors_rsi, ibs_mean_reversion, extended_ibs).

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Pattern is now well-established: NSE stocks at oversold conditions
predominantly continue weakness, breaking the mean-reversion thesis that
holds in indices/ETFs.

## Parameters

**Entry:**
- `bb_period` — BB lookback (tested 10-100)
- `bb_std` — BB stddev multiplier (tested 1.5-3.0)
- `sma_trend_period` — long MA filter (tested 100-300)

**Exit:**
- `max_hold_days` — time stop (tested 60-400)

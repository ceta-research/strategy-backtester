# momentum_dip Optimization

**Strategy:** Buy RSI(14) oversold dips in top-N momentum winners. Exit at profit target
or max hold. Based on Reddit r/algotrading post that claimed 81.6% WR.
**Signal file:** `engine/signals/momentum_dip.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Honest post-audit performance fails to clear NIFTYBEES buy-and-hold 10.45% CAGR benchmark.

### Best found across 378 configs (R0-R2)

| Round | Best CAGR | Best Cal | MDD |
|---|---|---|---|
| R0 (baseline) | 3.85% | 0.078 | -49% |
| R1 (162 configs) | 5.61% | 0.137 | -45% |
| R2 (216 configs, extended ranges) | **8.62%** | 0.205 | -55% |

Best Calmar found: 0.205 (rsi=25, lb=252, top_n=75, pt=0.08, mh=21 — CAGR 8.05%).
**Both below NIFTYBEES 10.45% CAGR / ~0.27 Cal.**

### Marginal findings
- rsi=20-25 (stricter oversold) beats 30-35
- momentum_lookback=252d-378d (longer) beats 63-126
- top_n=75 optimal (concentrated enough, large enough pool)
- profit_target=0.08-0.15 (larger wins help)
- max_hold=21-63d (more time for target to hit)
- All best params were at the edges of what's reasonable — no meaningful interior optimum.

### Why it fails

The Reddit claim of "81.6% WR, 31% in 2 months" relied on:
- Close-entry bias (entering at signal bar's close rather than next open)
- No transaction costs
- A specific favorable 2-month sample period

Post-audit (no same-bar bias, NSE charges applied), the strategy generates too many
low-edge trades. RSI oversold in momentum winners happens frequently, and the 3-8%
profit target with 10-21d hold doesn't consistently capture enough edge to overcome
charges and occasional deep losers.

### Retirement decision

Marked AUDIT_RETIRED. 378 configs tested across R0-R2. Best honest CAGR 8.62% is
2pp below NIFTYBEES buy-and-hold benchmark. No reasonable parameter combination
found to beat the index. R3/R4 not run — no point validating a below-benchmark
strategy.

## Known prior note (from memory/backtest_bias_audit)
> Reddit backtest claims ~50% overstated (close-entry bias, no costs)

Entry here uses `next_open` (no same-bar bias). Post-audit charges applied.

## Parameters

**Entry:**
- `rsi_threshold` — RSI(14) level for oversold
- `momentum_lookback_days` — trailing return period for ranking
- `top_n` — momentum universe size
- `rerank_interval_days` — re-rank cadence

**Exit:**
- `profit_target_pct`
- `max_hold_days`


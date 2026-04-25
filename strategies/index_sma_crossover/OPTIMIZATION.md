# index_sma_crossover Optimization

**Strategy:** Classic SMA crossover trend-following on index.
Entry: SMA(short) crosses above SMA(long) → buy at next open.
Exit: SMA(short) crosses below SMA(long) | SL | max_hold.
**Signal file:** `engine/signals/index_sma_crossover.py`
**Data:** `nse.nse_charting_day` (NIFTYBEES)
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best 5.43% CAGR / Cal 0.324 across 193 configs. Calmar BEATS NIFTYBEES
buy-and-hold (0.27) by ~20% but CAGR LOSES by ~5pp (5.43 vs 10.45).
Marginal risk-adjusted edge insufficient to justify the strategy.

## Bias check

Verified bias-clean:
- Crossover detection uses today's SMA + shift(1) (prior bar) — no look-ahead
- Entry next_open T+1
- Exit on close, forward iteration

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Best Trades | Notes |
|---|---|---|---|---|---|---|
| R0 (50/200 golden cross) | 1 | -0.66% | -0.061 | -10.76% | ~10 | Vanilla |
| R1 (SMA × SL × max_hold) | 192 | **5.43%** | 0.324 | -16.74% | 18 | 48/192 positive |

R1 sweep:
- sma_short: [5, 10, 20, 50]
- sma_long: [50, 100, 150, 200]
- stop_loss_pct: [0, 0.05, 0.10, 0.15]
- max_hold_days: [0, 60, 252]

Best config: sma_short=20, sma_long=200, SL=0.10, max_hold=252.
Top configs by CAGR cluster around SMA(5)/(20) and SMA(20)/(200) variants.

## Comparison to NIFTYBEES buy-and-hold

| Metric | This strategy (best) | NIFTYBEES B&H |
|---|---|---|
| CAGR | 5.43% | 10.45% |
| MDD | -16.74% | ~-41% |
| Calmar | 0.324 | ~0.27 |
| Sharpe | 0.42 | ~0.45 |

Strategy reduces drawdown by 60% but cuts CAGR in half. The Calmar edge
(0.324 vs 0.27, ~20% better) is too thin to justify operational complexity
when buy-and-hold delivers nearly the same risk-adjusted return with
simpler execution.

## Why it underperforms on NIFTYBEES

1. **Crossover lag.** By the time SMA(short) crosses SMA(long), 30-50%
   of the move is already done. Entry is consistently late.
2. **Whipsaw on choppy markets.** 2015-2016 and 2018-2019 NIFTY range
   markets generated 5-8 false crossovers, each costing STT 0.1% +
   small drawdown.
3. **Time-out-of-market drag.** Strategy is in cash 30-40% of the time
   (during downtrends and chop). NIFTYBEES gradual uptrend means cash
   periods forfeit compounding.
4. **No volatility/regime adjustment.** Same rules apply equally in
   2008-style crashes and 2017-style melt-ups.

## Same pattern as other index strategies

- `index_breakout`: best Cal 0.299 vs B&H 0.27, CAGR 5.10% vs 10.45%
- `index_sma_crossover`: best Cal 0.324 vs B&H 0.27, CAGR 5.43% vs 10.45%
- `index_dip_buy`: 0/432 positive
- `index_green_candle`: best 1.30% CAGR

All NIFTYBEES single-instrument timing strategies fail to materially
beat buy-and-hold. The honest verdict: NIFTYBEES has too persistent
an uptrend for mechanical timing to add value after costs.

## Retirement decision

AUDIT_RETIRED for **insufficient edge over buy-and-hold**.
16th NSE strategy retired this session.
The 20% Calmar improvement is real but small; CAGR-50% drag dominates.

## Recommendation

If revisiting:
1. Apply leverage during in-market periods (2× NIFTYBEES futures during
   golden-cross regime) to make up CAGR shortfall while keeping Cal advantage
2. Test on **higher-MDD assets** (BANKBEES, sector ETFs, individual cyclicals)
   where SMA crossover's drawdown reduction has more value
3. Combine with **tactical asset allocation** (rotate between NIFTYBEES /
   GOLDBEES / LIQUIDBEES based on multi-asset crossover)

## Parameters (not optimized)

**Entry:**
- `sma_short` — fast MA (tested 5, 10, 20, 50)
- `sma_long` — slow MA (tested 50, 100, 150, 200)

**Exit:**
- `stop_loss_pct` — fixed SL (tested 0, 0.05, 0.10, 0.15)
- `max_hold_days` — time stop (tested 0, 60, 252)
- (SMA cross-down hardcoded as primary exit)

# index_green_candle Optimization

**Strategy:** Buy after N consecutive green candles, exit on M consecutive
red candles OR take-profit OR stop-loss.
Entry: green_streak >= N → buy at next open.
Exit: red_count >= M | TP% | SL%.
**Signal file:** `engine/signals/index_green_candle.py`
**Data:** `nse.nse_charting_day` (NIFTYBEES)
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best CAGR 1.30% / Cal 0.115 across 193 configs (R0 + 192 R1). NIFTYBEES
buy-and-hold over the same period is **10.45% / Cal ~0.27**. Crushed.

## Bias check

Verified bias-clean:
- `is_green = close > open`, observable at close T
- Green streak = rolling sum, prior closes only
- Entry at next_open T+1
- Exit forward iteration starts at i+2 (skips entry day)

No same-bar bias.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Best Trades | Notes |
|---|---|---|---|---|---|---|
| R0 (2 green / 1 red) | 1 | -0.49% | -0.058 | -8.48% | ~280 | Vanilla |
| R1 (green × red × TP × SL) | 192 | **1.30%** | 0.115 | -11.35% | 32 | 21/192 positive |

R1 sweep:
- green_candles: [2, 3, 4, 5]
- red_candles_exit: [1, 2, 3, 5]
- take_profit_pct: [0, 0.05, 0.10, 0.20]
- stop_loss_pct: [0, 0.05, 0.10]

Best config: green=5, red=any, TP=0.20, SL=any → 1.30% CAGR / 32 trades.
TP=20% essentially never triggers (NIFTYBEES rarely makes 20% in a single
trade window), so the strategy reduces to "buy after 5 greens, sell on red(s)".

## Why it fails on NIFTYBEES

1. **Mean reversion bias.** After 5 green candles, NIFTYBEES is more likely
   to consolidate or pull back than continue. Entry-at-next-open buys the
   peak of the short-term run.

2. **Tight red exit cuts winners.** With red_candles_exit=1, every minor
   doji/down day exits. With NIFTYBEES averaging ~50% red days, expected
   hold = 2 days. Forfeits all multi-week trends.

3. **Cost dominance.** 280 trades × NSE STT 0.1% per round-trip = 28%
   total cost over 16 years. Strategy doesn't generate enough alpha to
   overcome this.

4. **No regime/quality filter.** Pure pattern strategy with no market
   context. Buys after 5 greens equally in 2008 chaos and 2020 V-recovery.

## Comparison to similar retirements

Same pathology as `index_dip_buy`, `index_breakout`:
- Pattern signal on NIFTYBEES with mechanical exit
- Cost-of-frequent-trading > pattern edge
- Always loses to buy-and-hold

## Retirement decision

AUDIT_RETIRED for **failure to beat NIFTYBEES buy-and-hold**.
15th NSE strategy retired this session.

## Recommendation

If revisiting:
1. Combine with **regime filter** (only buy after greens during NIFTYBEES > SMA200)
2. **Higher entry threshold** (10+ greens, very rare → fewer trades, less cost drag)
3. Test on **higher-volatility ETFs** where pattern persistence > mean-reversion
4. Use **trailing stop** instead of red-candle exit (let winners run longer)

## Parameters (not optimized)

**Entry:**
- `green_candles` — N consecutive (tested 2, 3, 4, 5)

**Exit:**
- `red_candles_exit` — M consecutive (tested 1, 2, 3, 5)
- `take_profit_pct` — fixed TP (tested 0, 0.05, 0.10, 0.20)
- `stop_loss_pct` — fixed SL (tested 0, 0.05, 0.10)

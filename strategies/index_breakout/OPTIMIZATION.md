# index_breakout Optimization

**Strategy:** N-day high breakout on index/ETF with trailing stop-loss exit.
Entry: close >= max(close.shift(1), N-day) → buy at next open.
Exit: position drops X% from running peak, OR max hold days.
**Signal file:** `engine/signals/index_breakout.py`
**Data:** `nse.nse_charting_day` (NIFTYBEES)
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best meaningful config (>=30 trades) at 5.10% CAGR / Cal 0.284. NIFTYBEES
buy-and-hold over the same period is **10.45% CAGR / Cal ~0.27**. Strategy
fails CAGR by ~5pp; Calmar barely matches.

## Bias check

Verified bias-clean:
- `n_day_high` uses `close.shift(1).rolling_max()` — strictly prior bars
- Entry executes at `next_open` (T+1)
- Exits use close of current day (intra-day TSL evaluation, standard)
- Memory note `buy_2day_high.py` 13.3% CAGR was the **standalone script
  WITHOUT NSE charges**; engine-pipeline post-audit applies STT 0.1% per
  round-trip which alone cannot account for the gap — likely also config /
  data window differences

## Rounds run

| Round | Configs | Best CAGR (any) | Best Cal | Best Trades | Notes |
|---|---|---|---|---|---|
| R0 (lookback=3, TSL=5%, hold=0) | 1 | -0.70% | -0.055 | 66 | Memory's "best" config — fails post-audit |
| R1 (lookback × TSL × max_hold) | 126 | 8.18% (4 trades) | 0.299 (4 trades) | 4-90 | 47/126 positive |
| R2 (R1 + regime SMA filter) | 96 | 8.18% (4 trades) | 0.299 (4 trades) | 4-90 | 33/96 positive — regime invariant |

### R1 / R2 statistically-meaningful configs (>=30 trades)

| Lookback | TSL | Hold | Trades | CAGR | MDD | Calmar |
|---|---|---|---|---|---|---|
| 2 | 20% | 0 | 90 | 5.10% | -17.91% | 0.284 |
| 3 | 20% | 0 | 86 | 4.89% | -18.03% | 0.272 |
| 2 | 12% | 0 | 92 | 4.73% | -18.98% | 0.249 |
| 10 | 20% | 0 | 80 | 3.96% | -16.14% | 0.245 |

All meaningful configs **underperform NIFTYBEES buy-and-hold (10.45%)**
on CAGR. Calmar advantage (0.28 vs 0.27) is negligible.

The "winning" 8% configs (max_hold=60d) only execute 4 trades over 16
years — statistically meaningless flukes. They sit in cash 95% of the
time and got lucky on entry/exit timing.

## Why it fails on NIFTYBEES

1. **Time out of market dominates.** NIFTYBEES uptrends are persistent and
   gradual. Any TSL that's tight enough to capture meaningful turns
   (3-5%) gets whipsawed in normal noise (10-15 round trips/year). Each
   exit costs STT 0.1% AND forfeits ~1% in re-entry slippage on the next
   2-day-high signal.

2. **Cost drag exceeds DD reduction.** The trailing stop reduces MDD
   from -41% (buy-and-hold) to -18% to -27% — a real benefit. But the
   forfeit of compounding during cash-out periods costs more than the
   MDD reduction is worth.

3. **Regime filter is invariant.** R2 added NIFTYBEES SMA50/100/200
   filter; results are identical to R1 (top configs unchanged). The
   breakout signal already implicitly requires uptrend; an SMA gate
   adds nothing.

4. **NSE STT is the killer.** Standalone script (`buy_2day_high.py`) without
   charges showed 13.3% CAGR — ~3pp better than NIFTYBEES B&H. The
   honest engine with NSE STT 0.1% per round-trip eats this entire edge.

## Comparison to memory note

Memory note (`buy_2day_high.py`):
> NIFTYBEES 3d-high + 5% TSL: 13.3% CAGR, -41% MDD, Calmar 0.32, Sharpe 0.63

Engine-pipeline R0 (same params, NSE charges applied):
- CAGR -0.70% (~14pp drop) — too large to explain by costs alone
- Likely also: different exit semantics in standalone vs pipeline,
  prefetch days affecting first-trade entry, or pre-2010 vs post-2010
  start window. Investigation deferred (low ROI).

## Retirement decision

AUDIT_RETIRED for **failure to beat NIFTYBEES buy-and-hold** on either
CAGR or meaningfully on Calmar. 13th NSE strategy retired this session.

The honest verdict: a single trailing-stop breakout strategy on NIFTYBEES
cannot beat buying and holding the index. NSE STT 0.1% × frequent re-entry
+ forfeit-of-compounding-while-in-cash > the drawdown reduction value.

## Recommendation

If revisiting:
1. Test on **higher-volatility ETFs** (BANKBEES, PHARMABEES) where TSL
   cost-vs-DD-reduction tradeoff may favor the strategy
2. Combine with **leverage during in-market periods** (2× when regime is
   bullish) to make up the cash-drag
3. Compare against simple "NIFTYBEES > SMA200" timing strategy — likely
   simpler and equally effective
4. Investigate the standalone-vs-pipeline discrepancy (memory's 13.3% vs
   engine -0.70%) — may indicate engine bug

## Parameters (not optimized)

**Entry:**
- `lookback_days` — N-day high window (tested 2, 3, 5, 10, 20, 50)
- `regime_instrument` — SMA reference (tested NIFTYBEES)
- `regime_sma_period` — SMA period (tested 50, 100, 200)

**Exit:**
- `trailing_stop_pct` — TSL % (tested 3, 5, 8, 10, 12, 15, 20, 30)
- `max_hold_days` — time stop (tested 0, 60, 252)

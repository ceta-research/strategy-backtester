# gap_fill Optimization

**Strategy:** Intraday gap fill mean reversion.
Entry: Stock gaps down min-max % at open. Buy at the open.
Exit: Sell at the close, same day.
Literature claim: Small gaps (0-0.25%) fill 89-93% of the time. Sharpe ~1.3.
**Signal file:** `engine/signals/gap_fill.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED — same-bar execution bias

R0 baseline shows CAGR **35.27%** / MDD -16.06% / Cal **2.196** / Sharpe
**2.10**. Triggers OPTIMIZATION_PROMPT plausibility threshold (CAGR >20%
mandates bias re-check). Investigation confirms structural same-bar
execution bias — not reproducible in real trading.

## R0 baseline stats

- CAGR 35.27%, MDD -16.06%, Cal 2.196, Sharpe 2.10
- 30,982 trades over 16 years
- Win rate 54.88%, avg win 2.57%, avg loss -1.86%
- **avg_hold_days = 0.0** — same-bar entry + exit confirmed
- Profit factor 1.26, expectancy ₹69,688/trade

## Why the result is fictional

The strategy logic (lines 31-86 of `gap_fill.py`):

```
gap_pct = (open - prev_close) / prev_close      # uses TODAY's open
entry: gap_pct in (-max_gap, -min_gap)          # signal triggers AT today's open
entry_price = row["open"]                       # also TODAY's open
exit_price = row["close"]                       # TODAY's close
entry_epoch == exit_epoch                       # same bar
```

**Real-world execution breaks this:**
1. **You cannot see today's open until 9:15 AM** (NSE call-auction print).
2. By that time, the order book has moved past the call-auction price.
3. To execute AT the open, your order must be in the call-auction queue
   BEFORE 9:08 AM — but you don't know which stocks will gap by then.

Two realistic execution paths both fail:

**Path A: Pre-open limit orders.** Place buy orders at 1-4% below prev_close
the night before. Issues:
- Fill rate <30% (most stocks don't gap to your level)
- Can't filter for quality (no info available pre-open)
- Selection bias: only stocks that gap MORE than your limit fill — those
  often continue down (bad-news gaps), inverting the edge

**Path B: Post-open market orders.** See the gap at 9:15, market-buy:
- Typical NSE slippage 20-50 bps for liquid stocks, more for mid-caps
- 30,982 trades × 30bp avg slippage ≈ destroys the 0.66% per-trade
  expectancy
- Realistic CAGR after honest slippage modeling: estimated 0-5%

## Comparison to memory note

From `memory/backtest_bias_audit.md`:
> Same-bar entry bias inflates mean-reversion returns by 15-20pp CAGR

This case shows ~30+ pp inflation (literature ~5% vs backtest 35%) — even
larger than typical because the strategy is PURELY same-bar (other audited
strategies were partly biased).

## Retirement decision

AUDIT_RETIRED for **same-bar execution bias**, not strategy failure.
Skipped optimization — even with parameter tuning the bias would inflate
all variants identically. The honest CAGR is unknowable without intraday
data and a realistic execution simulator (limit-order fill modeling +
slippage). Such infrastructure doesn't exist in this backtester.

## Recommendation

If revisiting this strategy:
1. Use minute-bar data (intraday simulation)
2. Model limit-order fill probabilities (likely 20-40% at any given level)
3. Apply realistic slippage (20-50 bps post-open market orders)
4. Rebuild signal generator with `next_open` semantics + execution lag

None of these are quick fixes. The current implementation cannot be made
honest with parameter tuning alone.

## Parameters (not optimized)

**Entry:**
- `min_gap_down_pct` — minimum gap % (default 0.01 = 1%)
- `max_gap_down_pct` — maximum gap % (default 0.04 = 4%)

**Exit:**
- `exit_at` — close (intraday) or open (overnight hold)
- `max_hold_days` — default 1

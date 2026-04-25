# index_dip_buy Optimization

**Strategy:** Buy index dips in uptrend, sell on recovery.
Entry: close < SMA(short) AND close > SMA(long); optional RSI < threshold.
Exit: close > SMA(short) (recovery) OR max_hold_days OR stop_loss_pct.
**Signal file:** `engine/signals/index_dip_buy.py`
**Data:** `nse.nse_charting_day` (NIFTYBEES)
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best CAGR -0.31% across 433 configs (R0 + 432 R1). Zero positive.

## Bias check

Verified bias-clean:
- Signal at close T (close vs SMAs computed from prior closes)
- Entry at next_open T+1
- Exit on close (forward iteration, intraday-touch SL standard)

No same-bar bias.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Best Trades | Notes |
|---|---|---|---|---|---|---|
| R0 (SMA20/200, no RSI, hold=20, no SL) | 1 | -0.65% | -0.049 | -13.22% | 188 | Vanilla dip-buy |
| R1 (SMA × RSI × hold × SL) | 432 | **-0.31%** | -0.019 | -8.99% | 2-188 | 0/432 positive |

R1 sweep:
- sma_short: [10, 20, 50]
- sma_long: [100, 150, 200]
- rsi_threshold: [0, 30, 40, 50]
- max_hold_days: [10, 30, 60, 252]
- stop_loss_pct: [0, 0.05, 0.10]

## Why it fails on NIFTYBEES

This confirms a known finding (per `MEMORY.md`):
> Selling with target profit DESTROYS dip-buy returns
> (sits in cash during bull runs)

The strategy mechanism:
1. Buy when close < SMA(20) but > SMA(200) — i.e., a shallow pullback within
   a long-term uptrend
2. Sell when close > SMA(20) — i.e., the pullback resolves up

This **explicitly cuts winners short**:
- The pullback resolves up → exit immediately at `close > SMA(20)`
- Forfeit all upside that follows the recovery
- Re-enter only on the next pullback (could be weeks)

Net: in cash during 60-80% of the bull-run upside. NIFTYBEES has been
in a near-monotonic uptrend; this strategy keeps trading out of it.

## Comparison to never-sell variant

Memory note (`MEMORY.md`):
> NIFTYBEES dip-buy never-sell: 12.8% CAGR, -59% MDD (basically buy-and-hold)

The "never-sell" variant matches buy-and-hold (~10.45%) plus a slight
boost from buying dips with idle cash. The "sell-on-recovery" version
(this strategy) destroys the entire edge by exiting too early.

The dip-buy concept on NIFTYBEES requires either:
1. **Never sell** (= leveraged buy-and-hold via dip cash deployment)
2. **Sell on much higher target** (multi-month, defeats the "buy dip" logic)

The mechanical "sell when above SMA(20)" exit is the worst possible choice.

## Retirement decision

AUDIT_RETIRED for **structurally premature exit on NIFTYBEES**.
14th NSE strategy retired this session.

## Recommendation

If revisiting:
1. Test **never-sell variant** (single buy on initial dip, hold forever) —
   would be a `dca_dip_buy` strategy, fundamentally different
2. Test **target-based exit** (10%, 20%, 50% from entry) instead of SMA recovery
3. Test on **higher-volatility ETFs** (BANKBEES, sector ETFs) where the
   recovery exit might capture meaningful swings before mean-reversion fades

## Parameters (not optimized)

**Entry:**
- `sma_short` — pullback reference (tested 10, 20, 50)
- `sma_long` — uptrend reference (tested 100, 150, 200)
- `rsi_threshold` — optional RSI < threshold (tested 0, 30, 40, 50)

**Exit:**
- `max_hold_days` — time stop (tested 10, 30, 60, 252)
- `stop_loss_pct` — fixed SL (tested 0, 0.05, 0.10)
- (recovery on close > SMA(short) is hardcoded, the dominant exit)

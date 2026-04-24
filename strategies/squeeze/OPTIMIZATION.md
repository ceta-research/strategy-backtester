# squeeze Optimization

**Strategy:** John Carter "The Squeeze" volatility expansion breakout.
Entry: Bollinger Bands inside Keltner Channels (squeeze on) just released
+ positive momentum. Exit: momentum turn down OR stop-loss OR max_hold.
Source: Carter "Mastering the Trade" Ch. 10.
**Signal file:** `engine/signals/squeeze.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Catastrophic failure. R0 -21.51% CAGR / -98.1% MDD. R1 0/108 positive
configs, best -18.91% CAGR with -96.7% MDD.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Notes |
|---|---|---|---|---|---|
| R0 (defaults) | 1 | **-21.51%** | -0.219 | -98.1% | 17K orders |
| R1 (kcm × mom × SL × hold) | 108 | -18.91% | -0.196 | -96.7% | 0/108 positive |

### R1 finding

- All 108 configs have CAGR -18.91% to -25%
- All have MDDs -96% to -98% (effectively wipe out account)
- 11K orders per config — high turnover
- Wider stops (50%) marginally better than 5% but still catastrophic
- mom_period 6 (faster) marginally less bad than 12/24

## Why it fails

The squeeze entry signal (BB inside KC then released, with positive momentum)
on NSE individual stocks reliably catches **failed breakouts**:
1. Tight consolidation phase ends with a sharp false-breakout pop
2. Strategy enters at next-open after the pop signal
3. Most of the move has already happened (or it was a bull trap)
4. Subsequent reversal triggers stops; many small losses compound

The 17K orders in R0 confirm signals fire frequently. Each is a thin-edge
bet; the negative expectancy compounds across thousands of trades.

In US futures (Carter's domain) the squeeze works because:
- Index futures have natural mean-reversion-then-breakout behavior
- Lower transaction costs
- Larger move-on-breakout (true volatility expansion)

NSE individual stocks experience squeeze releases that are dominated by
overnight gap risk, news-driven false breakouts, and operator-driven pumps
that fail.

## Retirement decision

AUDIT_RETIRED after 109 configs. No parameter combination produces positive
CAGR. Tied with extended_ibs as worst result of any optimization run in this
suite. Skipped modern-window check given universal failure.

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Same architectural pattern of failure (signal frequency + cost compounding +
false-breakout dynamics) as previously-retired mean-reversion strategies,
even though the strategy intent is breakout-following.

## Parameters

**Entry:**
- `bb_period` / `bb_std` — Bollinger Band params
- `kc_period` / `kc_mult` — Keltner Channel params (tested 1.5, 2.0, 2.5)
- `mom_period` — momentum lookback (tested 6, 12, 24)

**Exit:**
- `stop_loss_pct` — fixed SL (tested 10%, 15%, 25%, 50%)
- `max_hold_days` — time stop (tested 40, 80, 200)

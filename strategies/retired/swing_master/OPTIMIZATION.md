# swing_master Optimization

**Strategy:** Larry Swing trend-pullback.
Entry: close > SMA_short > SMA_long (uptrend) AND N-day pullback (declining
highs) AND short-term Force Index < 0 (selling on pullback) AND long-term
Force Index > 0 (overall buying pressure).
Exit: target_pct OR stop_loss OR max_hold (trailing stop based on prior low).
**Signal file:** `engine/signals/swing_master.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best CAGR 2.62% across 649 configs — far below NIFTYBEES 10.45%.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Notes |
|---|---|---|---|---|---|
| R0 (defaults SMA10/20, pull=3, T=7%, SL=4%) | 1 | -4.10% | -0.081 | -50.6% | 6574 orders |
| R1 (SMA × pullback × T × SL × hold) | 648 | **2.62%** | 0.061 | -43.1% | 68/648 positive |

### R1 finding

- 648 valid configs (729 total, some failed)
- Only 68 positive (10%)
- 0 above 5% CAGR
- Best at 2.62% with -43% MDD
- All top configs have 7K-9K trades

## Why it fails

Despite multiple confirmations (SMA uptrend + pullback + FI divergence), the
strategy generates 7000-9000 trades per config. The Force Index divergence
+ 3-day pullback combination fires very frequently — almost every minor
pullback in any uptrending stock qualifies.

This high turnover compounds:
- NSE STT 0.1% per round-trip
- Slippage on tight pullback entries
- 4% default stop is too tight for normal pullback noise → many small losses

The 7%/4% target/SL ratio (1.75:1) requires high WR to be profitable. With
7K trades, even tiny WR shortfalls below the breakeven of ~36% WR (with
costs) make the strategy unprofitable.

The strategy concept is sound (pullback in uptrend + divergence) but
implementation generates too many marginal signals on NSE individual stocks.
A stronger quality filter (RSI rank, percentile, 200-day SMA trend) would
reduce noise but defeats the original "Swing Master" simplicity.

## Retirement decision

AUDIT_RETIRED after 649 configs. Best 2.62% / Cal 0.061 cannot match
NIFTYBEES 10.45% / Cal 0.27. Eighth NSE strategy retired this session.
Skipped modern window — pattern indicates structural turnover problem,
not regime-dependent.

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Same architectural failure as `darvas_box` and `squeeze`: tight target/SL
ratio + frequent signal firing = cost-dominated negative expectancy.

## Parameters

**Entry:**
- `sma_short` / `sma_long` — uptrend MAs (tested 10-50 short, 50-200 long)
- `pullback_days` — declining-high count (tested 2, 3, 5)

**Exit:**
- `target_pct` — profit target (tested 7%, 15%, 30%)
- `stop_loss_pct` — fixed SL (tested 4%, 10%, 15%)
- `max_hold_days` — time stop (tested 20, 60, 120)
- `trailing_buffer_pct` — buffer for trailing-stop raise (0.002)

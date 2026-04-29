# overnight_hold Optimization

**Strategy:** Buy at close, sell at next-day open. Captures the overnight risk premium.
Literature claim: Since 1993, all S&P 500 gains came from overnight holds (+1,100% cumulative).
**Signal file:** `engine/signals/overnight_hold.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best CAGR -13.44% across 73 configs (R0 + 72 R1). Zero positive.

## Bias check (per OPTIMIZATION_PROMPT plausibility protocol)

Verified NO same-bar bias (lesson from gap_fill):
- Entry at close of day T (MOC; observable filters only — `close<open`, `rsi_14` from past)
- Exit at open of day T+1 (next_open, next_epoch)
- entry_epoch ≠ exit_epoch (confirmed structurally distinct bars)

Strategy is honest. Failure is real, not artifactual.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Trades | Notes |
|---|---|---|---|---|---|---|
| R0 (no filters, 15 pos) | 1 | -15.22% | -0.163 | -93.23% | 1,489,366 | Vanilla overnight on all NSE liquid |
| R1 (down-day × RSI × sort × pos) | 72 | -13.44% | -0.149 | -90.45% | 13,699 (best) | 0/72 positive |

## Why it fails on NSE individual stocks

The literature thesis (Bessembinder/Lou-style "overnight premium") is documented for **broad US indices/ETFs** (SPY, QQQ). It does NOT generalize to NSE individual stocks because:

1. **NSE STT is brutal at this turnover.** R0 generates 1.49M overnight trades (every liquid NSE stock × every day). At 0.1% per round-trip, costs alone subtract ~25% per year from the equity curve.

2. **NSE individual stocks gap UP into close, then DOWN into next-open** more often than US large-caps. The opposite of what you need. Insider/news flow, FII overnight selling pressure, and lack of after-hours price discovery (no NSE pre-market book) mean the open often gives back close-day strength.

3. **Even with filters (R1)**, trade count drops to ~14K but the average per-trade overnight return is still negative after costs. The best filter combination (top_gainer + no-down-day + no-RSI) lost 13.44% CAGR.

4. **No filter inverts the edge.** Top_loser sorting (buy yesterday's losers overnight) would help if there were a mean-reversion premium overnight on NSE — there isn't.

## Comparison to gap_fill

Both strategies bet on intraday/overnight micro-structure. Both fail on NSE for related reasons:
- gap_fill: 35.27% CAGR was **fictional** (same-bar bias, AUDIT_RETIRED)
- overnight_hold: -15.22% CAGR is **real** (no bias, AUDIT_RETIRED)

The honest verdict: **NSE individual-stock micro-structure does not pay an overnight risk premium**. This is a US-large-cap phenomenon.

## Recommendation

If revisiting:
1. Test on **NIFTYBEES / BANKBEES / index ETFs** (not individual stocks) — index-level overnight premium MAY exist for India
2. Test on US universe (`exchange: US`) — that's where the literature is from
3. Add overnight gap quantile filters (only buy when prev-day's intraday move was modest)
4. Compare to a coupled "intraday-only" strategy (sell at close, buy at next open) — if intraday returns are positive on NSE, that's the inverse opportunity

## Retirement decision

AUDIT_RETIRED for **structural negative-expectancy on NSE individual stocks**.
Tenth NSE strategy retired this session. Pattern-consistent with other high-turnover failures (squeeze, darvas, swing_master): cost-dominated negative expectancy.

## Parameters (not optimized)

**Entry:**
- `buy_on_down_day` — only enter when close<open (default false)
- `min_rsi_14` — only enter when RSI ≥ threshold (default 0 = off)

**Exit:**
- `exit_at` — next_open (default)
- `max_hold_days` — 1 (default)

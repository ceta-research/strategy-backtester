# holp_lohp Optimization

**Strategy:** John Carter LOHP reversal (long only).
Entry: stock makes new N-day low, then a subsequent bar closes ABOVE the
high of that low bar (failed-breakdown reversal confirmation).
Exit: initial stop at low-bar's low; 2-bar trailing stop after day 3;
hard time stop at max_hold_days.
**Signal file:** `engine/signals/holp_lohp.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-25 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Best CAGR -7.20% across 82 configs. Zero positive.

## Bias check

Verified bias-clean:
- Signal observable at close of day T (`close > last_low_bar_high`)
- Entry at `next_open` (next_epochs[i], next_opens[i]) — day T+1
- Distinct entry/exit epochs in walk-forward exit logic
- Exit price uses close of exit-trigger day (slightly optimistic vs intraday SL touch, but standard for daily-bar backtests)

No same-bar bias. Failure is real.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Best Trades | Notes |
|---|---|---|---|---|---|---|
| R0 (lookback=20, ts=3, hold=20, pos=15) | 1 | -10.30% | -0.120 | -86.14% | 45,833 | Vanilla Carter LOHP |
| R1 (lookback × ts × hold × pos) | 81 | **-7.20%** | -0.094 | -76.96% | 5,547 | 0/81 positive |

R1 sweep:
- lookback_period: [10, 20, 50]
- trailing_start_day: [2, 3, 5]
- max_hold_days: [10, 30, 60]
- max_positions: [10, 15, 30]

## Why it fails on NSE

The Carter LOHP pattern relies on:
1. A clean failed-breakdown setup (low bar followed by reversal close)
2. Discretionary trader judgment to filter false signals
3. Tight intraday stop management (low-bar's low)

On NSE individual stocks (mechanical screening, daily bars):
- **Pattern fires too often** — every minor pullback in a noisy uptrend qualifies. R0 generates 45K orders. Even R1 best config fires 5,500 trades over 16 years.
- **Tight stops + STT 0.1% = death by paper cuts.** Most LOHP signals get stopped out within days at ~1-3% loss; cumulative cost dominates the few winners.
- **Daily granularity loses the pattern's edge.** Carter's LOHP is an intraday/multi-bar pattern — looking for the EXACT bar where price reverses. On daily charts, "low bar high" is often pierced by noise the next session, generating false reversal signals.
- **Reversal timing in NSE individual stocks is mean-revert-then-fail.** Failed breakdowns often retest and break through 2-3 days later. The 2-bar trailing stop after day 3 is too loose to protect gains, too tight to ride the rare winners.

The strategy concept is sound for discretionary intraday traders (Carter's audience) but does not translate to mechanical daily-bar NSE backtesting.

## Comparison to similar retirements

Same pathology as `darvas_box`, `squeeze`, `swing_master`:
- High signal-fire rate on NSE individual stocks
- Tight stop / target ratios incompatible with NSE STT 0.1%
- Pattern works on US large-caps + intraday, not daily NSE

## Retirement decision

AUDIT_RETIRED for **structural negative-expectancy on daily NSE**.
12th NSE strategy retired this session.

## Recommendation

If revisiting:
1. Run on **minute-bar NSE data** (when infrastructure exists) — Carter's pattern is intraday-native
2. Add quality/percentile pre-filter to cut signal count by 5-10×
3. Replace 2-bar trailing with ATR-based trailing (less noise sensitivity)

## Parameters (not optimized)

**Entry:**
- `lookback_period` — N-day low window (tested 10, 20, 50)

**Exit:**
- `trailing_start_day` — day to switch from initial to 2-bar trailing (tested 2, 3, 5)
- `max_hold_days` — hard time stop (tested 10, 30, 60)

# darvas_box Optimization

**Strategy:** Classical Darvas box breakout.
Entry: close > N-day box high AND volume > 20d_avg × volume_breakout_mult.
Exit: trailing stop = max(box_low, highest_close × (1 − tsl)) OR max_hold.
Source: Nicolas Darvas "How I Made $2,000,000 in the Stock Market".
**Signal file:** `engine/signals/darvas_box.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: AUDIT_RETIRED

Catastrophic failure. R0 -26.42% / -99.3% MDD. R1 0/144 positive,
best -11.22% / -85.8% MDD.

## Rounds run

| Round | Configs | Best CAGR | Best Cal | Best MDD | Notes |
|---|---|---|---|---|---|
| R0 (10d box, vol=1.5×) | 1 | **-26.42%** | -0.266 | -99.3% | 60K orders |
| R1 (box × vol × tsl × hold) | 144 | -11.22% | -0.131 | -85.8% | 0/144 positive |

### R1 finding

- All 144 configs have CAGR -11.22% to -25%
- All MDDs -85% to -90%+
- Long boxes (200d) + strict volume (3×) marginally less bad
- 4000+ trades per config — high turnover + losses
- TSL setting basically irrelevant (effective_stop dominated by box_low)

## Why it fails (vs eod_breakout which works)

Darvas's "close > N-day high + 1.5× volume" is **too simple** for NSE stocks:
1. Generates many false-breakout signals on noise spikes and operator pumps
2. Trailing stop = max(box_low, highest × (1 − tsl)) — `box_low` from a long
   N-day range is FAR below entry, allowing huge losses on failed breakouts
3. No quality filter (no SMA trend, no direction-score, no percentile rank)

By contrast, `eod_breakout` (15.20% CAGR / Cal 0.446) and `enhanced_breakout`
(16.40% CAGR / Cal 0.656) both add:
- Direction-score quality gate (filters noise breakouts)
- Quality_in_n_year filter (compounders only)
- Percentile rank (only top X% by relative strength)
- Tighter, dynamic exits

Darvas's pure rule has none of these protections. On NSE, this means:
- 4000 trades, mostly false breakouts
- box_low trailing stop allows huge intra-trade drawdowns
- Compound effect: -11% CAGR with -85% MDD even with longest 200-day boxes

## Retirement decision

AUDIT_RETIRED after 145 configs. Skipped modern window since the structural
issue (no quality filter) doesn't change with regime. eod_breakout family
already proves NSE breakouts work WITH quality filters; this strategy
demonstrates they fail WITHOUT them.

## Prior note

Entry uses `next_open` (no same-bar bias). Post-audit NSE charges applied.
Same fate as `squeeze` and `extended_ibs`: simple-rule entries with
inadequate risk control fail catastrophically on NSE post-audit.

## Parameters

**Entry:**
- `box_min_days` — N-day box (tested 10-200)
- `volume_breakout_mult` — volume multiplier (tested 1.5-3.0)

**Exit:**
- `trailing_stop_pct` — fixed TSL (tested 8-25%)
- `max_hold_days` — time stop (tested 30-252)

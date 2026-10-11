# momentum_cascade audit — plan (2026-10-11)

**Goal:** turn "18–21% over 10–20 years" (studies/long_window) into a number Swas can plan around,
by testing whether the engine or the data flatters it. Base for every run: the repaired snapshot
(`long_window/providers.py`, `ca=True, clean=True`), config `strategies/momentum_cascade/config_champion.yaml`,
window 2010-01-01 → 2026-10-09 (baseline **19.01% / −25.88% / 554 trades**) unless a lens says otherwise.

**Out of scope:** survivorship (Swas, 10-11: already measured small on NSE breakouts, ~1pp on
eod_breakout via bhavcopy; carried as a caveat). Engine-wide fixes: anything found is measured here
with study-local variants; engine changes come after, as their own commits.

## Code read so far (what each lens is aimed at)

| path | behaviour | concern |
|---|---|---|
| `signals/momentum_cascade.py` entry | signal on close[i], fill at **open[i+1]** | fine if the open is real |
| `signals/momentum_cascade.py` exit walk | TSL / max-hold checked on close[j], **filled at close[j]** | can't observe a close and trade at it (NSE close = last-30-min VWAP) |
| exit walk | TSL arms only after close ≥ signal-day close; no stop before that | design, but uncapped downside until armed or 378 days |
| `base.build_regime_filter` | NIFTYBEES close > SMA200 on the signal day | causal |
| `ranking.top_gainer` | gain = prev_close vs prev_close 360 bars back, at the entry bar | causal |
| `utils.create_epoch_wise_instrument_stats` | liquidity cap = 30-bar mean turnover **including the entry day** | small look-ahead in sizing |
| `data_provider.remove_price_oscillations` | drops spike-and-revert bars using ±2 future bars | non-causal cleaning |
| `simulator` | entries before exits; 5 bps slippage hits cash (trade log shows 0, cosmetic) | fine |
| `charges` | STT 0.1%×2, stamp 0.015%, exch+GST+SEBI, ₹20/leg brokerage | ~0.23% round trip + 0.10% slippage; no tax |

## Lenses (in order of expected impact)

**L1 Exit fill timing.** Study-local copy of the generator with `exit_fill: close | next_open`; default
must reproduce the baseline exactly (byte-identity gate). Run next_open on 2010→now and 20y.

**L2 Fill-price reality.** Entry opens and exit closes come from charting. Check every trade's entry
open and exit close against `nse_bhavcopy_historical` (independent, unadjusted; through 2026-03),
scale-matched on the same day's close. Establish the epoch offset empirically first. Flag
mismatches > 2%; list the trades and their P&L share.

**L3 Look-ahead sweep.** (a) Liquidity cap from the previous bar's 30-day mean, not the entry bar's.
(b) Count oscillation-filter removals inside the scanner universe and inside held trades; if
material, run with the filter restricted to causal (past-only) detection.
(c) Code-read check that every signal input is ≤ signal-day close (done above; re-confirm on the
variant).

**L4 Metric + determinism check.** Rebuild the daily equity curve independently from the trade log
and charting closes; compare CAGR / MDD to the engine's. Re-run the baseline twice and once with the
input frame's row order shuffled; trades must match exactly.

**L5 Costs and capacity.** Slippage 5 / 15 / 30 bps. Starting capital ₹50L, ₹1 cr
(baseline), ₹5 cr, ₹25 cr; record how often the 4.5%-of-turnover cap binds.

**L6 Parameter robustness.** One-at-a-time neighbours for each of 10 parameters (fast/slow lookback,
accel, min momentum, breakout window, regime SMA, TSL, max hold, ranking window, max positions).
A champion sitting on a narrow peak gets discounted.

**L7 Concentration and timing.** Start-year sweep (2006…2018 → now). Year-by-year vs NIFTYBEES,
rolling 3y CAGR, time in market, top-N trade removal, ex-2021.

**L8 Tax (translation to live).** Approximate post-tax CAGR from the trade log under Indian
STCG/LTCG with same-year loss set-off. Estimate only; labelled as such.

**L9 Live-realistic combined run (the planning number).** The lens deltas don't add. One config with
every realistic choice at once: exit at next open, lagged liquidity cap, ₹50L, 15 bps slippage, on
2010→now and 20y; L8's tax estimate applied to its trade log.

## Execution order and details (after review)

1. Variant generator (`mc_variant.py`, own `strategy_type`), gate on **trade-list equality**. L4 alongside.
2. Fan out L5/L6/L7 in the background; do L1 → L2 → L3 meanwhile; L9 and L8 last.
- L1: fill at `next_opens[j]` / `next_epochs[j]` on the trigger bar; `end_of_data` stays at last close.
  Record per trade whether the TSL ever armed (share of trades / P&L that ran with no stop).
- L2: bhavcopy filtered to `SERIES='EQ'` (the TATASTEEL partly-paid series lives there under its own
  code). Verify the charting↔bhavcopy epoch offset on one liquid name; don't assume it. Also: share of
  charting bars with `open == prev close` exactly, and the open-gap distribution. Stub opens would
  mean entries fill at the signal close, a look-ahead L1 can't see.
- L4: the generator re-sorts its input, so shuffle `all_order_rows` instead (variant knob). The cohort
  that can move: names with 262–361 bars (eligible, null-ranked, no tie-break). Count their trades.
- L7: also report longest time underwater (`max_dd_duration_periods`, 1,079 days on the board run).
- L8: today's rates (STCG 20%, LTCG 12.5% over ₹1.25L) applied to all years; check the hold-time split
  first (`max_hold_days: 378` puts many exits near the 1-year line). NIFTYBEES buy-and-hold carries
  ~no tax until sold, so compare on that basis.

## Gates

- L1 variant with defaults reproduces 19.01% / 554 trades exactly before any variant result counts.
- Every number in AUDIT.md is printed by a script in this directory (no hand arithmetic).
- Each lens reports: what was tested, the delta, verdict (holds / discount / broken).

## Output

`AUDIT.md`: per-lens results, then one combined "planning number" with the adjustments that
compose (L1 + L3 + L5 at ₹50L + L8), and the ones that are ranges (L6, L7).

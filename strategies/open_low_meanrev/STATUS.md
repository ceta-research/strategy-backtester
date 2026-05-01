# Open-Low Mean Reversion — STATUS & Tracking

**Created:** 2026-05-01 (placeholder — to be filled in by next session)
**Status:** SCOPED, NOT YET IMPLEMENTED
**Predecessor context:** `docs/sessions/completed/2026-05-01/SESSION_HANDOVER_INTRADAY_AUDIT_AND_PIVOT.md`
**Why this strategy:** intraday breakout family conclusively dead across 3 formulations. Mean reversion was the *failure mode* there — make it the *mechanism* here.

---

## Hypothesis

Stocks that move down N basis points from the day's open within the trading session tend to revert toward the open. Buy the dip, sell at the recovery.

This is structurally distinct from breakout strategies (which assume continuation) and inverse to what failed:
- Phase A/B failure mode: breakout entries got mean-reverted (stops 2x targets)
- This strategy's success mode: BE the mean-reverter

---

## Concept

**Universe:** Nifty 50 (clean, deep liquidity, narrowest spreads). Nifty 100 as expansion.

**Per stock per day:**
1. Observe opening price (first bar's open at 09:15)
2. Place buy limit at `open_price × (1 - dip_threshold)`
3. If filled: target = opening price (~`dip_threshold`% gain)
4. Exit by EOD-30min if target not hit (15:00)

**`dip_threshold` candidates to test:**
- Constant 0.5%, 0.7%, 1.0%
- Per-stock rolling 30-day median of "max % below open during day"
- Per-stock rolling 30-day p25 (more conservative)

**Stop loss design (test both):**
- (a) No stop, just EOD-30min exit
- (b) Hard stop at `open × (1 - 1.5×dip_threshold)` (i.e., 50% wider than entry-from-open)

**Don't enter if:**
- Stock is in multi-day downtrend (5d return < −3%) — avoid catching falling knives
- Optional: stock has gapped DOWN at open (already at the lows, less reversion potential)

---

## Why this might work (priors)

- Large-caps round-trip on most days — opening drives often fade toward VWAP
- Nifty 50 result from Phase B showed **41% of breakout trades exited at EOD close** — that tape is precisely the round-trip pattern this strategy harvests
- Limit-order entry → near-zero slippage (only fills at your price, no chasing)
- Bounded loss: if no fill, no loss (just opportunity cost)
- Bounded gain: only target the open price, not "to the moon"

## Why it might fail (risks)

- Trend days: dip keeps dipping, no recovery, stop or EOD exit at large loss
- News-driven moves: same as trend days
- Volatility surge: large dip on day N, opening fade plays out next day instead of same day
- "Average dip" is computed from history — if today's dip exceeds it, you fill but the dip continues

---

## R0 Plan (next session)

1. Build `prod_runner` script — `open_low_meanrev_prod.py` (self-contained, parquet reads, Nifty 50 universe)
2. R0a: constant `dip_threshold = 0.7%`, no stop, EOD-30min exit
3. R0b: same but with `1.5%` hard stop
4. Per-year breakdown (2022-2025), exit-type distribution, drawdown shape
5. **Decision gate:** CAGR > 3% at 0bps slippage AND MDD < 5% → proceed to parameter sweeps. Else: pivot to next direction.

Estimated runtime: ~5-10 min on prod (similar to Phase A/B infra).

## Reuse-from-previous

- `intraday_breakout_prod.load_minute_data()` — minute parquet loader
- `intraday_breakout_prod.nse_intraday_charges()` — Zerodha intraday cost model
- `intraday_breakout_prod.SECONDS_IN_ONE_DAY`, `OR_OFFSET` (= 555 for 09:15) constants
- `run_phase_b_nifty.NIFTY_50` constituent list
- Bug fix patterns (`max(level, bar_open)`, `range(entry_idx + 1, ...)`) — even though limit-order entry mostly avoids Bug 1, copy as safety

## Bug-fix carryover (MANDATORY)

Even though limit orders are immune to the gap-up bug (you only fill at your specified price, not "wherever it broke through"), copy the safety patterns from `intraday_breakout_prod.py`:
- Entry: limit order fills at exactly `open × (1 - dip_threshold)` (no slippage on entry by definition)
- Exit: target/stop check loop must start at `range(entry_idx + 1, ...)` to avoid same-bar bias

---

## Future work (if R0 succeeds)

- Sweep dip threshold (constant vs per-stock rolling)
- Add volume confirmation (large dip on heavy volume = panic = better bounce)
- Test with Nifty 100 (more candidates, slightly less liquid)
- Combine with pair trading (if open-low works, what about pair-spread fades?)
- Live paper trading on Kite API

---

*See `docs/sessions/completed/2026-05-01/SESSION_HANDOVER_INTRADAY_AUDIT_AND_PIVOT.md` for full prior-session context.*

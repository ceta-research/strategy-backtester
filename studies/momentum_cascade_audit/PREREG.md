# Pre-registration: stop-from-entry and exit mechanics (2026-10-11)

Written and committed **before** any of these runs. The decision rules below are fixed; the runs
decide, not the reader of the results.

Base for every arm: the audit's live-realistic setup (`L9b_live_ebe`): exits settle before entries,
exit fills at the next open, liquidity cap from the prior bar, ₹50L, 15 bps, repaired snapshot
(`studies/long_window/providers.py`). Baseline after the `simulator.py:393` sizing fix is re-run as
`S0X0` and is the reference for every comparison. Windows: 2010-01-01 → 2026-10-09 ("2010→") and
2006-01-01 → 2026-10-09 ("20y"); 2006–2010 alone is a tiebreak, never a gate.

## Why
The audit found 23 of 581 trades never armed their trailing stop (it arms only once the position
regains its signal-day close): mean −35.9%, worst −70.7%, −₹379L = −24% of net P&L. And the backtest
exits on a close-based trigger with a next-open sell, while the live sell-side tool (`_gtt_trail.py`)
is an intraday GTT ratcheted from LTP from day one. Those are two different strategies.

## Arms

Stop (S), all with the 15% trailing width unchanged:
- **S0** as-is: no stop until the TSL arms.
- **S1** trail from entry: the 15% trailing stop is live from the entry bar (high-water starts at the entry price).
- **S2** fixed floor: exit if close ≤ entry × 0.85 until the TSL arms, then as-is.

Exit mechanics (X):
- **X0** backtest: trigger on close, sell at the next open.
- **X1** GTT-style: level = (high-water close) × 0.85, ratcheted after each close and applying to the
  next session; fires if that session's low ≤ level; fills at min(open, level). Max-hold exits stay X0.

Mapping to live tools: **the existing `_gtt_trail.py` as-is is S1×X1.** S2 needs a floor-then-trail
change to the ratchet. X0 needs next-open sells (AMO/limit at the open).

Runs: S0X0, S1X0, S2X0, S0X1, S1X1 on both windows (10 runs). No other widths or variants.

## Decision rules

1. **Stop.** Adopt S2 if, on BOTH windows vs S0X0: max drawdown improves, the worst single trade
   improves, and CAGR falls by ≤ 1.0pp. If S2 fails and S1 passes the same test, adopt S1. Otherwise
   keep S0. A larger CAGR gain never overrides a drawdown or worst-trade failure.
2. **Mechanics.** X1 is acceptable for live exits only if it costs ≤ 1.0pp CAGR vs the same S arm under
   X0 on BOTH windows. Otherwise live exits are next-open sells (X0) and the ratchet is not used for
   this sleeve.
3. The chosen S×X becomes `config_live.yaml`. Nothing else in the config changes.

## What is NOT tested here
Entry mechanics (AMO limit at the open vs GTT-buy): measured from the paper ledger instead, which
records prior close and actual open per entry. Universe changes (e.g. excluding the five silver ETFs
the scanner lets through): a follow-up re-baseline, flag-only in the observe runner meanwhile.

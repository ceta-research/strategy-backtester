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

---

## Verdict (runs completed 2026-10-11, `prereg_decide.py`, raw table in `out/prereg_verdict.md`)

| arm | window | CAGR | max DD | Calmar | trades | worst trade |
|---|---|---|---|---|---|---|
| **S0X0** (reference) | 2010→ | **18.58%** | −32.2% | 0.58 | 565 | −67.8% |
| S0X0 | 20y | 18.10% | −32.2% | 0.56 | 684 | −67.8% |
| S1X0 trail from entry | 2010→ | 16.45% | −30.8% | 0.53 | 656 | −66.7% |
| S1X0 | 20y | 16.26% | −29.6% | 0.55 | 773 | −66.7% |
| S2X0 fixed −15% floor | 2010→ | 16.40% | −30.8% | 0.53 | 652 | −66.7% |
| S2X0 | 20y | 16.22% | −29.6% | 0.55 | 763 | −66.7% |
| S0X1 intraday GTT | 2010→ | **10.15%** | −42.6% | 0.24 | 663 | −91.6% |
| S0X1 | 20y | 10.25% | −42.6% | 0.24 | 824 | −91.6% |
| S1X1 (= `_gtt_trail.py` as-is) | 2010→ | **7.89%** | −48.2% | 0.16 | 846 | −58.2% |
| S1X1 | 20y | 8.79% | −51.2% | 0.17 | 1032 | −66.5% |

**Rule 1 → S0.** Both stop-from-entry arms fail the CAGR test: −2.1pp (S1) and −2.2pp (S2) on 2010→,
−1.8pp / −1.9pp over 20y, against the ≤ 1.0pp allowance. They do improve max DD (−32.2 → −30.8 /
−29.6) and the worst trade barely (−67.8 → −66.7). So the no-stop-until-armed design stays. The 23
unarmed trades are a known cost of the strategy, not a defect to patch.

**Rule 2 → X0.** The intraday GTT arm costs **8.4pp** (2010→) and **7.9pp** (20y): the stop fires
on intraday lows that the close recovers: 17% more exits (565 → 663 trades; 50% more with a trail
from entry, 846), and the typical trade is what pays for it: median +0.1% → −2.1%, mean +10.7% →
+5.8%. The −91.6% worst trade (INDOTHAI) is an unarmed position that collapsed to end-of-data
with no one-day move over 30%, i.e. a real collapse, not a GTT fill or a corrupt bar. With a
trail from entry (S1X1, what the existing ratchet does) it is 7.9%, and the real ratchet trails
at 8–12%, tighter than the 15% tested, so the as-is ratchet would do worse still. **The `_gtt_trail.py` ratchet must not be used for this sleeve.** Live exits
are next-open sells after a close-based trigger.

**Reference on the fixed simulator** (the audit's L9b figure of 17.71% was computed before the
`simulator.py:393` sizing fix): 18.58% / −32.2% 2010→, 18.10% / −32.2% over 20y. After tax at
today's rates: see AUDIT.md addendum. `config_live.yaml` = S0X0.

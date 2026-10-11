# momentum_cascade paper phase

**Started:** 2026-10-11 (first observe run on data through 2026-10-09). **Status:** OBSERVE ONLY.
Nothing is placed from this flow. The phase ends with an explicit decision by Swas, not by the
tooling.

## What is being measured
The runner (`scripts/momentum_cascade_daily.py`, `/momentum-cascade`) writes, every trading day:
- `results/momentum_cascade/paper_ledger.jsonl`: append-only entries and exits at the engine's own
  prices (next-open fills), with the prior close and the entry-day close per entry so entry
  mechanics (AMO limit at the open vs GTT-buy) can be measured later. First run seeds the book
  as found (`seeded: true`); everything after is observed.
- `observe_log.jsonl`: one row per run with data health, regime, book, tomorrow's orders.
- `daily_latest.json`: the latest run, incl. a hash of closed history older than 5 days
  (rewrite detector).

Config: `config_live.yaml` (champion parameters; repaired data; exits settle before entries;
next-open exit fills; **S0 × X0** per `studies/momentum_cascade_audit/PREREG.md`: no stop until the
TSL arms, close-based trigger, sell at the next open. The intraday GTT variant halved CAGR, so the
`_gtt_trail.py` ratchet is not used for this sleeve.)

## Graduation gates (all must hold)
1. **≥ 40 trading days** of runs with no gap longer than 3 trading days.
2. **Zero history rewrites** flagged (`history_rewritten: false` on every run). A rewrite means
   charting backfill changed booked trades; the ledger then can't be compared with the backtest.
3. **Data health clean** on every counted run: lag ≤ 5 days, 0 duplicate bars, every
   split-like bar on a held name confirmed as a real corporate action.
4. **Fidelity:** for every observed exit, the ledger's exit date and price equal what a fresh
   backtest over the same window produces (re-run at the end of the phase and diff).
5. **Hard-disqualifier vet rate** known: how many proposed entries were DISQUALIFIED and why. If
   vetoes are frequent, the universe needs an upstream exclusion and a re-baseline before live.
6. **Feasibility of the daily cycle:** on what share of days was a clean bar for day T available
   before the T+1 open (the charting ingest runs ~00:00–03:30 UTC)? Below ~90% the live path
   needs a different intraday data source for the last bar.

## Decision at the end
Swas decides whether ~13% after tax (≈ +3–5pp over NIFTYBEES after tax), −32% drawdown and
multi-year flat stretches are worth a live sleeve, and its size.
Live execution would be: buys at the next open (AMO/limit), exits per the
PREREG verdict (next-open sells, or the ratchet if X1 was accepted). Entry mechanics
chosen from the ledger's prior-close / open data, not guessed.

## What this phase does NOT settle
Survivorship (measured small before on NSE breakouts; out of scope by Swas's call), and the
universe's non-equity names (five silver ETFs traded in 2025–26, 1.9% of P&L): flag-only here,
an upstream exclusion + re-baseline is a follow-up.

## Log
| date | data through | regime | book | sells | buys | health | notes |
|---|---|---|---|---|---|---|---|
| 2026-10-12 (IST) | 2026-10-09 | BEAR | 13/15 | 1 (UJJIVANSFB tsl) | 0 (3 signals, regime off) | clean; 2 recent splits (BLSE, PGIL raw) | seed run; ledger seeded with 13 open entries. GMBREW −29% and CARTRADE −10% are unarmed (no stop) by design. |

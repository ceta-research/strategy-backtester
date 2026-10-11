# momentum_cascade on bhavcopy: the survivorship check (2026-10-12)

**Question (Swas):** does running on `nse_bhavcopy_historical` give us anything new?
**Answer:** one number: universe membership (the names that later delisted) costs **−0.2pp from
2010 and −1.5pp over 20 years**. The rest of the bhavcopy gap is bhavcopy's own data. Keep the
repaired charting feed for the backtest and the daily runner; use bhavcopy as the periodic cross-check.

Scripts: `bhav_provider.py` (calendar split repair on bhavcopy, +IST epoch alignment verified on
RELIANCE 2026: 50/57 closes equal, 0 under a UTC-day floor), `survivorship.py`; raw output
`out/survivorship.md`. Live config (S0 × X0, exits first, next-open fills, ₹50L, 15 bps) on
identical windows, all ending at bhavcopy's last day, 2026-03-25.

| data | 2010-01 → 2026-03 | 2006-01 → 2026-03 |
|---|---|---|
| charting, repaired (CH) | **18.36% / −32.2%** (552 trades) | **17.90% / −32.2%** (671) |
| bhavcopy data, **charting's symbols only** (BHS) | 16.10% / −39.3% (494) | 15.91% / −39.6% (598) |
| bhavcopy, full universe incl. delisted (BH) | 15.88% / −38.4% (535) | 14.44% / −38.4% (660) |
| **survivorship = BH − BHS** | **−0.22pp** | **−1.47pp** |
| bhavcopy's data = BHS − CH | −2.26pp, −7pp DD | −1.99pp, −7pp DD |

## What the delisted names did
Trades in names charting does not have: 77 (2010→) / 131 (20y), **+6.8% / +4.0% mean, a fifth of
the P&L**. Most are renames of survivors (CADILAHC, MINDTREE, MOTHERSUMI, HEROHONDA, KPIT, PVR,
INFRATEL, ADANITRANS …), some real deaths (GITANJALI, AMTEKINDIA, BHUSANSTL, EDUCOMP, GLODYNE,
ABGSHIP). The strategy buys new highs and ranks on 360-day gain, so a name that later dies is
bought during its run-up, not its collapse; the loss shows up as a displaced slot more than as a
dead position. The 20-year number is larger because 2006–2010 is where the deaths cluster
(bhav 2008: −22.3% vs charting −14.8%).

## Why bhavcopy's data costs ~2pp (not survivorship)
1. **Renames restart history.** bhavcopy keys by ticker and never re-keys (ZOMATO ends 2025-04-07,
   ETERNAL starts 2025-04-08). A held position exits at the rename as end-of-data, and the new
   ticker can't be ranked for ~360 bars.
2. **Splits the FMP calendar lacks.** On repaired bhavcopy, 7 trades (2010→) / 11 (20y) were held
   across a ≥30% one-bar drop, net **−₹110L / −₹162L = −21% of P&L**: JKTYRE 2014-12 (1:5),
   CUPID 2026-01 (bonus), ATLASCYCLE, SUJANATOW, TATATEA 2010 (rename), IVRCLINFRA 2006,
   INDIABULLS 2007 (demerger). Charting had adjusted these; the calendar-driven repair can't.
   ⚠️ The same holes exist in the live feed: a split the calendar lacks shows up as a fake stop
   trigger. The daily runner's split-like-bar check (`looks_like_split`) is the guard; it flags
   and suppresses, it doesn't repair.
3. Both effects are identical in BH and BHS, so they cancel in the survivorship difference.

## Verdict
The audit's planning number (~16% pre-tax, ~13% after tax) already carries a 1.5pp selection
discount; survivorship adds 0.2–1.5pp on top, so budget **12–13% after tax**. The conclusion that
momentum_cascade is the one strategy worth running does not change: nothing else on the board
has been measured on a survivorship-free universe at all.

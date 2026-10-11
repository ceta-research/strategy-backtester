# Long-window re-run of the board's top four (2026-10-11)

**Question (Swas):** can we backtest the top four momentum/breakout strategies on current data,
over 10–20 years?

**Answer:** yes. Three of the four already were 15.7-year numbers (2010-01 → 2026-03; enhanced_breakout
stopped at 2025-01). Re-run here to the latest bar (**2026-10-09**) over 10, 15 and 20 years, on one
local snapshot of `nse.nse_charting_day`, with two data repairs the board never had.

**Only `momentum_cascade` survives every window.** The other three either lose ~2–3pp once the data
is cleaned, fall apart over 20 years, or both.

## Headline (clean data, final repairs)

> **Updated later on 2026-10-11** after the momentum_cascade audit (`../momentum_cascade_audit/AUDIT.md`)
> found three more charting defects: 12 **double-adjusted** splits (fake +100% to +945% up-days),
> **listing stubs**, and **recycled tickers** (an old company's history under a new listing's symbol).
> All repaired in `providers.py`; every number in this file is on the final data.

CAGR / max drawdown / Calmar. Strategies are price-only; NIFTYBEES reinvests dividends (≈TRI), so the
index row is flattered by roughly its yield.

| strategy | board (as published) | 2010 → now | 10y (2016→) | 15y (2011→) | 20y (2006→) | 2006–2010 only |
|---|---|---|---|---|---|---|
| **momentum_cascade** | 17.9 / −23 / 0.77 | **18.3 / −30 / 0.61** | 21.7 / −30 / 0.71 | 20.0 / −30 / 0.67 | **17.9 / −29 / 0.61** | **13.3 / −25 / 0.53** |
| eod_technical adaptive-TSL | 18.5 / −29 / 0.65 | 17.1 / −29 / 0.59 | 18.7 / −32 / 0.58 | 17.6 / −29 / 0.61 | 15.3 / **−71** / 0.21 | 6.6 / −71 / 0.09 |
| eod_breakout IR-hyst | 17.4 / −28 / 0.63 | 12.1 / −35 / 0.35 | 17.5 / −29 / 0.60 | 12.6 / −36 / 0.35 | 13.9 / −43 / 0.33 | 14.4 / −43 / 0.34 |
| enhanced_breakout | 15.9 / −25 / 0.64 | 15.5 / −24 / 0.64 | 12.7 / −40 / 0.32 | 9.6 / −39 / 0.24 | **6.2 / −64 / 0.10** | −4.0 / −62 / −0.06 |
| **NIFTYBEES** | 10.3 / −36 / 0.28 | 10.0 / −36 / 0.27 | 11.4 / −36 / 0.31 | 9.4 / −36 / 0.26 | 11.1 / −60 / 0.19 | 16.5 / −60 / 0.28 |

Board windows re-run on the clean snapshot (same config, same dates, only the data repaired):
momentum_cascade 17.87 → **18.08**, eod_technical 18.47 → **16.26**, eod_breakout 17.37 → **11.67**,
enhanced_breakout 15.87 → 16.84.

**2006–2010 is the only out-of-sample stretch.** Every parameter was tuned on 2010–2026, so 2011→ and
2016→ are in-sample subsets, not independent evidence. The 2006–2010 segment contains 2008.

## What each strategy did

- **momentum_cascade.** 18–22% in every window, drawdown −25% to −30% throughout. 2008: **−13.5%**
  against the index's −52%; its NIFTYBEES 200-day regime filter took it out. Out of sample (2006–10):
  13.3% at −25% drawdown vs the index's 16.5% at −60%. That's lower return than a strong-bull index,
  at less than half the drawdown. ⚠️ **2021 (+144%) is about a quarter of the compounding**: ex-2021 the
  20y CAGR is **13.7%** (2010→now: 12.6%). The year is broad, not one trade (TATAELXSI, ADANIENT, CDSL
  each ~+300%; the largest single trade is 5.6% of total P&L). Recent years are flat: 2025 −4.3%,
  2026 YTD +2.8%. Full audit: `../momentum_cascade_audit/AUDIT.md`.
- **eod_technical adaptive-TSL.** No market-regime filter: **2008 −65.7%, 20y max drawdown −71%**.
  The board's 18.47% also carries a fabricated ₹82L TATASTEEL trade (below). Clean: 17–19% over
  2010+, 15.3% over 20 years.
- **eod_breakout IR-hyst.** 12–18%, but **the number moves ±3pp on any perturbation**: a one-year
  start shift (2010→2011, raw data) moved it 17.9 → 14.6; split adjustment moved it 17.9 → 14.5 while
  directly touching **2 trades worth −₹1L**; dropping 124 BFUTILITIE bars moved 15y from 12.9 to 15.5.
  The mechanism is the internal regime (breadth of scanner-passing names) with `force_exit_on_regime_flip`:
  a tiny universe change shifts a regime flip, which force-exits the whole book and reshuffles every
  later slot. Quote it as a range, never a point. (Separately parked by Swas, DECISIONS #028.)
- **enhanced_breakout.** Its 15.87% was a 2010–2025 window. Start in 2011 and it's 9.6%; start in 2006
  and it's **6.2% with a −64% drawdown**. Not robust. Runs on ₹10L capital (the others ₹1 cr), so its
  liquidity caps barely bind.

## Two data repairs, and why the raw numbers can't be used

### 1. Raw splits/bonuses (`ca`)
`nse_charting_day` leaves many corporate actions unadjusted. Detected by matching every
`fmp.splits_calendar` NSE event to a bar within ±5 days whose close-to-close ratio equals the split
ratio (log tolerance 0.10, and within half the split's own size); events under 5% effect skipped
(FMP files rights issues as 1017:1000-style splits). **520 raw events found**: heavy 2003–2010
(20–60/yr), 106 across 2011–2025 (TCS 2018, BEL 2017, UNITDSPR 2018 …) and **30 in 2026 alone**
(LICI 2:1, TRENT 3:2, METROPOLIS 4:1, CUB 4:3, ANANDRATHI, KIRLPNU, TDPOWERSYS …). After
back-adjustment the jump bar's ratio is ≈1 (q01/median/q99 = 0.92/1.02/1.10) and rupee turnover is
unchanged (TCS May-2018 identical to the paisa).

Direction of the error on raw data: a held position takes the fake crash. eod_breakout/eod_technical
route it through `anomalous_drop`, which books the exit at **80% of the prior close** (a fabricated −20%);
momentum_cascade and enhanced_breakout have no such exit and eat the full ratio (momentum_cascade's 5
straddling trades averaged −38%). So raw data mostly **understates**. Before 2011 it's unusable:
momentum_cascade 2006–10 is **1.0% raw vs 13.3% adjusted**.

### 2. Interleaved stray series (`clean`)
**TATASTEEL 2018-09 → 2020** alternates between the stock (~₹55, ~9M shares/day) and a ~₹6–15 series
on ~0.3M shares (consistent, by level and dates, with the partly-paid rights shares keyed onto the
same symbol). **BFUTILITIE 2012–2015** does the same. `remove_price_oscillations` misses both because
the stray runs last several bars, not 1–2. eod_technical "bought" TATASTEEL on 2019-04-24 at **₹7.19**
(stray series) and "sold" on 2019-05-15 at **₹47.31** (real series): **+557.9%, ₹82L net = 5.4% of the
board run's total P&L**. Fix: walk each symbol that has any >2× bar; drop an off-level run unless it
persists 10 bars (a real level change); quarantine BFUTILITIE 2012–15 and WEWIN 2017–18, whose stray
series sit too close to 2× to separate. Final pass: 2,865 bars dropped across 127 symbols, on liquid
days mostly BFUTILITIE 724, TATASTEEL 260, WEWIN 124. Effect on eod_technical 2010→now: 19.9 → **17.1**.

### 3. Double adjustments, listing stubs, recycled tickers (added after the audit)
12 splits are adjusted **twice** (the ex-date jumps up by the ratio: BALKRISIND 2:1 2017, PERSISTENT
2:1 2024, FEDERALBNK 5:1 2013, CESC 10:1 2021); the repair now detects inverse jumps ≥22% too. Bars
before a listing on another scale (IDEAFORGE ×551, AWL ×153) are dropped. Symbols with a >1-year data
gap are split into separate instruments (150 symbols), because charting keys an older company's
history to a recycled ticker (AWL, INDIGO, RATEGAIN, ROSSARI, RVNL, SUPRIYA).

## Residual risk (not fixed)

`residual.py` lists every trade held across a ≥30% one-bar drop on the clean runs. What's left is
mostly missed corporate actions the calendar lacks (ENGINERSIN Dec-2016 −55%, ZOTA 2016 −50%, ATLANTAA
2010 −80%) and dead listings (ROML held 2010→2020 then −98%; AURUM 2020 −99%). Per 20y clean run:
momentum_cascade 6 trades / −₹39L, eod_breakout 15 / −₹44L (3 are BFUTILITIE 2014 bars of ~2× that
the 2.5× filter doesn't catch), eod_technical 5 / −₹7L, enhanced_breakout 2 / −₹2L. These mostly
bias **down**.

## Caveats that change how the numbers read

- **Survivorship.** `nse_charting_day` holds 2,862 survivors. 2008 above was tested on names still listed
  in 2026. Breakouts are structurally less exposed (a dying stock rarely makes new highs), but a
  bhavcopy cross-check of 2006–10 is the next study, not this one.
- **Universe size.** The scanner's ₹7 cr/day turnover and ₹50/₹99 price bars are nominal. Names that
  clear them: **157 in 2006**, 249 in 2010, 294 in 2016, **1,371 in 2026**. A 2006 result is a
  large-cap strategy; a 2024 result isn't.
- **Back-adjusted prices vs a nominal price floor.** Adjusting past prices down can drop a name under
  ₹50/₹99 that actually traded above it. The board already has this for every adjusted event; the CA
  repair extends it to the 520 raw ones.
- Costs: engine's linear 5 bps slippage is optimistic for thin names; no dividends on the stock legs.

## A correction to the 10-09 leaderboard re-run

The board's pre-fix momentum_cascade **8.28%** came from `results/momentum_cascade/champion_2005_2026.json`,
a **2005-start** run: `scripts/rebuild_leaderboard.py:113-115` pairs `config_champion.yaml` (2010 start)
with that 2005 result file. The fixed engine on the exact 2005 config gives **15.01%**. So of the
"+9.59pp from the ranking fix", **~6.7pp is the fix and ~2.9pp is the window change**.

## Reproduce

```bash
cd strategy-backtester/studies/long_window
../../../.venv/bin/python providers.py            # snapshot charting + splits calendar (~40s, 142 MB, gitignored)
../../../.venv/bin/python run_windows.py momentum_cascade            # all windows; ~1 min per run
../../../.venv/bin/python run_windows.py momentum_cascade 20y_clean  # one window
../../../.venv/bin/python bench.py && ../../../.venv/bin/python summarize.py
../../../.venv/bin/python residual.py
```

Snapshot: 8,013,638 rows, 2,862 symbols, 1995-01-05 → 2026-10-09. All four board numbers reproduced
**exactly** on it before any window was changed (17.87 / 18.47 / 17.37 / 15.87; momentum_cascade's 548
trades match `results/_lb_rerun_momentumcascade.json`). Full per-run JSONs in `out/` (gitignored); `out/summary.jsonl` has
every run including yearly returns.

## Year by year, 20y clean (%)

| year | momentum_cascade | eod_technical_atsl | eod_breakout_irhyst | enhanced_breakout | NIFTYBEES | universe |
|---|---|---|---|---|---|---|
| 2006 | 16.7 | 52.3 | 39.8 | 7.4 | 41.3 | 157 |
| 2007 | 37.6 | 28.4 | 44.0 | 72.5 | 52.9 | 196 |
| 2008 | -13.5 | -65.7 | -31.0 | -59.1 | -52.1 | 190 |
| 2009 | 24.5 | 71.9 | 32.3 | 1.6 | 75.6 | 156 |
| 2010 | 8.1 | 20.2 | 6.6 | 6.4 | 18.6 | 249 |
| 2011 | -3.6 | -15.4 | -12.8 | -17.8 | -24.0 | 201 |
| 2012 | 27.2 | 29.5 | 30.3 | 33.8 | 26.5 | 148 |
| 2013 | 9.0 | 6.3 | 4.5 | 2.2 | 7.2 | 154 |
| 2014 | 61.1 | 28.7 | 24.4 | 10.8 | 31.6 | 226 |
| 2015 | -0.8 | 10.8 | -0.4 | -3.0 | -4.3 | 283 |
| 2016 | -1.8 | -15.2 | 14.1 | -9.1 | 4.0 | 294 |
| 2017 | 57.4 | 75.6 | 37.3 | 31.6 | 29.9 | 416 |
| 2018 | -12.8 | -12.4 | -19.4 | -20.0 | 4.8 | 442 |
| 2019 | -0.1 | 3.8 | 1.8 | 0.8 | 13.6 | 325 |
| 2020 | 24.7 | 98.7 | 83.4 | 21.2 | 15.4 | 426 |
| 2021 | 143.5 | 68.9 | 55.6 | 30.3 | 26.0 | 714 |
| 2022 | 11.9 | 0.8 | -5.8 | -10.8 | 5.5 | 761 |
| 2023 | 42.3 | 44.3 | 52.1 | 78.8 | 21.0 | 912 |
| 2024 | 20.0 | 27.2 | -3.1 | 30.7 | 10.4 | 1203 |
| 2025 | -4.3 | -9.2 | -12.0 | -2.8 | 11.7 | 1296 |
| 2026 | 2.8 | 10.4 | 15.4 | 14.1 | -12.9 | 1371 |

Each strategy's 2006 figure is from a cold start on 2006-01-01, so it's a partial-deployment year.

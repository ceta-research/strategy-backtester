# Blue-chip de-rating rule — backtest

**Asked:** 2026-10-10, "yes, backtest it. Use bhav-copy data."
**Rule under test:** buy a large, liquid, profitable NSE stock trading ≥25% below its
trailing 2-year high, with EPS above its level two fiscal years earlier and a required
re-rating multiple ≤35×; **hold until it regains that 2-year high**. Equal weight.
**Origin:** the 2026-10-09 screen in `screen/` (10 names, mean upside +78.8% as reported).

---

## The answer in three lines

1. **Held to recovery as Swas described it, a properly diversified implementation does not
   beat the index.** 20 equal-weight slots return **9.16% CAGR against NIFTYBEES's 10.27%**
   over the same 15.7 years, and the book ends with capital trapped in UCOBANK −78%,
   IDEA −82%, RPOWER −66%. This is the one headline number that is capital-weighted rather
   than an average of ratios, so it is the one to trust.
2. **A 2–3 year time stop is what makes it work at all**, because the defect is the exit,
   not the entry: 20 slots go to **14.64% CAGR (+4.37pp over the index)** and 10 slots to
   22.31% (+12.04pp). But the 10-slot figure rests on 60 closed trades and is the same
   effect as "concentrate on the deepest names", not a second confirmation.
3. **Drawdown never improves on the index** (−27.7% to −38.0% vs NIFTYBEES's −36.34%).
   There is no risk-reduction story here, only a return story.

**For a ₹42L basket: this does not clear the bar.** The version Swas described trails
the index; the version that works needs a time stop he did not specify, delivers ~+4pp when
diversified, and rests on fewer trades than the leaderboard entries I just told him not to
trust. It belongs in the background-research track, not the main cash. **Nothing was placed.**

⚠️ **Per-trade "excess" figures in this document are metric-fragile — read the warning
below before quoting any of them.**

---

## Method — what was point-in-time, and what the data forced

| Leg | Source | Why |
|---|---|---|
| Universe | `nse.nse_bhavcopy_historical`, top 150 by trailing-1y rupee traded value, ≥180 trading days, profitable in the last published FY | Rupee turnover is **corporate-action invariant** and the table is survivorship-free (5,072 symbols). Today's `fmp.profile.marketCap` would be pure look-ahead. |
| Price path | `nse.nse_charting_day` (adjusted) | Needed for a level-comparison signal. |
| EPS | `fmp.income_statement`, FY, usable at **period end + 120 days** | 🚨 `filingDate` == period-end `date` in 41,103 of 41,177 NSE FY rows, so there is **no real filing date for India**. SEBI LODR Reg 33 allows 60 days for audited annuals; 120 is the safe margin. |
| Dividends | `fmp.dividend_calendar` (`adjDividend`) added to the stock leg | charting is price-only; NIFTYBEES reinvests, so without this the benchmark was favoured by the yield. |
| Benchmark | NIFTYBEES, same entry and exit dates per trade | 6,148 bars from 2002-01-08, split-clean. |
| Costs | 0.15% each way | slippage + charges. |

**Window: 2011-01-31 → 2026-06-30, 186 monthly decision dates.** It starts in 2011 and not
earlier because of a finding this study produced:

> Auditing all 1,599 NSE split events that `nse_charting_day` covers, the table is
> **adjusted from 2011 onward but RAW for 2003–2010** (2005: 44 raw vs 5 adjusted; 2006:
> 55 v 4; 2008: 49 v 2; 2010: 60 v 13; 2024: 0 v 117; 2025: 0 v 54). A 2006–2010 arm would
> fabricate a −50% crash on every bonus. **The 2008 GFC is therefore not testable here** —
> the single best regime for this rule is out of reach without external corporate actions.

Demergers are never adjusted in either table: ITC printed **−8.1%** on its hotels-demerger
ex-date 2025-01-06, which a −20% detector cannot see. The study truncates the 2-year-high
window at detected level shifts; `exclude` and `off` arms are reported as sensitivities.

---

## Results — 253 distinct drawdown episodes, 98 symbols

Monthly re-qualification would count the same drawdown up to 14 times; 1,146 monthly
signals collapse to **253 distinct episodes**. That is the honest sample size.

| | value |
|---|---|
| Recovered to target, ever | 209 / 253 = **83%** |
| within 12m / 24m / 36m | **26% / 51% / 69%** |
| Median time to recovery | **612 days (20 months)**; p75 35 months, p90 75 months |
| Drawdown **before** recovery | median **−22.5%**, p25 −43.4%, p10 −67.4%, worst −98.2% |
| Fell a further ≥20% / ≥40% | **53% / 28%** |
| Net return | median +45.0%, mean +58.9%, worst −85.3% |
| **Excess vs NIFTYBEES** | median **+14.0%**, mean **−2.0%**, beat the index 65% |

**The mean/median split is the whole story:** usually a little better than the index,
occasionally catastrophically worse.

### The regime cut — the test that matters
Advisor's point: in a rising market the index also recovers to its prior high, so raw hit
rate flatters. Splitting on whether NIFTY was itself ≥15% off its own 2-year high at entry:

| regime | n | recovered | mean excess |
|---|---:|---:|---:|
| Broad market drawdown | 81 | 86% | **−6.1%** |
| Idiosyncratic fall (index near its high) | 172 | 81% | **+0.0%** |

**Mean excess is not positive in either regime.** Today's basket (TCS, HDFCBANK, ITC, IRFC,
IOC) is the broad-drawdown case — the worse of the two.

### By entry cohort (selected)
| year | n | recovered | med days | med excess | med drawdown-first |
|---|---:|---:|---:|---:|---:|
| 2011 | 45 | 89% | 1,289 | **−10.4%** | **−38.8%** |
| 2013 | 33 | 91% | 316 | +20.0% | −17.8% |
| 2018 | 17 | 88% | 1,132 | +11.8% | −51.8% |
| 2020 | 16 | 100% | 331 | +14.8% | −18.9% |
| 2025 | 20 | 40% | 302 | +14.6% | −7.7% |
| 2026 | 11 | 9% | 96 | −1.6% | −7.4% |

2011 is the cautionary cohort and the closest analogue to today: you got your money back
after 3.5 years, having been down 39% on the way, and **did worse than holding the index.**

---

## ⚠️ Why per-trade excess is the wrong metric here (a correction to my own first pass)

My first write-up led with **arithmetic** per-trade excess (`net − bench`) and concluded the
edge lived entirely in deep entries: mean excess **+41.4%** for ≤−35% vs **−23.5%** for the
−25..−35% band. **Both numbers are artifacts and that conclusion does not stand.**

- Arithmetic excess is **unbounded over long holds**. RELINFRA, held 14.3 years, scores
  −433% excess simply because the index compounded 4.6× meanwhile. That one mechanism is
  what made the shallow bucket look value-destroying: it is full of long-held laggards.
- On the other side, **161% of the deep bucket's total excess came from three episodes of a
  single stock, HINDPETRO** (+1852.9%, +1174.9%, +564.4%). Trim 10% off each tail and the
  deep bucket's mean excess falls from +41.4% to **+2.1%**; drop the best three and it is
  **−26.6%**.
- Annualising (trade CAGR − index CAGR) fixes the long-hold blow-up but breaks the short
  end: CHOLAFIN returns +215%/yr on a 0.6-year hold. The *mean* annualised excess
  (+106.8%) is equally meaningless.

What survives, on **median annualised excess** — robust to both tails:

| bucket | n | median ann. excess | beat index | p10 |
|---|---:|---:|---:|---:|
| all ≥−25% off high | 261 | **+7.4%/yr** | 65% | −9.2% |
| shallow −25% to −35% | 208 | **+6.7%/yr** | 63% | −9.2% |
| DEEP ≤−35% | 53 | **+10.6%/yr** | 70% | −9.0% |
| DEEP, excluding HINDPETRO | 50 | **+7.9%/yr** | 68% | — |

So deep entries are modestly better, not categorically different, and **the shallow band is
not value-destroying.** The honest statement is that the entry signal carries a few points a
year of information across the board, and that **the portfolio simulation — which compounds
real capital through real holding periods — is the only aggregation worth quoting.**


---

## Survivorship-corrected results — the decisive table

The universe is survivorship-free but `nse_charting_day` is not: **112 of 450 universe
symbols are absent, 17.1% of all universe slots.** Each was classified (rename / share-swap
/ demerger vs real failure) and the real failures were re-run on bhavcopy and folded in.
Renames and swaps are **dropped**, which is conservative — they showed as winners
(TATAMOTORS +36.6%, SRTRANSFIN +38.9%, CADILAHC +58.7%, HDFC +21.4%).

Mean excess over NIFTYBEES, over identical holding dates:

| exit rule | bucket | n | recovered | med net | mean net | med excess | **mean excess** | beat idx | p10 net | med months |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hold until recovery | all ≥−25% | 266 | 80% | +44.7% | +54.5% | +12.2% | **−10.3%** | 64% | −14.2% | 21.0 |
| hold until recovery | shallow −25..−35% | 212 | 81% | +41.8% | +35.2% | +10.6% | **−23.5%** | 62% | −12.3% | 20.1 |
| hold until recovery | **DEEP ≤−35%** | **54** | 76% | +71.6% | +130.3% | +26.3% | **+41.4%** | 69% | −14.5% | 27.7 |
| + 24-month stop | all ≥−25% | 266 | 91% | +32.2% | +27.4% | +6.6% | +9.3% | 56% | −34.4% | 21.0 |
| + 24-month stop | shallow | 212 | 90% | +29.2% | +16.8% | +6.5% | **+0.8%** | 56% | −32.2% | 20.1 |
| + 24-month stop | DEEP | 54 | 94% | +56.5% | +68.8% | +10.1% | +42.8% | 57% | −36.9% | 24.0 |
| + 36-month stop | all ≥−25% | 266 | 91% | +38.6% | +38.0% | +10.8% | +11.4% | 62% | −30.8% | 21.0 |
| + 36-month stop | shallow | 212 | 90% | +38.1% | +24.1% | +10.2% | **−0.3%** | 62% | −30.2% | 20.1 |
| + 36-month stop | **DEEP** | **54** | 94% | +62.0% | +92.6% | +24.6% | **+57.4%** | 65% | −35.5% | 27.7 |

Before correction the all-entry mean excess read −2.0%; the real failures take it to
**−10.3%**. A bound treating every unverified ticker as a total loss gives −10.5% and leaves
the deep bucket at +43.0%, so the split is not an artefact of the classification.

⚠️ The mean-excess column in the table above is the metric criticised in the warning
section; the bucket *split* it implies is not reliable. The recovery rates, median net
returns and p10 columns are unaffected by that problem.

**Two readings of one effect, not two findings.** The 10-slot/36-month portfolio result
(+12.04pp over the index) and the deep-bucket excess are the same thing: the sim fills slots
deepest-first, so 10 slots ≈ the deep bucket each month, while 20 slots dilutes into the
shallow band and the edge falls to +4.37pp. Do not count them as independent confirmation.

**What the rule actually is.** A deep entry with `pe_needed ≤ 35×` implies `pe_now ≲ 17×` on
a name 50% off its high. This is a **value screen on large caps with a drawdown trigger** —
the outperformance, to the extent it survives, is more plausibly the value premium than a
"recovery" effect. Note `low_pe` is independently the best Calmar on our own leaderboard
(1.05) and is the cheaper way to own the same exposure.


---

## Sensitivity — the conclusion is not threshold-dependent

| arm | episodes | recovered | med net | med excess | med drawdown |
|---|---:|---:|---:|---:|---:|
| off-high −20% | 335 | 83% | +33.2% | +14.0% | −17.1% |
| **off-high −25% (base)** | 253 | 83% | +45.0% | +14.0% | −22.5% |
| off-high −30% | 208 | 82% | +57.9% | +18.5% | −23.9% |
| off-high −35% | 148 | 82% | +67.4% | +24.0% | −26.4% |
| off-high −40% | 94 | 77% | +81.5% | +21.6% | −36.2% |
| no EPS / multiple legs | 681 | 76% | +41.6% | +10.2% | −22.0% |
| EPS leg, no multiple cap | 450 | 75% | +40.2% | +9.3% | −21.4% |
| required multiple ≤25× | 189 | 84% | +46.4% | +14.1% | −21.7% |
| CA mode exclude / off | 233 / 459 | 84% / 84% | +45.4% / +46.4% | +15.4% / +19.0% | — |
| universe top 50 / top 100 | 106 / 182 | 83% / 81% | +45.8% / +45.7% | +14.0% / +10.6% | — |

Buying deeper is better per trade and costs more drawdown. **The EPS and multiple legs earn
their keep** (+14.0% vs +10.2% median excess, 83% vs 76% recovery). Corporate-action
handling does not drive the result.

---

## Portfolio — where the rule as stated fails

Equal weight, fixed slots, fill on the bar after the decision date, 2011-01 → 2026-10 (15.7y).
NIFTYBEES buy-and-hold over the identical window: **10.27% CAGR, MDD −36.34%.**

| exit rule | slots | CAGR | MDD | Calmar | vs index | closed |
|---|---:|---:|---:|---:|---:|---:|
| hold until recovery | 5 | 18.33% | −37.13% | 0.49 | +8.06pp | **15** |
| hold until recovery | 10 | 11.48% | −31.30% | 0.37 | +1.21pp | 21 |
| hold until recovery | 20 | 9.16% | −29.75% | 0.31 | **−1.10pp** | 46 |
| + 24-month time stop | 10 | 20.37% | −37.98% | 0.54 | +10.10pp | 77 |
| **+ 36-month time stop** | **10** | **22.31%** | **−27.66%** | **0.81** | **+12.04pp** | 60 |
| + 25% stop-loss | 10 | 16.59% | −40.87% | 0.41 | +6.32pp | 93 |
| + 24-month time stop | 20 | 14.23% | −36.63% | 0.39 | +3.96pp | 130 |
| + 36-month time stop | 20 | 14.64% | −35.09% | 0.42 | +4.37pp | 107 |

The 5-slot 18.33% is **not a result** — 15 closed trades in 15.7 years and 99% of final
equity sitting in 5 open positions. Ignore it.

The 20-slot hold-till-recovery book ends holding **UCOBANK −78%, IDEA −82%, RPOWER −66%,
UNIONBANK −47%, PNB −37%**. Those are large, liquid, profitable names that fell 25% with
EPS intact, and the rule says hold them forever.

### Capping the hold, at episode level
| variant | med net | mean net | med excess | **mean excess** | beat idx | worst |
|---|---:|---:|---:|---:|---:|---:|
| hold until recovery | +45.0% | +58.9% | +14.0% | **−2.0%** | 65% | −85.3% |
| + 12-month stop | +6.4% | +17.2% | −1.8% | +8.0% | 48% | −71.6% |
| + 24-month stop | +33.9% | +30.1% | +8.2% | **+12.0%** | 58% | −77.0% |
| + 36-month stop | +39.0% | +41.4% | +11.8% | **+14.9%** | 64% | −79.9% |
| + 25% stop-loss | −5.4% | +14.8% | −3.2% | +5.7% | 47% | **−42.7%** |

A time stop converts opportunity cost into a realised exit. A stop-loss cuts the worst
case from −85% to −43% but costs most of the edge — it exits the drawdowns that were the
entry thesis.

---

## 🚨 The bias that is NOT yet in those numbers

The universe is survivorship-free; the price path is not. **112 of 450 universe symbols are
absent from `nse_charting_day` — 17.1% of all universe slots**, and the absentees are
exactly the names that matter: RCOM, JETAIRWAYS, HDIL, RELCAPITAL, IBREALEST, SINTEX,
JPASSOCIAT, ORIENTBANK, plus renames (TATAMOTORS 181 of 186 months, HDFC, MCDOWELL-N,
SRTRANSFIN, MINDTREE, CADILAHC).

Raw, that leg gives 35 episodes with excess median −45.2% and mean −96.9%. But most of
those tickers ended in a **rename or share swap, not a loss** — IDFC→IDFCFIRSTB (104% of
value retained), HDFC→HDFCBANK, LTI→LTIM, AMARAJABAT→ARE&M, SRTRANSFIN→SHRIRAMFIN,
TATAMOTORS→TMPV. Only 6 symbols are real failures or real price collapses: **RCOM
(−95.9%), SINTEX (−92.4%, 5 episodes, equity extinguished at NCLT), RELCAPITAL, RELINFRA,
BGRENERGY, ABAN** = 13 episodes. Those are what the corrected table above folds in.

⚠️ One classification needed overriding: an agent returned RCOM as "still listed, 100%
recovered". The ticker does survive at about ₹2.60 — but a holder who entered at ₹62.9 lost
96%. Ticker survival is not holder recovery, so RCOM is kept at its real mark.

⚠️ Episode counting cuts the other way here: the episode table counts SINTEX **five times**
(it re-qualified monthly), whereas a rule-follower holds from the first entry and does not
re-enter. So the merged episode numbers are **pessimistic** on persistent losers, and the
portfolio sim is the behavioural truth.

## Other known limits
- **2008 GFC not testable** (charting raw before 2011) — the best regime for this rule.
- **Thin sample, and thinnest exactly where the edge is:** 266 corrected episodes but only **54 in the deep bucket** over 15.7 years (~3.4/year), across ~6 de-rating clusters; 60–130 closed portfolio trades. The deep-bucket mean is also skewed by a handful of very large winners — the median (+26.3%) and the 69% beat rate are the figures to lean on.
- **The regime split is too thin to use.** NIFTYBEES is −14.9% off its own 2-year high today, against a −15% cut. The two deep sub-buckets give opposite risk reads for a 0.1pp difference (P(further −40%) 18% vs 48%; drawdown-first −14.5% vs −37.8%). Neither is reliable; today is on the boundary.
- **Concentration-sensitive:** 10 slots 22.31% vs 20 slots 14.64% under the same rule. High variance, not a robust edge.
- **Drawdown is no better than the index** (−27.7% to −38% vs −36.3%). This is not a lower-risk alternative.
- Right-censored: 44 of 253 charting episodes still open; 2024–26 cohorts have not had time.
- The level-shift detector flags 2,722 days across 319 symbols (~8.5/symbol), which is too many for genuine corporate actions — it is catching earnings crashes. Sensitivity arms show it does not drive the result.

## Correction to the 2026-10-09 screen
**ITC's stated upside is overstated by 15.6pp.** Its 2-year high of 512.55 (2024-10-08)
predates the ITC Hotels demerger, which removed 8.09% of value on 2025-01-06 and is not
adjusted in charting. Reachable high ≈ **471.10**, so upside from 266 is **+77.1%, not
+92.7%**. Basket mean upside **+78.8% → +77.2%**.
Also flagged and not yet acted on: IRFC needs ~31× earnings to regain its high, which for a
fixed-spread railway lease financier is a re-rating bet, not a recovery bet.

## Reproduce
```
cd studies/bluechip_derating/backtest
python pull.py        # caches every input (idempotent)
python main.py        # event study + sensitivity + portfolio   -> main.log
python variants.py    # time stops and stop-losses
python surv.py        # survivorship leg on bhavcopy
python p0_ca_audit.py # the 1,599-event adjustment-era audit
```
Cached inputs live in the session scratchpad and are re-pullable; nothing here reads a
local parquet.

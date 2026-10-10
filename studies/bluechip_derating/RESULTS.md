# Blue-chip de-rating rule — backtest

**Asked:** 2026-10-10, "yes, backtest it. Use bhav-copy data."
**Rule under test:** buy a large, liquid, profitable NSE stock trading ≥25% below its
trailing 2-year high, with EPS above its level two fiscal years earlier and a required
re-rating multiple ≤35×; **hold until it regains that 2-year high**. Equal weight.
**Origin:** the 2026-10-09 screen in `screen/` (10 names, mean upside +78.8% as reported).

> **Read this first.** This document was rewritten three times. Two earlier conclusions were
> artefacts of my own defects — a window bug and a corrupt vendor field — and are retracted
> in full at the bottom. The numbers below are the post-fix ones. The verdict never changed.
>
> The defects were found by an adversarial five-lens audit over this code (29 agents, five
> lenses plus refute-checks). Three lenses independently identified the dividend corruption.
> Audit output: `backtest/audit_2026-10-10.json`.

---

## The answer

**NIFTYBEES buy-and-hold, same 15.7 years: 10.27% CAGR, MDD −36.34%, Calmar 0.28.**

| exit rule | slots | CAGR | MDD | Calmar | vs index |
|---|---:|---:|---:|---:|---:|
| hold until recovery | 10 | 11.25% | −37.40% | 0.30 | +0.98pp |
| hold until recovery | **20** | **8.16%** | −38.74% | 0.21 | **−2.10pp** |
| + 36-month time stop | 10 | 11.11% | −59.72% | 0.19 | +0.84pp |
| + 36-month time stop | 20 | 9.82% | −51.60% | 0.19 | −0.45pp |
| **deep entries only (≤−35%)** | 20 | **6.75%** | −39.16% | 0.17 | **−3.52pp** |

Price-only on **both** legs, which is the only internally consistent comparison available:
`fmp.dividend_calendar` holds **zero rows for NIFTYBEES** in any ticker form, so the
benchmark could never be credited a yield either. ⚠️ I earlier justified crediting the stock
leg alone by asserting "NIFTYBEES reinvests, so the benchmark was favoured by the yield" —
that had no evidential basis and is withdrawn. NIFTYBEES has in fact paid occasional
distributions, so crediting both legs properly would raise the **index** and make the rule
look slightly worse. Sanitising rather than dropping the stock dividends gives 12.80% /
−32.30% at 10 slots (+2.53pp) and **9.46% / −34.55% at 20 slots (−0.81pp)** — same picture.

1. **Diversified, the rule does not beat the index.** At 20 slots it trails by 0.8–2.1pp.
   The 10-slot figure (+1.0 to +2.5pp) rests on **26 closed trades in 15.7 years** and is
   indistinguishable from noise.
2. **Drawdown is never better and is often far worse** — −32% to −60% against the index's
   −36%. A time stop raises nothing and costs 12–23pp of drawdown. There is no
   risk-adjusted case: the best Calmar here is 0.40 against the index's 0.28, and that is
   the 26-trade arm.
3. **Buying deeper is worse, not better.** Deep-only is the single worst arm, −3.52pp.
4. Only ~70% of equity is ever invested (slots cap names, not capital), so part of the
   shortfall is cash drag and this is not a clean like-for-like against a fully-invested
   index. ⚠️ It cuts the other way on risk: **the drawdowns above are cash-damped.** Measured
   on invested capital they are materially worse (the audit puts it at roughly 19pp on the
   hold-to-recovery books), so the risk comparison in this table *flatters* the rule.
5. ⚠️ **The sign of every portfolio headline is sensitive to the start month** and to
   fill-queue path dependence — with 26–45 closed trades, which name happens to take a slot
   first changes the result. Treat the CAGR gaps as ±2pp at best.

**For a ₹42L basket: no. This does not clear the bar, and it is not close.** Per
episode the signal does carry information — median annualised excess ~+9.5%/yr, beats the
index 67% of the time — but it does not survive being turned into a portfolio, because the
third of entries that go wrong go very wrong (p10 net −24.8% overall, **−88.8% on deep
entries**, only 71% of deep entries ever recovering and taking a median 35 months).
**Nothing was placed.**

---

## Method

| Leg | Source | Why |
|---|---|---|
| Universe | `nse.nse_bhavcopy_historical`, top 150 by trailing-1y rupee traded value, ≥180 trading days, profitable in the last published FY | Rupee turnover is **corporate-action invariant** and the table is survivorship-free (5,072 symbols). Today's `fmp.profile.marketCap` would be look-ahead. |
| Price path | `nse.nse_charting_day` (adjusted) + `nse_bhavcopy_historical` for the 112 symbols charting lacks | A level-comparison signal needs adjusted prices; the delisted names exist only in bhavcopy. |
| EPS | `fmp.income_statement`, FY, usable at **period end + 120 days** | `filingDate` == period-end `date` in 41,103 of 41,177 NSE FY rows, so India has **no real filing date**. SEBI LODR Reg 33 allows 60 days for audited annuals. |
| Dividends | **not trusted** — see the defect below | |
| Benchmark | NIFTYBEES, same entry and exit dates per trade | 6,148 bars from 2002-01-08, split-clean. |
| Costs | 0.15% each way | |

**Window 2011-01 → 2026-06, 186 monthly decision dates, 511 distinct episodes** (462 from
charting, 49 from the delisted names). It starts in 2011 because of a finding this study
produced: auditing all 1,599 NSE split events charting covers, the table is **adjusted from
2011 but RAW for 2003–2010** (2005: 44 raw vs 5 adjusted; 2024: 0 vs 117). A 2006–2010 arm
would fabricate a −50% crash on every bonus, so **the 2008 GFC is not testable here** —
the best regime this rule has. ⚠️ Even post-2011 is not uniform: **90 split events from
2011 on are raw, including TCS 2:1 on 2018-05-31**, which is why corporate actions are taken
from the splits calendar rather than inferred from price.

---

## 🚨 Data defects this study found

1. **`fmp.dividend_calendar.adjDividend` is corrupt for NSE.** 53 rows exceed 25% of the
   share price; **HINDPETRO 2021-07-08 reads ₹1,314.93 against a ₹189.23 price (6.9×)**, and
   DRREDDY 2018-07-16 reads ₹1,727.64 against ₹418.79. Those rows carry **25.3% of all
   dividend rupee value** in the pull. HINDPETRO was the largest single contributor to what
   I had reported as a deep-entry edge, so **that edge was this defect.** `adjDividend` is
   correctly *split*-adjusted (WIPRO's pre-bonus 1.0 → 0.5) — the corruption is separate and
   needs a `div/price` sanity cap on any use.
2. **charting's adjustment era** — above.
3. **No Indian filing dates** in `fmp.income_statement` — above.
4. **bhavcopy `TURNOVER` is NULL from mid-2024** (100% in 2025–26). `CLOSE*VOLUME` is a
   validated substitute: 2023 top-100 overlap 100/100, rank correlation 1.0.
5. Minor: ~0.6% of dividend rows are near-duplicates one day apart (0.4% of value).

All five are recorded in `CLAUDE.md` → `operational_rules.nse_price_data`.

---

## Per-episode results, 511 episodes

| exit rule | bucket | n | recovered | med net | **med ann. excess** | beat idx | p10 net | med months |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| hold until recovery | all ≥−25% | 511 | 80% | +45.4% | **+9.5%/yr** | 67% | −24.8% | 23.0 |
| hold until recovery | shallow −25..−35% | 380 | 83% | +42.8% | +9.0%/yr | 67% | −10.3% | 20.3 |
| hold until recovery | DEEP ≤−35% | 131 | **71%** | +75.6% | +12.8%/yr | 68% | **−88.8%** | **35.2** |
| + 24-month stop | all | 511 | 92% | +32.5% | +4.5%/yr | 56% | −40.6% | 23.0 |
| + 36-month stop | all | 511 | 91% | +39.3% | +8.3%/yr | 63% | −33.0% | 23.0 |

Use **median annualised excess**. Arithmetic excess is unbounded over long holds (a 14-year
hold scores −433% purely because the index compounded meanwhile) and annualised *means* are
broken by short holds (+215%/yr on a 0.6-year hold). Both produced earlier nonsense here.

The deep bucket is where the rule's reputation dies: a third never recover, the median wait
is nearly three years, and one entry in ten loses ~89%.

---

## Survivorship

The universe is survivorship-free; charting is not — **112 of 450 universe symbols are
absent, 17.1% of universe slots.** The missing names were re-run on bhavcopy and folded in.
The 49 surviving episodes span 21 symbols including **DHFL, HDIL, EDUCOMP, IBREALEST,
JPASSOCIAT, ARSSINFRA, OPTOCIRCUI, IBULHSGFIN** — marked to **zero** where the equity was
extinguished at NCLT. Renames and share swaps are dropped, which is conservative: they were
winners (TATAMOTORS +36.6%, SRTRANSFIN +38.9%, CADILAHC +58.7%, IDFC→IDFCFIRSTB 104%).

The portfolio sim runs on the **combined** panel, so the deepest-first fill really does buy
DHFL and HDIL and really does take them to zero.

⚠️ A classifier agent returned RCOM as "still listed, 100% recovered". The ticker survives
at about ₹2.60 — a holder who entered at ₹62.9 lost 96%. **Ticker survival is not holder
recovery.**

---

## 🚨 Retractions — two earlier conclusions were my own defects

**Retraction 1 — "the edge lives entirely in deep (≤−35%) entries, +41.4% mean excess vs
−23.5% for the −25%–−35% band."** Three causes, all mine:
- `ca_mode='truncate'` **never truncated.** After shortening the lookback I re-applied
  `MIN_BARS=400` to the *shortened* window, so any corporate action inside the most recent
  ~19 of 24 months silently **dropped** the candidate. Only 7 of 253 episodes were truly
  truncated and **53% of qualifying signals were deleted.** Fixed: the history requirement
  is tested on the full window; truncation affects only the high. Episodes 253 → 462.
- The level-shift detector was **96.4% false positives** (70 of 2,722 flags near a real
  split) — an earnings-crash detector. It removed deep episodes at 55% vs 41% shallow, which
  manufactured both halves of the split. Replaced with splits-calendar events.
- Worst: the same filter ran on the bhavcopy leg and **deleted the real failures before they
  could be classified** — circular, since an NCLT-bound company necessarily has ≥8% down
  days. I named HDIL as "exactly the name that matters" and shipped a correction containing
  neither it nor IBREALEST. Survivorship leg 35 → 49 episodes.
- And the residue of the "edge" was the HINDPETRO dividend corruption.
**Deep-only is now the worst arm (−3.52pp), not the best.**

**Retraction 2 — "held to recovery it loses (−1.10pp), and a 2–3 year time stop rescues it
(+12.04pp)."** The −1.10pp was right by accident; under the fixes the diversified shortfall
is −0.81 to −2.10pp. The time-stop "rescue" is dead: it adds 12–23pp of drawdown (−51.6%
and −59.7%) for no CAGR gain. **A time stop is not the fix.**

What survived every version: **the rule is not a safe alpha and a ₹42L basket should not go
to it.**

---

## Other limits
- Thin: 26–45 closed trades in the hold-to-recovery arms over 15.7 years.
- Concentration-sensitive: 10 slots +0.98pp vs 20 slots −2.10pp, same rule.
- ~70% average invested, so the index comparison is not fully like-for-like.
- 53 symbols / 907 universe slots clear the price trigger and are dropped for having no FY rows in `fmp.income_statement`.
- Right-censored: the 2024–26 cohorts have not had time to resolve.
- Not audited: entry at the close rather than a realistic fill, and no impact cost.
- **EPS and the adjusted price sit on inconsistent bases in places**, so the `pe_now ≤ 60` / `pe_needed ≤ 35` gates are contaminated for names whose price era and share-count era disagree. The gates still earn their keep directionally but their exact thresholds are not meaningful.
- The audit found the deepest-first fill does **not** in fact explain the 10-slot vs 20-slot gap, so an earlier claim of mine that the concentrated and deep-bucket results were "the same effect seen twice" is withdrawn — the gap is concentration and path dependence, not entry depth.

## Correction to the 2026-10-09 screen
**ITC's screened upside is overstated by 15.6pp.** Its 2-year high of 512.55 (2024-10-08)
predates the ITC Hotels demerger, which removed 8.09% on 2025-01-06 and is unadjusted in
charting. Reachable high ≈ **471.10**, so upside from 266 is **+77.1%, not +92.7%**. Basket
mean **+78.8% → +77.2%**. IRFC separately needs ~31× earnings to regain its high — a
re-rating bet, not a recovery bet.

## Reproduce
```
cd studies/bluechip_derating/backtest
python pull.py        # caches every input (idempotent)
python pull_surv.py   # bhavcopy for the 112 missing names
python p0_ca_audit.py # the 1,599-event adjustment-era audit
python fixed_run.py   # CORRECTED event study          -> fixed_run.log
python fixed_pf.py    # CORRECTED portfolio            -> fixed_pf.log
python nodiv.py       # FINAL, dividend field untrusted -> nodiv.log
```
`study.py` / `report.py` / `variants.py` / `final.py` / `merge2.py` are the **superseded**
first pass, kept only so the retractions above can be checked. Do not quote their output.

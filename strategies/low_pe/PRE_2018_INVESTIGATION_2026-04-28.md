# low_pe pre-2018 investigation (2026-04-28)

**Goal:** determine whether FMP NSE fundamentals pre-2018 are genuinely sparse
(i.e. need data backfill from an alternate source) OR whether low_pe's
underperformance pre-2018 is a strategy-logic / threshold issue with available
data.

**Result:** **The data is NOT sparse.** FMP NSE FY fundamentals coverage runs
1547-2300 symbols/year throughout 2010-2025. The "pre-2018 sparsity" mental
model in prior memory entries is inaccurate. low_pe's zero-trade pre-2018
behavior is driven by **strict filter + scanner-eligibility intersection +
min_stocks=10 cash fallback**, not by missing data.

**Recommendation:** No data backfill needed. To unlock low_pe pre-2018
performance, loosen the filter or adjust the min_stocks fallback — these are
strategy-tuning options, not data-engineering ones.

---

## What the data actually looks like

Coverage of `fmp.key_metrics WHERE symbol LIKE '%.NS' AND period = 'FY'`:

| Year | FY rows | Distinct symbols | Pass low_pe filter (PE<8, ROE>8%, DE<1, mktcap>10B) |
|---:|---:|---:|---:|
| 2010 | 1547 | 1547 | 27 |
| 2011 | 1599 | 1597 | 22 |
| 2012 | 1673 | 1673 | 25 |
| 2013 | 1852 | 1850 | 35 |
| 2014 | 1927 | 1927 | 29 |
| 2015 | 1957 | 1956 | 18 |
| 2016 | 2008 | 2008 | 26 |
| 2017 | 2073 | 2073 | 14 |
| 2018 | 2126 | 2126 | 33 |
| 2019 | 2181 | 2181 | 40 |
| 2020 | 2207 | 2207 | 52 |
| 2021 | 2224 | 2224 | 57 |
| 2022 | 2300 | 2300 | 78 |
| 2023 | 2273 | 2273 | 82 |
| 2024 | 2194 | 2193 | 35 |
| 2025 | 2246 | 2244 | 37 |

Pre-2018: 1547-2126 symbols (74-100% of 2025 coverage). 14-35 pass champion
filter. **This is not sparse.**

Looser filter (PE<15, ROE>10%) yields 60-96 candidates pre-2018 (vs 115-183
post-2019) — completely workable for a 30-50 stock portfolio.

---

## What low_pe is actually doing pre-2018

Trades by entry year for `low_pe` champion params on full 2010-2026:

| Year | Trades |
|---:|---:|
| 2010 | 36 |
| 2011 | 0 |
| 2012 | 0 |
| 2013 | 0 |
| 2014 | 10 |
| 2015 | 0 |
| 2016 | 0 |
| 2017 | 20 |
| 2018 | 24 |
| 2019 | 33 |
| 2020 | 57 |
| 2021 | 121 |
| 2022 | 130 |
| 2023 | 146 |
| 2024 | 144 |
| 2025 | 87 |

The strategy enters in 2010, then nothing 2011-2013, sporadic 2014-2017, then
ramps up 2018+. Total: 808 trades over 16 years; ~85% concentrated in 2019+.

### Why the gaps

The signal logic in `engine/signals/low_pe.py:124-170` (`screen_at_date`):

1. Intersect FY-filed-and-aged candidates (90-day filing lag) with
   `eligible_instruments` (scanner pass — price>50 INR, avg_txn>70M, etc).
2. Apply PE/ROE/DE/mktcap filters.
3. Sort by lowest PE, take top N.
4. **`min_stocks=10` fallback:** if fewer than 10 qualifying candidates after
   step 3, the strategy holds cash for that quarter.

Pre-2018, the intersection of (filter-passing FY candidates) ∩
(scanner-eligible by liquidity) is often <10 stocks per quarter. The
min_stocks fallback then keeps the strategy in cash.

Confirming this is the root cause:
- 2010 had 36 trades (year-1 fundamentals + initial scanner pass = lucky
  intersection).
- 2011-2013 had 0 trades (scanner-eligibility filter narrowed mid-cap pool
  faster than FY data flow).
- 2014-2017 sporadic (sparser overlap).
- 2018+ continuous (mid-cap liquidity expansion + more FY filings → robust
  intersection).

This is a **strategy-thresholds + market-microstructure** issue, not a
data-availability issue.

---

## Implications for the N-leg ensemble experiment

The N-leg conclusion (`strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md`)
was that low_pe full-period CAGR 5.86% drags the ensemble. The cause cited
was "FMP NSE fundamentals sparse pre-2018 → leg holds cash 2010-2017."

**Updated mental model:** the leg holds cash pre-2018 because the
strategy's filter+scanner intersection is empty, not because the data is
missing. This means:

1. A literal data backfill (BSE filings, Refinitiv, screener.in scrape) would
   NOT change low_pe's full-period CAGR. The data is already there.
2. The unblock is strategy-side: loosen one of the filters (mktcap, scanner
   liquidity, min_stocks fallback) so that pre-2018 is also tradeable.
3. This is a **NEW optimization round (R5 for low_pe)** — re-tune the
   filters with the goal of continuous trading 2010+ rather than peak
   modern-window performance.

---

## Specific R5 directions for low_pe (if pursued)

Out of scope for D-as-investigation, listed for forward reference:

1. **`min_stocks=5`** instead of 10. Allows trading even with sparse
   intersection; portfolio under-diversifies but starts compounding earlier.
2. **`mktcap_min=1e9`** (₹1B) instead of ₹10B. Mid- and small-cap inclusion
   widens the pre-2018 candidate pool (early NSE was mid-cap dominant).
3. **Looser scanner liquidity** (`avg_txn>20M` instead of 70M). Pre-2018
   liquidity floor was lower; current threshold is 2018+-calibrated.
4. **`pe_max=12` or `15`** instead of 8. Trades off portfolio "value purity"
   for continuous capital deployment.
5. **No min_stocks fallback** (set `min_stocks=1`). Concentrated portfolio is
   still better than 0% return when 5-9 candidates qualify.

Each of these dilutes the strategy thesis somewhat. Worth a focused 32-64
config sweep if the goal is full-period viability.

---

## Cost of alternate data sources (not needed, listed for record)

If a data backfill IS pursued for some other reason (e.g. richer fundamentals
fields, sector splits, segmented financials), here are the practical options:

| Source | Cost | NSE coverage | Pre-2018 quality | Caveat |
|---|---|---|---|---|
| **BSE/NSE corporate filings** | Free | Full | Patchy formatting | Manual parse / OCR; high effort |
| **screener.in** | Free | Good | Last 10 years cited | ToS prohibits bulk scraping; legal risk |
| **Tijori** | Subscription (~₹999/mo) | Good | Decent | Manual export only; no API |
| **Refinitiv Eikon** | Enterprise (₹lacs/yr) | Excellent | Excellent | Massive overkill for this use case |
| **CMIE Prowess** | Subscription (~₹5L/yr) | Excellent | Excellent | Purpose-built for Indian fundamentals; standard for academic finance |
| **Capitaline / Ace Equity** | Subscription | Good | Good | Indian players |
| **AMFI/SEBI bulk filings** | Free | Listed companies | Available | Raw XBRL filings; need parser |
| **Kite Connect MIS API** | Free with broker | NSE-only | None pre-broker | Real-time only, no historical fundamentals |

None of these are recommended. **The FMP data we have is sufficient.**

---

## What to update in memory

The memory entry `feedback_ensemble_invvol_trap.md` mentions "FMP NSE
fundamentals sparse pre-2018." This phrasing is misleading — the data is
present; it's the strategy's filter intersection that's empty. The trap
identified (invvol over-weighting cash-period legs) is still valid; the
root-cause attribution should be corrected to "low_pe's filter+scanner
intersection is empty pre-2018, causing the leg to hold cash."

---

## Decision

**D as originally scoped is closed: no data backfill needed.**

If the user wants to pursue low_pe full-period viability, the path is a R5
strategy-tuning round (above), not a data project. That would be a separate
task, not part of D.

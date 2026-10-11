# "Won't most of these reach their 6-month high within a year?"

Swas's question, 2026-10-11, against a 13-name list of large caps.

**Short answer: yes, most do — and the basket still loses to the index.**

## The list, Friday 2026-10-09 closes
Mean gap to the 6-month high **+19.4%**, median +17.2%, range +5.5% to +31.8%. None is within 2% of its 6-month high. **NIFTYBEES is itself 10.5% below its
own 6-month high** — those highs were set May–August 2026 and the whole market has pulled
back since. This is a broad drawdown, not 13 idiosyncratic stories.

## Test
23,132 observations, 186 month-ends 2011–2026, 338 symbols. Universe = top 150 by
point-in-time trailing rupee traded value (bhavcopy, CA-invariant, survivorship-free).
Prices = `nse_charting_day`. Entry at month-end close, exit at the first close ≥ the
6-month high, else marked at 1 year. **Benchmark = NIFTYBEES over each trade's own holding
window**, not a fixed year — comparing a 160-day stock gain to a 365-day index gain would
understate the strategy.

## Per trade — Swas's intuition is correct
| gap to 6m high | n | **hit ≤1y** | med days | med return | mean return | med index | **med excess** | **mean excess** | beats | p10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2–10% | 6,840 | **85%** | 39 | +4.8% | +1.6% | +1.7% | +2.4% | −1.2% | 70% | −12.1% |
| **10–20% ← the list** | 5,878 | **67%** | 160 | +11.9% | +3.5% | +5.3% | **+4.9%** | **−2.3%** | 62% | −26.3% |
| 20–30% | 3,223 | **49%** | 323 | +20.0% | +4.3% | +7.2% | +3.2% | −2.9% | 54% | −31.0% |
| 30–50% | 3,013 | 33% | 364 | +4.0% | +4.5% | +8.6% | −4.9% | −4.2% | 45% | −35.2% |

With the market itself ≥7% off its 6-month high (today's regime, 10.5%), the 10–20% bucket
improves: **hit 71%**, median return +12.6%, median excess +4.5%, mean excess −1.5%.

**Note the median/mean split.** Median excess is POSITIVE through the 20–30% bucket. Mean
excess is NEGATIVE everywhere.

## The basket — 13 names, each 10–32% below its 6-month high, held 1 year
| | p10 | median | p90 | mean |
|---|---:|---:|---:|---:|
| basket, exits held in cash | −10.7% | **+3.7%** | +17.4% | +3.5% |
| basket, exits reinvested in NIFTYBEES | −12.0% | **+5.5%** | +36.0% | +9.0% |
| **NIFTYBEES, same year** | −5.8% | **+10.1%** | +31.2% | +11.8% |

- Beats the index in **20%** of start months (38% if exits are reinvested in the index).
- Loses money in **38%** of start months.
- Median **54% of the basket** hits its 6-month high within the year.
- On ₹42L: median **+₹1.5L** (cash) or **+₹2.3L** (reinvested) against the index's **+₹4.2L**.
- Restricting to market-drawdown starts like today does not rescue it: median excess −2.1%,
  beats 41%.

## Why a high hit rate still loses
1. **Capped upside, uncapped downside.** The rule sells every winner at the 6-month high
   (+11.9% median, gone in ~5 months) and holds every loser with no floor (p10 −26.3%).
2. **A basket gets the MEAN, not the median.** The median trade beats the index by ~4.9pp;
   the average trade loses by 2.3pp. Thirteen names average out to the mean.
3. **Correlation.** These names are down because the market is down. When they recover, the
   index recovers with them — you are buying a higher-beta version of the same rebound and
   paying single-stock risk for it.

## Caveat
`nse_charting_day` lacks delisted names, so the per-trade table is survivorship-biased in
the strategy's **favour**. The sister study quantifies that at 17.1% of universe slots.

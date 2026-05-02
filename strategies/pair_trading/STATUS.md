# Pair Trading & US Intraday — STATUS

**Created:** 2026-05-01
**Status:** US gap-fade validated (Sharpe 0.70); NSE pair trading dead; deployed as BrightFunded prop-firm system

---

## Summary of Work (2026-05-01)

### NSE Pair Trading (DEAD)

**Phase 1 — Cointegration Discovery:**
- 45 I(1) stocks from Nifty 50 (excluded 3 stationary: KOTAKBANK, BAJAJFINSV, ADANIENT)
- 990 pairs tested; 62 pass strict filter (p<0.05, half-life 1-30d, hurst<0.5, β-CV<0.30)
- Top pair: POWERGRID/BAJAJ-AUTO (p=0.0000, hl=10d, hurst=0.36)

**Phase 2 — Intraday Execution (DEAD):**
- 20 top pairs, 5-min bars, daily z-score signal, EOD square-off
- Result: CAGR −5.6%, MDD −20%, only 1 target hit out of 1902 trades
- Root cause: half-life 10-25 days — mean reversion doesn't manifest intraday

**Phase 3 — Multi-Day Holding (DEAD OOS):**
- Same pairs, hold up to 30 days
- In-sample 2022-2024: CAGR +1.5%, Sharpe 0.40 (2024 alone: +12%)
- OOS 2025: CAGR −6.6% — cointegration relationships broke out-of-sample
- Root cause: regime sensitivity + multiple-testing bias (picked top 20 from 990)

### US Intraday Gap-Fade (POSITIVE — deployed)

**Strategy:** Fade opening gaps on NAS100/QQQ, SMA20 regime filter
- Validated on 6 years QQQ (2020-2026), 1-min bars
- Entry: 09:35 ET, fade direction of gap ≥ 0.2%
- Exit: 70% gap fill target / 2× gap stop / 120min time / EOD

**Results (with SMA20 filter):**

| Period | CAGR | MDD | Sharpe | Calmar | WR |
|---|---:|---:|---:|---:|---:|
| Full 2020-2026 | +2.4% | −8.4% | 0.70 | 0.29 | 63% |
| OOS 2024-2026 | +5.6% | −5.6% | 1.01 | 1.00 | 64% |
| 2025 alone | +9.5% | ~−3% | ~1.5 | ~3.0 | 68% |

**Per-year:** 2020:+3.3% | 2021:+1.8% | 2022:−0.7% | 2023:+3.8% | 2024:−0.5% | 2025:+9.5%

**Deployed as BrightFunded prop-firm system:** see BRIGHTFUNDED_SYSTEM.md

### Also Tested (DEAD):

- EOD momentum on QQQ (last 30min carry): −1.8% CAGR
- Intraday RSI reversion on AAPL/MSFT/NVDA: −3.6% CAGR
- Gold (GCUSD) gap-fade: −10% CAGR (24h market, no real gaps)

---

## Key Learnings

1. **Gap-fade is equity-specific.** Works on US equity indices (overnight gap from news → fades at open). Does NOT work on 24h markets (gold, forex).

2. **NSE minute-bar tape has no exploitable intraday edge.** Tested breakout, mean-reversion, pair trading — all dead. Mean reversion exists at multi-day timescale but breaks OOS.

3. **SMA20 regime filter is mandatory.** Bear markets (2022) turn gap-fade from +3%/year to −13%/year. Filter removes ~35% of trades but eliminates the worst year entirely.

4. **Cointegration ≠ tradeable edge.** In-sample cointegration (p<0.05) inflated by multiple testing. Only ~1/3 of "significant" pairs hold OOS. Half-life 10-25d means intraday execution can't capture the reversion.

5. **US > NSE for intraday retail strategies.** Deeper liquidity, more documented patterns, tighter spreads, and gap-fade has real structural basis (overnight news processing).

---

## Files

| File | Purpose |
|---|---|
| `discovery.py` | Phase 1: Engle-Granger cointegration on Nifty 50 |
| `intraday_pair_prod.py` | Phase 2: intraday pair trading (5-min, z-score, EOD) |
| `multiday_pair_prod.py` | Phase 3: multi-day pair trading (daily z, 30d hold) |
| `us_gap_fade_prod.py` | US gap-fade on QQQ/NAS100 (validated) |
| `BRIGHTFUNDED_SYSTEM.md` | Complete prop-firm deployment guide |
| `STATUS.md` | This file |

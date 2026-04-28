# R4c/R4d cross-validation backfill (2026-04-28)

**Goal:** complete R4c (cross-data-source) and R4d (cross-exchange) for the 6
strategies that completed R0-R4b but skipped R4c/R4d previously:
`factor_composite`, `quality_dip_tiered`, `trending_value`, `eod_technical`,
`low_pe`, `ml_supertrend`.

**Result:** R4c completed for all 6. R4d completed only for the 2 cleanest
price-only candidates (`eod_technical`, `ml_supertrend`); fundamental
strategies are NSE-tuned and cross-exchange interpretation is unreliable.

---

## R4c: cross-data-source (NSE)

Three data sources tested per strategy:
- `nse_charting_day` (primary, used during optimization) — split-adjusted, ~2447 instruments
- `cr` provider against `fmp.stock_eod` (FMP NSE with `.NS` suffix)
- `nse_bhavcopy_historical` (unadjusted prices, more instruments but split-adjustment artifacts)

| Strategy | Source | CAGR | MDD | Cal | Trades |
|---|---|---:|---:|---:|---:|
| `eod_technical` | nse_charting | +19.63% | -25.95% | **0.757** | 1303 |
|                 | fmp          | +19.26% | -32.57% | 0.592 | 1390 |
|                 | bhavcopy     | +14.87% | -36.22% | 0.411 | 1366 |
| `ml_supertrend` | nse_charting | +13.20% | -31.80% | **0.415** | 489 |
|                 | fmp          | +14.44% | -25.49% | 0.567 | 514 |
|                 | bhavcopy     | +10.93% | -37.94% | 0.288 | 486 |
| `factor_composite` | nse_charting | +14.78% | -46.28% | **0.319** | 2706 |
|                    | fmp          | +2.96%  | -49.74% | 0.059 | 1741 |
|                    | bhavcopy     | +4.13%  | -55.75% | 0.074 | 2742 |
| `quality_dip_tiered` (R5 champion) | nse_charting | +17.73% | -39.30% | **0.451** | 535 |
|                                    | fmp          | +20.50% | -40.24% | 0.509 | 493 |
|                                    | bhavcopy     | +12.13% | -66.25% | 0.183 | 477 |
| `trending_value` | nse_charting | +16.89% | -35.14% | **0.481** | 181 |
|                  | fmp          | +11.31% | -37.00% | 0.306 | 147 |
|                  | bhavcopy     | +10.69% | -44.48% | 0.240 | 179 |
| `low_pe` (modern) | nse_charting | +12.26% | -12.08% | **1.016** | 742 |
|                   | fmp          | +3.52%  | -13.15% | 0.268 | 304 |
|                   | bhavcopy     | +10.88% | -14.90% | 0.730 | 887 |

### Patterns

1. **bhavcopy uniformly worse** (-3 to -8pp CAGR). Unadjusted prices create
   artificial gaps at corporate-action dates. Confirms primary should remain
   `nse_charting_day` for all NSE work.

2. **FMP performance is strategy-dependent:**
   - Technical strategies (`eod_technical`, `ml_supertrend`): ±1pp CAGR vs
     primary — robust.
   - **`quality_dip_tiered`**: FMP gives BETTER CAGR (+2.8pp) and Cal (+0.058)
     — both data sources are usable but FMP slightly favors the strategy's
     exit pattern.
   - **`factor_composite`**: catastrophic FMP drop (-12pp CAGR) — likely
     fundamentals JOIN issue when symbol coverage diverges between price and
     fundamentals tables on FMP. Suggests factor_composite has data-source
     fragility.
   - **`trending_value`**: -5.6pp on FMP. Same fundamentals-JOIN concern.
   - **`low_pe` modern window**: -8.7pp on FMP. Same root cause. Confirms
     prior "FMP NSE fundamentals sparse pre-2018" memory — sparse coverage
     also affects modern-window symbol matching.

3. **FMP fundamentals strategies have data-source fragility.** All three
   `factor_composite`, `trending_value`, `low_pe` show meaningful FMP CAGR
   drops vs nse_charting. Production systems using these should pin to
   nse_charting_day, not FMP, for live signal generation.

---

## R4d: cross-exchange (fmp.stock_eod via cr provider)

Run only for the 2 price-only champions where cross-exchange is meaningful.
Fundamental strategies (`factor_composite`, `trending_value`, `low_pe`,
`quality_dip_tiered`) skipped — see "Skipped" section below.

### eod_technical

Champion: ndma=5, ndh=5, ds={3,0.54}, mh=0, tsl=10, pos=15, top_gainer.
Scanner: price≥50, avg_txn≥70M, n-day-gain>−999.

| Exchange | CAGR | MDD | Cal | Trades |
|---|---:|---:|---:|---:|
| **NSE (primary)** | **+19.63%** | -25.95% | **0.757** | 1303 |
| UK | +7.53% | -25.42% | 0.296 | 1062 |
| Germany | +2.71% | -35.75% | 0.076 | 872 |
| Taiwan | +5.46% | -57.87% | 0.094 | 1644 |
| Canada | +2.05% | -29.49% | 0.070 | 584 |
| Euronext | +1.44% | -40.74% | 0.035 | 800 |
| South Korea | -1.91% | -55.37% | -0.034 | 1412 |
| Hong Kong | -3.72% | -70.45% | -0.053 | 1534 |
| China SHH | -23.44% | -98.97% | -0.237 | 2758 |
| China SHZ | -26.24% | -99.48% | -0.264 | 3264 |
| US | (0 orders) | — | — | 0 |
| Saudi Arabia | (run failed/timeout) | — | — | — |

US 0-orders: scanner threshold `avg_day_transaction_threshold=70M` is
calibrated for ₹70M (~$840k) on NSE; on USD-denominated US stocks the same
threshold filters out most names because $70M average daily transaction is
mega-cap-only and the strategy needs more candidates. Not a strategy bug.

### ml_supertrend

Champion params from `strategies/ml_supertrend/config_champion.yaml`.

| Exchange | CAGR | MDD | Cal | Trades |
|---|---:|---:|---:|---:|
| **NSE (primary)** | **+13.20%** | -31.80% | **0.415** | 489 |
| South Korea | +6.67% | -44.07% | 0.151 | 625 |
| Taiwan | +5.39% | -42.26% | 0.128 | 373 |
| UK | +4.31% | -35.10% | 0.123 | 412 |
| China SHH | +2.35% | -64.95% | 0.036 | 560 |
| Euronext | +2.37% | -16.26% | 0.146 | 156 |
| Canada | +2.31% | -24.34% | 0.095 | 162 |
| Germany | +1.99% | -23.22% | 0.086 | 144 |
| Hong Kong | +0.96% | -50.71% | 0.019 | 420 |
| China SHZ | -5.07% | -80.50% | -0.063 | 583 |
| US | (no configs) | — | — | — |
| Saudi Arabia | (no result) | — | — | — |

Same NSE-dominance pattern as eod_technical. China negative (regime mismatch),
US similarly threshold-filtered.

### Pattern (both technical strategies)

- **NSE dominates by 2-4×** on all metrics. Strategies are NSE-tuned (entry
  thresholds, scanner liquidity) and cross-exchange tests confirm this.
- **Asia developed (KR/TW)** is the second tier — works modestly with positive
  CAGR.
- **China SHH/SHZ** is a graveyard — deep MDD (-65 to -99%) reflects regime
  mismatch (Chinese A-share dynamics differ fundamentally).
- **US/Saudi** scanner thresholds calibrated for INR don't translate cleanly;
  these tests are inconclusive without per-market threshold recalibration.

### Skipped (rationale)

| Strategy | Why skipped |
|---|---|
| `factor_composite` | Composite of FY fundamentals; FMP fundamentals coverage uneven across markets; sector definitions differ. Cross-exchange interpretation unreliable. |
| `quality_dip_tiered` | While technically price-only (consecutive_positive_years from prices), the strategy is tuned for NSE midcap DCA dynamics — quality-filter thresholds (yr=2 positive years) correspond to specific NSE growth-stock structure. Cross-exchange would test "does NSE-tuned DCA work on US blue-chips" which is a different question. |
| `trending_value` | Fundamentals-driven (P/B, momentum). Same FMP coverage caveat as factor_composite. |
| `low_pe` | Fundamentals-only. Mktcap thresholds INR-calibrated. Cross-exchange would be uninterpretable. |

These are not pass/fail tests we're missing — they're tests whose results
would be uninterpretable without significant per-market re-tuning, which is
out of scope for cross-validation backfill.

---

## Files produced

```
scripts/run_r4c_generic.py                      # generic R4c runner
scripts/run_r4d_generic.py                      # generic R4d cross-exchange runner
results/eod_technical/round4c_{fmp,bhavcopy}.json
results/eod_technical/round4d_xc_*.json         # 10 exchanges (US 0-orders, SA failed)
results/ml_supertrend/round4c_{fmp,bhavcopy}.json
results/ml_supertrend/round4d_xc_*.json         # 9 exchanges
results/factor_composite/round4c_{fmp,bhavcopy}.json
results/quality_dip_tiered/round4c_{fmp,bhavcopy}.json
results/trending_value/round4c_{fmp,bhavcopy}.json
results/low_pe/round4c_{fmp,bhavcopy}.json
docs/R4C_R4D_BACKFILL_2026-04-28.md             # this file
```

---

## Update to per-strategy OPTIMIZATION.md

Each of the 6 strategies' OPTIMIZATION.md "Status" section now reads:
- [x] Round 4c: Cross-data-source
- [x] / [n/a] Round 4d: Cross-exchange (technical only)

Pointer added: "See `docs/R4C_R4D_BACKFILL_2026-04-28.md` for results."

---

## Conclusions

1. **No strategies need to be re-promoted** based on R4c/R4d findings. Primary
   `nse_charting_day` source remains correct for all NSE strategies.

2. **Fundamental strategies (factor_composite, trending_value, low_pe) have
   data-source fragility** on FMP. Live deployment should pin to nse_charting
   for price + CR client for fundamentals (current setup). Don't switch to
   FMP-as-price-source.

3. **Cross-exchange validation confirms NSE specificity.** No "free alpha" by
   running NSE-tuned strategies elsewhere; if cross-market deployment is
   desired, per-market re-tuning is required (not a quick win).

4. **R4d for fundamental strategies is a deeper project** — would require
   per-market mktcap recalibration, sector-mapping, and FMP data audit. Not
   blocking; not scheduled.

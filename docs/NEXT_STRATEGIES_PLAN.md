# Next Strategies Plan

Created: 2026-03-24
Updated: 2026-03-25

## All Results (Updated 2026-03-25)

| Strategy | Best Calmar | CAGR | MDD | Market | Trades | Status |
|----------|-------------|------|-----|--------|--------|--------|
| **Momentum-dip + D/E<1.0 (5+DE)** | **1.01** | +23.7% | -23.3% | NSE | 244 | **Champion** |
| Momentum-dip (5) | 0.90 | +26.3% | -29.1% | NSE | 236 | Done |
| Momentum-dip + vol-adj exits (5+6) | 0.87 | +31.7% | -36.5% | NSE | 92 | Done -- vol-adj hurts momentum-dip |
| Vol-adjusted exits (6) | 0.70 | +25.4% | -36.5% | NSE | 91 | Done -- helps quality dip-buy only |
| Quality dip-buy + fundamentals | 0.64 | +23.8% | -37.0% | NSE | ~200 | Beaten |
| Forced-selling dip (1a) | 0.64 | +21.4% | -33.6% | NSE | ~180 | Done |
| Quality dip-buy | 0.58 | +22.6% | -39.1% | US | ~200 | Done |
| Intraday dip-buy (3e, 8yr) | 0.44 | +14.0% | -31.7% | NSE | 90 | Done -- open best |
| Earnings surprise (2a) | 0.44 | +5.7% | -12.9% | NSE | 30 | Done -- too sparse |
| Forced-selling dip (1a) | 0.39 | +5.6% | -14.1% | US | 66 | Done |
| SPY vs EWJ dual-z MOC | 0.39 | +9.6% | -25.0% | US | - | Done |
| Momentum-dip (5) | 0.37 | +17.0% | -45.9% | US | 167 | Done |
| NIFTYBEES 3d-high + TSL | 0.32 | +13.3% | -41.0% | NSE | - | Done |
| Earnings volume confirm (2c) | 0.26 | +3.0% | -11.5% | NSE | 11 | Dead -- 11 trades in 16yr |
| Earnings volume confirm (2c) | 0.24 | +5.7% | -23.7% | US | 117 | Marginal |
| Earnings surprise (2a) | 0.23 | +10.6% | -46.4% | US | 98 | Marginal |
| ORB corrected (3b) | <0 | -35% | - | NSE | - | Dead -- all configs negative |
| Tax-loss calendar (1b) | 0.09 | +5.8% | -65.2% | US | 244 | Dead |
| Tax-loss calendar (1c) | 0.08 | +2.8% | -34.9% | NSE | 146 | Dead |

---

## Key Learnings (from P0 + P1)

1. **Quality dip-buy + fundamentals is the core alpha source.** ROE>15% + PE<25 filter is the single biggest improvement (Calmar 0.33 -> 0.64).
2. **Momentum filter (63d) is the second biggest improvement.** Top 30% momentum + quality intersection: Calmar 0.64 -> 0.90. Only 33% overlap with quality-only, catches different stocks.
3. **D/E<1.0 filter is the third biggest improvement.** Calmar 0.90 -> 1.01. MDD drops from -29.1% to -23.3% by excluding leveraged companies.
4. **Vol-adjusted exits help quality dip-buy but HURT momentum-dip.** On quality dip-buy: Calmar 0.54 -> 0.70 (+30%). On momentum-dip: Calmar 0.90 -> 0.87 (-3%). Momentum-selected stocks are already calibrated for 10% TSL.
5. **Short momentum (63d) dominates on NSE.** 3-month momentum rank is most predictive. 6-month and 12-month are worse.
6. **10 positions consistently beats 5.** Diversification helps across all configurations.
7. **Intraday execution doesn't help.** Open is optimal. VWAP/near-low/midpoint all underperform.
8. **Calendar strategies don't work.** Fixed hold periods expose you to crash drawdowns.
9. **Earnings signals are too sparse on NSE.** Only 1,022 symbols have earnings data, combined with quality + fundamental filters = ~10-30 trades in 16 years.
10. **ORB is dead after bias correction.** The original Calmar 5.36 was entirely from same-bar entry bias.
11. **Same-bar entry bias inflates returns by 15-20pp CAGR.** All strategies must use MOC (next-day open).

---

## Path to 30% CAGR

The best risk-adjusted result is Calmar 1.01 (+23.7% CAGR, -23.3% MDD). The highest raw CAGR is +31.7% (vol-adjusted, 126d momentum, 5 positions) but with -36.5% MDD. Closing the gap to 30% CAGR with sub-25% MDD requires structural changes.

### Strategy 4: Combined Portfolio Allocator (P0 -- next)

**Thesis**: Not "reduce cash drag" (champion is 87% deployed), but "diversify alpha sources to reduce MDD." Quality dip-buy and momentum-dip catch different stocks (33% overlap). Running both on shared capital diversifies drawdown exposure.

**Implementation**: Single script with shared capital pool. On each trading day:
1. Check both signal sources (quality dip-buy, momentum-dip)
2. Rank entries by expected alpha (dip magnitude, momentum rank)
3. Allocate from shared capital to top N signals regardless of source
4. Unified exit logic (10% TSL + 504d max hold)

**Key question**: Does combining signal sources reduce MDD below -23.3% (current champion)?

**Sweep**: allocation weights, max positions per strategy, shared vs split capital

### Strategy 5: Momentum-Dip -- DONE

**Result**: Calmar 0.90 (+26.3% CAGR, -29.1% MDD) without D/E. **Calmar 1.01 (+23.7%, -23.3%) with D/E<1.0.**

**Best config**: 63d momentum, top 30% percentile, 5% dip, 10 positions, ROE>15%, D/E<1.0, PE<25, fixed 10% TSL.

**Files**: `scripts/momentum_dip_buy.py`, `scripts/momentum_dip_vol_exits.py`

### Strategy 6: Vol-Adjusted Exits -- DONE (mixed results)

**Result on quality dip-buy**: Calmar 0.70 (+25.4%, -36.5%) vs fixed baseline 0.54. Genuine improvement for quality-only signal.

**Result on momentum-dip**: Vol-adjusted HURTS. Best vol-adj Calmar 0.87 vs fixed 0.90-1.01. The 10% TSL is already well-calibrated for momentum-selected stocks.

**Conclusion**: Vol-adjusted exits are useful for quality dip-buy but should NOT be applied to momentum-dip.

**Files**: `scripts/vol_adjusted_exits.py`

---

## Remaining P2/P3 Strategies (Unchanged)

| Priority | Strategy | Status | Notes |
|----------|----------|--------|-------|
| P2 | 3d. Volume-weighted close signal | Not started | Pre-aggregate minute VWAP to daily signal |
| P2 | 1e. Volume anomaly no-news | Not started | Exclude earnings windows from volume spikes |
| P2 | 2d. Consecutive beats + dip | Not started | Build per-symbol earnings chain |
| P3 | 3a. Closing auction accumulation | Not started | Needs minute-level VWAP computation |
| P3 | 1d. Index reconstitution (US) | Not started | S&P 500 only, small sample |
| P3 | 3b/3c. ORB + VWAP intraday | Dead | ORB killed by bias correction |

---

## Updated Priority Order

| Priority | Strategy | Est. Effort | Expected Impact | Rationale |
|----------|----------|-------------|-----------------|-----------|
| ~~P0~~ | ~~5. Momentum-dip~~ | ~~2-3 hrs~~ | ~~High~~ | **DONE** -- Calmar 1.01 with D/E<1.0 |
| ~~P1~~ | ~~6. Vol-adjusted exits~~ | ~~1-2 hrs~~ | ~~Medium~~ | **DONE** -- helps quality dip-buy (0.70), hurts momentum-dip |
| **P0** | 4. Combined portfolio allocator | 3-4 hrs | Medium | Diversify alpha sources, might reduce MDD below -23.3% |
| P2 | 3d. Volume-weighted close | 2 hrs | Medium | New daily signal from minute data |
| P2 | 1e. Volume anomaly no-news | 2 hrs | Medium | Better forced-selling isolation |
| P3 | Backlog items | Varies | Uncertain | See backlog section below |

---

## Execution Rules (Non-Negotiable)

All strategies must follow:
- **MOC execution**: signal at close, execute at next open (or documented intraday model)
- **Real charges**: `engine/charges.py` (NSE: STT 0.1% delivery; US: SEC + FINRA)
- **5 bps slippage minimum**
- **Integer quantities**
- **Standalone script pattern**: `BacktestResult` / `SweepResult` / `run_remote.py`
- **NSE native data preferred** over FMP for NSE backtests

---

## Backlog

Ideas worth exploring if the above strategies pan out, or if we need new directions:

### Cross-Market Signals
- **Cross-market dip arbitrage**: when NIFTYBEES drops but SPY doesn't (or vice versa), buy the dipping market's quality stocks. Exploits local liquidity events.
- **Global momentum rotation**: rank NSE vs US vs other markets by 6-month momentum, allocate to the strongest. Use country ETFs as proxies.
- **Currency-adjusted pair trading**: SPY vs NIFTYBEES adjusted for USD/INR. The alpha_variations.py work found currency mean-reversion, not equity value -- could isolate the currency component.

### Alternative Factor Combinations
- **Quality + momentum + value**: triple-factor screen. We have quality (consecutive returns), value (PE, ROE), could add momentum (6-12 month return). Academic literature shows combining 3+ factors is more robust than any single factor.
- **Gross profitability + dip**: Novy-Marx (2013) showed gross profit / assets is the cleanest profitability measure. FMP has `income_statement.grossProfit` and `balance_sheet.totalAssets`. Could replace ROE in the quality gate.
- **Low volatility + dip**: stocks with low realized volatility that dip tend to recover faster. Volatility can be computed from existing price data.
- **Dividend aristocrats + dip**: stocks with 5+ years of consecutive dividend increases that dip. Dividend data available in `fmp.financial_ratios.dividendYieldPercentage`.

### Machine Learning
- **Gradient boosting on feature set**: we now have ~10 features (quality filter, dip %, ROE, PE, DE, sector, volume, regime, RSI, earnings surprise). Train a classifier to predict which dips recover vs which keep falling. Use 2010-2020 as training, 2020-2025 as test.
- **Feature importance analysis**: even without deploying an ML model, running feature importance (random forest) on the historical entry signals would tell us which factors actually drive recovery probability.

### Structural/Mechanical
- **Options-implied dip-buy**: when put/call ratio spikes on a quality stock, it implies fear. Buy the stock (not options) and capture the reversion. Requires options data we don't have.
- **ETF creation/redemption pressure**: when an ETF's price deviates from NAV, authorized participants create/redeem shares, creating pressure on underlying stocks. Detectable from ETF premium/discount data.
- **Mutual fund quarter-end window dressing**: funds buy winners and sell losers before quarter-end reporting. Buy the losers that get dumped in the last week of each quarter. Calendar-based, similar to 1b/1c.

### Data Expansion
- **Add more exchanges**: JNB (South Africa), KSC (Korea), SES (Singapore) -- these have good FMP data. Quality dip-buy could work on any market with quality stocks.
- **Alternative data**: if we ever get news sentiment, social media, or satellite data, these could add signal to the forced-selling detector (confirming no fundamental deterioration).

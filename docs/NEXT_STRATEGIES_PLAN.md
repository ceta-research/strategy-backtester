# Next Strategies Plan

Created: 2026-03-24
Updated: 2026-03-25

## All Results (Updated 2026-03-25)

| Strategy | Best Calmar | CAGR | MDD | Market | Trades | Status |
|----------|-------------|------|-----|--------|--------|--------|
| Quality dip-buy + fundamentals | **0.64** | +23.8% | -37.0% | NSE | ~200 | **Champion** |
| Forced-selling dip (1a) | **0.64** | +21.4% | -33.6% | NSE | ~180 | Done |
| Quality dip-buy | 0.58 | +22.6% | -39.1% | US | ~200 | Done |
| Intraday dip-buy (3e, 8yr) | 0.44 | +14.0% | -31.7% | NSE | 90 | Done -- open best |
| Earnings surprise (2a) | 0.44 | +5.7% | -12.9% | NSE | 30 | Done -- too sparse |
| Forced-selling dip (1a) | 0.39 | +5.6% | -14.1% | US | 66 | Done |
| SPY vs EWJ dual-z MOC | 0.39 | +9.6% | -25.0% | US | - | Done |
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
2. **Intraday execution doesn't help.** Open is optimal. VWAP/near-low/midpoint all underperform.
3. **Calendar strategies don't work.** Fixed hold periods expose you to crash drawdowns.
4. **Earnings signals are too sparse on NSE.** Only 1,022 symbols have earnings data, combined with quality + fundamental filters = ~10-30 trades in 16 years.
5. **Volume confirmation helps risk but kills signal count.** Better MDD but far fewer trades.
6. **ORB is dead after bias correction.** The original Calmar 5.36 was entirely from same-bar entry bias.
7. **Same-bar entry bias inflates returns by 15-20pp CAGR.** All strategies must use MOC (next-day open).

---

## Path to 30% CAGR

The best honest result is 23.8% CAGR. Closing the gap to 30% requires structural changes, not parameter tuning.

### Strategy 4: Combined Portfolio Allocator (NEW -- P0)

**Thesis**: Our strategies fire at different times. Quality dip-buy capital sits in cash 40-60% of the time. Running multiple uncorrelated strategies on a shared capital pool reduces cash drag and diversifies alpha sources.

**Implementation**: Single script with shared capital pool. On each trading day:
1. Check all signal sources (quality dip-buy, forced-selling, momentum-dip)
2. Rank all pending entries by expected alpha (dip magnitude, signal strength)
3. Allocate from shared capital to top N signals regardless of strategy source
4. Unified exit logic (TSL + max hold)

**Expected improvement**: If idle cash currently earns 0% and could earn ~12% (benchmark), the combined CAGR could be 5-8pp higher. The always-invested adjustment already hints at this.

**Sweep**: allocation weights, max positions per strategy, shared vs split capital

### Strategy 5: Momentum-Dip (NEW -- P0)

**Thesis**: "Buy weakness in strong stocks." Stocks in the top 20% of 6-12 month momentum that then dip 5%+ have the strongest mean-reversion. This catches different stocks than quality dip-buy (which uses 2yr trailing returns, not recent momentum).

**Signal**:
1. Compute 6-month momentum rank across universe (return over trailing 126 days)
2. Filter to top 20% by momentum (strong uptrend)
3. Quality gate: ROE>15%, PE<25
4. Dip: 5%+ from rolling peak
5. Entry: next-day open

**Data**: Same as quality dip-buy -- no new data sources needed.

**Sweep**:
```python
product(
    [63, 126, 252],   # momentum_lookback (3mo, 6mo, 12mo)
    [0.20, 0.30],     # momentum_percentile (top 20%, top 30%)
    [5, 7],           # dip_threshold_pct
    [5, 10],          # max_positions
)
```

### Strategy 6: Volatility-Adjusted Exits (NEW -- P1)

**Thesis**: The 10% TSL is a one-size-fits-all exit. Low-volatility stocks should have tighter stops (they rarely dip 10%), while high-volatility stocks need wider stops (they dip 10% regularly without it meaning anything). Calibrating stops to realized volatility should let winners run longer and cut losers faster.

**Implementation**: Replace fixed TSL with `tsl_pct = k * realized_vol(60d)` where k is a sweep parameter.

**Sweep**: k = [1.0, 1.5, 2.0, 2.5] (so a stock with 20% annualized vol gets 3-5% TSL, while a stock with 40% vol gets 6-10% TSL)

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
| **P0** | 5. Momentum-dip | 2-3 hrs | High | New signal source, catches different stocks than quality dip-buy |
| **P0** | 4. Combined portfolio allocator | 3-4 hrs | High | Reduces cash drag, combines uncorrelated alpha |
| P1 | 6. Volatility-adjusted exits | 1-2 hrs | Medium | Better exit calibration, lets winners run |
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

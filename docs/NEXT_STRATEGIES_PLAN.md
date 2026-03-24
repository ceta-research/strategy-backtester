# Next Strategies Plan

Created: 2026-03-24
Context: Quality dip-buy experiments complete. Fundamental overlay (ROE>15% + PE<25) is the breakthrough -- Calmar 0.64 on NSE, 0.58 on US. This plan covers 3 new strategy families plus a backlog.

## Results So Far (Baseline)

| Strategy | Best Calmar | CAGR | MDD | Market |
|----------|-------------|------|-----|--------|
| Quality dip-buy + fundamentals | 0.64 | +23.8% | -37.0% | NSE |
| Quality dip-buy (US) | 0.58 | +22.6% | -39.1% | US |
| Quality dip-buy (baseline) | 0.33 | +20.0% | -60.1% | NSE |
| NIFTYBEES 3d-high + TSL | 0.32 | +13.3% | -41.0% | NSE |
| SPY vs EWJ dual-z MOC | 0.39 | +9.6% | -25.0% | US |

---

## Strategy 1: Forced-Selling Detection

### Thesis
The biggest mispricings happen when fundamentally strong stocks drop due to structural selling (index rebalancing, fund redemptions, tax-loss harvesting) rather than deteriorating fundamentals. These sellers MUST sell regardless of price, creating temporary dislocations that reliably revert.

### Data Available
- `nse.nse_charting_day` / `fmp.stock_eod`: daily OHLCV with volume
- `fmp.financial_ratios`: P/E, debt/equity, profitability (FY, with dateEpoch)
- `fmp.profile`: sector, industry
- `fmp.sp500_constituent`: S&P 500 membership + `dateFirstAdded` (US only)
- No Nifty 50 constituent history available

### Sub-Strategies to Test

**1a. Idiosyncratic Dip + Volume Spike (NSE + US)**
- Signal: quality stock drops 5%+ from peak BUT its sector average doesn't drop
- Confirmation: volume on the dip day is >2x 20-day average (abnormal selling pressure)
- Entry: next-day open (MOC)
- Exit: peak recovery + TSL (same as quality dip-buy)
- Why it works: sector-neutral dip isolates stock-specific forced selling from market risk
- Sweep: dip threshold [5, 7, 10%], volume multiplier [1.5, 2.0, 3.0], sector lookback [5, 20 days]
- Data: sector from `fmp.profile`, compute sector daily return as average of sector peers

**1b. Tax-Loss Harvesting Calendar (US)**
- Signal: quality stock drops 10%+ during Oct-Dec (tax-loss selling season)
- Entry: buy in late December / early January
- Exit: hold 60-90 days (January effect recovery)
- Sweep: entry window [Dec 15-31, Dec 1-31], hold period [30, 60, 90 days], dip threshold [10, 15, 20%]
- Data: `fmp.stock_eod` (20 years US data), calendar filter only

**1c. Tax-Loss Harvesting Calendar (NSE)**
- Same thesis but NSE fiscal year ends March 31
- Signal: quality stock drops 10%+ during Jan-Mar
- Entry: buy in late March
- Exit: hold 60-90 days (April recovery)
- Data: `nse.nse_charting_day` (15 years)

**1d. Index Reconstitution Selling (US)**
- Signal: stock removed from S&P 500 (forced selling by index funds)
- Use `fmp.sp500_constituent.dateFirstAdded` to identify additions (removals = stocks no longer in the table)
- Entry: buy 5-10 days after removal announcement
- Exit: 60-90 day hold or peak recovery
- Limitation: only S&P 500, no Nifty constituent history
- Data: cross-reference current vs historical constituent lists

**1e. Volume Anomaly Without News (NSE + US)**
- Signal: volume > 3x average on a down day, but no earnings surprise within 30 days
- This isolates forced liquidations from fundamental-driven selling
- Combine with quality filter (ROE>15%, PE<25 from fundamental overlay)
- Sweep: volume threshold [2x, 3x, 5x], earnings exclusion window [15, 30, 60 days]

### Implementation Order
1a first (builds directly on quality dip-buy lib, adds sector-relative and volume filters).
1b/1c next (calendar-based, simple to implement).
1d last (needs S&P constituent tracking logic).

---

## Strategy 2: Earnings Surprise + Dip

### Thesis
Post-earnings announcement drift is one of the most robust anomalies in finance. The unexploited variant: stocks that BEAT earnings and then dip within 2-4 weeks are the highest-conviction dip-buys. The market sometimes sells winners on "sell the news" or sector rotation, creating a window where a fundamentally improving stock is temporarily cheap.

### Data Available
- `fmp.earnings_surprises`: 1,022 NSE symbols (13.8K rows), 12,796 US symbols (489K rows), 1996-2026
  - Columns: symbol, dateEpoch, epsActual, epsEstimated
  - No exchange column -- JOIN with `fmp.profile` for exchange filtering
  - Has duplicates -- deduplicate with ROW_NUMBER()
- `nse.nse_charting_day` / `fmp.stock_eod`: price data post-earnings
- `fmp.financial_ratios`: quality gate

### Sub-Strategies to Test

**2a. Positive Surprise + Post-Earnings Dip (NSE + US)**
- Filter: `epsActual > epsEstimated * 1.05` (5%+ earnings beat)
- Signal: price drops 5%+ from post-earnings high within 20 trading days
- Entry: next-day open after dip threshold hit
- Exit: peak recovery + TSL
- Quality gate: ROE>15%, PE<25 (proven from fundamental overlay)
- Sweep: surprise threshold [5, 10, 20%], dip threshold [5, 7, 10%], post-earnings window [10, 20, 30 days], max positions [5, 10]

**2b. Negative Surprise Reversal (contrarian)**
- Filter: `epsActual < epsEstimated * 0.90` (10%+ earnings miss)
- Hypothesis: market overreacts to earnings misses in quality stocks
- Signal: buy 5-10 days after earnings miss (let initial selling exhaust)
- Quality gate: still profitable (net income > 0), ROE > 10%
- Exit: 60-day hold or TSL
- Higher risk, potentially higher reward
- Sweep: miss threshold [10, 20, 30%], entry delay [5, 10, 20 days], hold [30, 60, 90 days]

**2c. Earnings Beat + Volume Confirmation**
- Combine 2a with volume signal: only buy if post-earnings dip has LOW volume (sellers exhausted) while the earnings beat day had HIGH volume (genuine buying interest)
- Volume ratio: dip_volume < 0.5 * earnings_day_volume
- This filters real reversals from continued deterioration

**2d. Consecutive Earnings Beats + Dip**
- Filter: 2+ consecutive quarters of earnings beats (epsActual > epsEstimated)
- Signal: dip after the latest beat
- Thesis: consistent beaters that dip are the highest quality -- the market is wrong
- Requires building per-symbol earnings history chain

**2e. Earnings Surprise Magnitude Ranking**
- Instead of binary beat/miss, rank stocks by surprise magnitude
- Top decile of positive surprises that then dip = strongest signal
- Portfolio: top 5 by surprise magnitude each quarter
- Rebalance quarterly around earnings season

### Implementation Order
2a first (core thesis, straightforward). 2c next (adds volume confirmation). 2d and 2e are refinements if 2a works.

---

## Strategy 3: Intraday Microstructure Signals

### Thesis
Institutional order flow creates predictable intraday patterns. With 8 years of NSE minute data (2015-2022 via `nse.nse_charting_minute`) and 4 years of FMP minute data (2022-2026), we can detect accumulation/distribution patterns that predict next-day returns.

### Data Available
- `nse.nse_charting_minute`: 2,625 symbols, 2015-02 to 2022-10 + 2026-02 to 2026-03
  - Columns: symbol, date_epoch, open, close, volume
  - Full trading day coverage (09:15-15:30 IST labeled as UTC)
  - Gap: 2022-10 to 2026-02 (3.3 years missing)
- `fmp.stock_prices_minute`: 2,666 NSE symbols (via exchange='NSE'), 2022-2026
  - Fills the NSE native gap partially
  - Also has US data (NASDAQ/NYSE) from 2020
- `nse.nse_charting_day`: EOD data for signal confirmation

### Sub-Strategies to Test

**3a. Closing Auction Accumulation (NSE)**
- Signal: VWAP(last 30 min, 15:00-15:30) > VWAP(full day) by 0.3%+
- Thesis: institutions buy in closing session to match benchmark closing price
- Entry: next-day open
- Exit: sell at next-day close (1-day hold) or 2-day hold
- Quality gate: liquid stocks only (turnover > 7Cr)
- Sweep: VWAP premium [0.2, 0.3, 0.5%], hold period [1, 2, 3 days], volume filter [1x, 1.5x avg]
- Period: 2015-2022 (8 years, NSE native)

**3b. Opening Range Breakout + Quality Filter (NSE)**
- Compute opening range: high/low of first 15/30 minutes (09:15-09:45)
- Signal: price breaks above OR high on quality stock
- Entry: at breakout price (intraday)
- Exit: end of day or 1% stop-loss
- We already have ORB in the intraday pipeline (`intraday_sql_builder.py`) -- port to standalone with quality filter
- Sweep: ORB window [15, 30 min], direction [long only, both], stop [0.5, 1, 2%]

**3c. VWAP Reversion on Quality Stocks (NSE)**
- Signal: quality stock opens > 1% above prior close but drops below VWAP by noon
- Thesis: gap-up that fails = distribution, but on quality stocks it reverts back to VWAP
- Entry: buy when price crosses back above VWAP (intraday)
- Exit: end of day
- Already have VWAP MR in intraday pipeline -- adapt with quality filter

**3d. Volume-Weighted Close Predictor (NSE + US)**
- Signal: compute daily `close_vs_vwap = (close - VWAP) / VWAP`
- Positive close_vs_vwap = net buying pressure (close > average transaction price)
- Negative = net selling pressure
- Use as a daily signal: buy next-day open when close_vs_vwap < -0.5% on a quality stock (selling exhaustion)
- This converts minute data into a daily signal -- can be pre-aggregated in SQL
- Sweep: threshold [-0.3, -0.5, -1.0%], hold [1, 3, 5 days], with/without quality filter

**3e. Intraday Quality Dip-Buy (hybrid EOD signal + intraday execution)**
- Re-run our quality dip-buy but on NSE native minute data (2015-2022) instead of FMP (2022-2026)
- Compare execution models: open vs VWAP vs near-low
- 8 years of data covering 2018 correction + 2020 COVID crash
- This validates or kills the Calmar 0.83 result from the FMP 3-year run

### Implementation Order
3e first (re-run existing strategy on better data -- quick validation).
3d next (pre-aggregated daily signal from minute data -- lightweight).
3a then (closing auction -- needs minute-level processing).
3b/3c last (full intraday strategies -- most complex).

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

## Priority Order

| Priority | Strategy | Est. Effort | Expected Impact | Rationale |
|----------|----------|-------------|-----------------|-----------|
| P0 | 1a. Idiosyncratic dip + volume | 2-3 hrs | High | Builds directly on proven quality dip-buy, adds sector-relative signal |
| P0 | 2a. Positive surprise + dip | 2-3 hrs | High | New data source (earnings), strong academic backing |
| P1 | 3e. Intraday dip-buy on 8yr NSE data | 1-2 hrs | Medium | Validates/kills the Calmar 0.83 result |
| P1 | 1b/1c. Tax-loss calendar | 1-2 hrs | Medium | Simple calendar filter, works on both markets |
| P1 | 2c. Earnings beat + volume | 1 hr | Medium | Refinement of 2a with volume confirmation |
| P2 | 3d. Volume-weighted close signal | 2 hrs | Medium | New daily signal from minute data |
| P2 | 1e. Volume anomaly no-news | 2 hrs | Medium | Filters forced liquidation from fundamentals |
| P2 | 2d. Consecutive beats + dip | 1 hr | Medium | Refinement of 2a |
| P3 | 3a. Closing auction accumulation | 3 hrs | Uncertain | Microstructure, needs careful timestamp handling |
| P3 | 1d. Index reconstitution (US) | 2 hrs | Uncertain | Limited to S&P 500, small sample |
| P3 | 3b/3c. ORB + VWAP intraday | 3 hrs | Uncertain | Already tested in pipeline, diminishing returns |

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

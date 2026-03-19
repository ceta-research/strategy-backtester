# Advanced/ML-Based Approaches to Systematic Stock Trading
## Comprehensive Research Survey (March 2026)

Focus: Techniques that work with EOD (end-of-day) data, with honest assessments of out-of-sample viability.

---

## Table of Contents

1. [Machine Learning That Actually Works](#1-machine-learning-that-actually-works)
2. [Feature Engineering for Stock Prediction](#2-feature-engineering-for-stock-prediction)
3. [Deep Learning Approaches](#3-deep-learning-approaches)
4. [Alternative Data Signals](#4-alternative-data-signals)
5. [Cross-Sectional Models](#5-cross-sectional-models)
6. [Volatility and Options-Informed Equity Trading](#6-volatility-and-options-informed-equity-trading)
7. [Walk-Forward and Robust Backtesting](#7-walk-forward-and-robust-backtesting)
8. [Open-Source Quant Frameworks](#8-open-source-quant-frameworks)
9. [Practical Recommendations](#9-practical-recommendations)

---

## 1. Machine Learning That Actually Works

### 1.1 Gradient Boosting (XGBoost / LightGBM) -- THE MOST VALIDATED APPROACH

**What was done:** Cross-sectional stock return prediction using gradient boosting models trained on fundamental + technical features, predicting next-month returns, then forming long/short or long-only portfolios rebalanced monthly.

**Data:** EOD prices, fundamental ratios, technical indicators. Typically 100-900 features covering momentum, value, quality, volatility, liquidity.

**Reported performance (multiple studies):**

| Study / Market | Model | Monthly Return | Annualized Sharpe | Notes |
|---|---|---|---|---|
| Chinese A-shares (long-only) | LightGBM | 2.54% | 1.34 | Alpha158 feature set |
| Chinese A-shares (long-short) | LightGBM | 2.63% | 1.77 | Same dataset |
| Chinese A-shares (long-only) | XGBoost | 2.65% | 1.35 | |
| Chinese A-shares (long-short) | XGBoost | 2.73% | 1.76 | |
| Thailand market | XGBoost | - | - | 48.19% annual return vs 5.28% index |
| European stocks (cross-section) | GBRT | +0.6%/mo alpha | - | Gu, Kelly, Xiu methodology |
| CSI300 (Qlib benchmark) | LightGBM | - | IR: 1.02 | 9.01% annualized excess return |
| CSI300 (Qlib benchmark) | DoubleEnsemble | - | IR: 1.34 | 11.58% annualized excess return |

**Honest assessment:** Gradient boosting is the most consistently validated ML approach for cross-sectional stock selection. LightGBM and XGBoost dominate academic benchmarks. However:
- Most impressive results come from Chinese markets, which have more retail participation and potentially more alpha.
- S&P 500 results are far more modest. One Bocconi study found Sharpe ratios did not substantially improve over market benchmarks.
- Transaction costs are frequently excluded. Monthly rebalancing of 50-100 stocks has meaningful costs.
- Results degrade out of sample as more participants adopt the same signals.

**Verdict: MOST LIKELY TO WORK.** Start here. Use walk-forward validation, include transaction costs, and expect Sharpe of 0.5-1.0 in developed markets (not the 1.5+ reported in Chinese markets).

### 1.2 Random Forest

**What was done:** Stock selection using random forest classifiers/regressors on fundamental + technical features.

**Reported performance:**
- Chinese market: Sharpe ratios of 2.75 (multi-factor space) and 5.0 (momentum space) over 5 out-of-sample years.
- Automated stock picking (Netherlands): Machine learning together with careful feature engineering predicted out-of-sample performance with Sharpe of 1.2.
- Welch & Goyal (2008) cautionary result: The vast majority of suggested prediction models perform poorly out of sample.

**Honest assessment:** Random forests work but are generally outperformed by gradient boosting on tabular financial data. Their main advantage is resistance to overfitting through bagging. The Chinese market Sharpe of 5.0 is almost certainly not reproducible in developed markets. Expect 20-50% lower Sharpe than gradient boosting equivalents.

**Verdict: WORKS, but use gradient boosting instead.** Random forest is useful as a baseline or for feature importance analysis.

---

## 2. Feature Engineering for Stock Prediction

### 2.1 The Canonical Feature Set (Gu, Kelly, Xiu 2020)

The landmark paper "Empirical Asset Pricing via Machine Learning" (Review of Financial Studies, 2020) tested ML models on 94 stock-level characteristics covering:

**Most predictive features (all models agree):**

1. **Momentum variants** (most important overall)
   - Short-term reversal (1-month)
   - Medium-term momentum (2-12 months)
   - Industry momentum
   - Earnings momentum / SUE (standardized unexpected earnings)

2. **Liquidity variables**
   - Market capitalization
   - Dollar trading volume
   - Bid-ask spread
   - Turnover

3. **Volatility variables**
   - Return volatility
   - Idiosyncratic volatility
   - Market beta and beta-squared

**Key finding:** Trees and neural networks trace their gains to capturing non-linear interactions between these features that linear models miss. For example, momentum interacts with volatility and liquidity in ways a linear model cannot capture.

### 2.2 Technical Indicators That Add Predictive Value

From feature importance studies across multiple ML models:

| Feature Category | Specific Indicators | Importance Rank |
|---|---|---|
| Momentum | Stochastic oscillator, CCI, ROC | Highest |
| Volatility | Bollinger Band width, ATR, historical vol | High |
| Trend | Moving averages (5/10/20/50/200), MACD | Medium-High |
| Mean reversion | RSI (2-day, 3-day, 14-day) | Medium |
| Volume | OBV, volume ratio, dollar volume | Medium |
| Price patterns | Squeeze Pro, Ichimoku, PPO | Lower |

### 2.3 Fundamental Features

| Feature | Predictive For | Time Horizon |
|---|---|---|
| Earnings surprises (SUE) | Post-earnings drift | 1-3 months |
| Revenue surprises | Drift (additive to SUE) | 1-3 months |
| Book-to-market | Value premium | 6-12 months |
| Profitability (ROE, gross profit/assets) | Quality premium | 6-12 months |
| Asset growth | Investment premium | 6-12 months |
| Accruals | Short-side alpha | 6-12 months |
| Short interest | Informed bearishness | 1-6 months |

### 2.4 Feature Engineering Best Practices

- **Fractional differentiation** (Lopez de Prado): Instead of differencing time series by integer amounts (losing memory), use fractional d that minimizes stationarity while preserving information.
- **Dollar/volume bars** instead of time bars: Sample based on equal dollar amounts traded, not equal time intervals. This normalizes information content per bar.
- **Lagged features with multiple lookbacks**: Yesterday's return, 5-day MA, 20-day MA, 60-day MA all provide different signal.
- **Cross-sectional ranking**: Rank features across stocks at each time point (percentile ranks) instead of raw values. This handles non-stationarity.
- **Interaction features**: Momentum x size, value x momentum, quality x volatility. Trees capture these automatically, linear models need explicit terms.

**Verdict: Feature engineering matters more than model choice.** Spending 80% of effort on features and 20% on models is the right ratio.

---

## 3. Deep Learning Approaches

### 3.1 LSTM (Long Short-Term Memory)

**What was done:** Sequence models trained on time series of stock features to predict future returns.

**Reported performance:**
- 150% cumulative return vs 100% baseline over 2021-2025 out-of-sample period (trend-following variant).
- 15% reduction in RMSE, 12% gain in directional accuracy vs traditional regression. Sharpe improvements of up to 78% after transaction costs.
- MAPE of 2.72% on unseen test data, outperforming ARIMA.
- Qlib benchmark (Alpha360, CSI300): 6.47% annualized excess return, Information Ratio 0.90. Max drawdown -8.75%.

**Honest assessment:** LSTMs work better on raw price/volume data (Alpha360) than on engineered features (Alpha158), where gradient boosting dominates. The advantage is capturing temporal patterns, but this advantage is modest. Training is expensive and unstable. Hyperparameter sensitivity is high.

**Verdict: MARGINAL IMPROVEMENT over gradient boosting.** Worth testing on raw price data but not worth the complexity for most use cases.

### 3.2 Temporal Fusion Transformer (TFT)

**What was done:** Attention-based architecture designed for multi-horizon forecasting with interpretable feature importance.

**Reported performance:**
- 40-50% reduction in MAE vs LSTM and BiLSTM.
- MAPE below 2% across all tested stocks.
- Hybrid CNN-LSTM-TFT: 80.7% return on long/short strategy over 5 years, outperforming baseline.
- TFT with adaptive Sharpe ratio optimization showed improved accuracy in volatile conditions.
- Qlib benchmark (Alpha158): 8.47% annualized excess return, IR 0.81. But max drawdown -18.24% (worst of all models).

**Honest assessment:** TFT is the most interpretable deep learning approach (attention weights show which features matter when). The high drawdown in Qlib benchmarks is concerning. The architecture is complex and data-hungry. Best suited for when you need interpretability alongside prediction.

**Verdict: PROMISING but high variance.** Use if interpretability matters. Not clearly better than gradient boosting for returns.

### 3.3 Other Deep Learning Models (Qlib Benchmarks on CSI300)

Full benchmark comparison from Microsoft's Qlib, 20 runs each:

**On Alpha158 (engineered features) -- top 5 by returns:**

| Model | Ann. Excess Return | Info Ratio | Max Drawdown | IC |
|---|---|---|---|---|
| DoubleEnsemble | 11.58% | 1.34 | -9.20% | 0.052 |
| LightGBM | 9.01% | 1.02 | -10.38% | 0.045 |
| MLP | 8.95% | 1.14 | -11.03% | 0.038 |
| TFT | 8.47% | 0.81 | -18.24% | 0.036 |
| XGBoost | 7.80% | 0.91 | -11.68% | 0.050 |

**On Alpha360 (raw price/volume) -- top 5 by returns:**

| Model | Ann. Excess Return | Info Ratio | Max Drawdown | IC |
|---|---|---|---|---|
| HIST | 9.87% | 1.37 | -6.81% | 0.052 |
| IGMTF | 9.46% | 1.35 | -7.16% | 0.048 |
| TRA | 9.20% | 1.28 | -8.34% | 0.049 |
| TCTS | 8.93% | 1.23 | -8.57% | 0.051 |
| GATs | 8.24% | 1.11 | -8.94% | 0.048 |

**Key insight:** On engineered features, gradient boosting wins. On raw data, specialized architectures (HIST, IGMTF, TRA) win. Simple Transformer/LSTM are mediocre on both.

### 3.4 Deep Reinforcement Learning

**What was done:** RL agents (DQN, PPO, A2C) learn trading policies directly by maximizing reward (returns, Sharpe).

**Reported performance:**
- Bitcoin: 120x growth in NAV from $1M starting capital (2022-2025). Extremely unrealistic.
- Equities: Sharpe of 1.23 vs 1.46 buy-and-hold. Actually underperformed.
- Portfolio DQN: TQQQ ROI of 11.24%, Sharpe 0.78.

**Honest assessment:** DRL is the most overhyped approach. A systematic review concluded that "RL successfulness in finance is mostly the result of implementation quality, data pre-processing, and domain knowledge instead of algorithmic complexity." Most impressive results are on crypto (which has different dynamics than equities). Equity results are consistently modest. Training is unstable. Reproducibility is poor.

**Verdict: DO NOT USE for EOD equity trading.** The complexity-to-performance ratio is terrible. Simple rule-based systems with ML ranking outperform DRL in nearly all controlled comparisons.

---

## 4. Alternative Data Signals

### 4.1 Sentiment Analysis (NLP/LLM)

**What was done:** Using NLP models (FinBERT, GPT-2, GPT-4) to analyze news, social media, and SEC filings for trading signals.

**Reported performance:**
- MarketSenseAI (GPT-4, S&P 100): 10-30% excess alpha, cumulative return up to 72% over 15 months.
- Hybrid AI system (S&P 500, 2023-2025): 135.49% return over 24 months.
- Long-only sentiment strategies: 2-2.5% excess return over S&P benchmark.
- Long-short sentiment strategies: Outperformed multi-factor models in some tests.
- Media sentiment as single factor: Results comparable to multi-factor strategies.

**Data required:** News feeds, social media, earnings call transcripts, SEC filings.

**Honest assessment:** Sentiment is the most promising alternative data category for retail implementation. FinBERT is free and performs well. The signal decays fast (hours to days for news, days to weeks for SEC filings). Works best as an overlay on existing strategies rather than standalone. LLM-based approaches (GPT-4) show impressive results but the cost of inference at scale is high. Academic results often use unrealistic execution assumptions.

**Verdict: WORTH ADDING as overlay.** Use FinBERT for free sentiment scoring on news/earnings. Don't build a strategy around sentiment alone. Signal horizon is too short for pure EOD trading unless you catch it at market open.

### 4.2 Insider Trading Signals

**What was done:** Tracking SEC Form 4 filings. Buying stocks where multiple insiders buy (cluster buying). Avoiding stocks with insider selling.

**Reported performance:**
- Historical research shows insider buying outperforms the broader market.
- Cluster buying (multiple insiders within short timeframe) is the strongest signal.
- Open market purchases (code "P") are the meaningful signal. Ignore option exercises.

**Critical caveat:** Recent research shows "positive abnormal returns appear for short holding periods but vanish and even become negative when limiting the tradable dollar amount to a reasonable size." Returns are negatively correlated with stock liquidity, nearly negating scalability.

**Honest assessment:** Insider buying is a real signal but it's too slow and too small for systematic trading. By the time Form 4 is filed (2 business days), much of the move has happened. The stocks with the strongest signal are often illiquid small-caps where you can't trade meaningful size.

**Verdict: REAL SIGNAL, POOR EXECUTION.** Use as a qualitative filter (don't short stocks with cluster insider buying) rather than a primary signal.

### 4.3 Short Interest

**What was done:** Forming portfolios based on short interest ratio. Short high-SI stocks, buy low-SI stocks.

**Reported performance:**
- Stocks with high short interest experience negative abnormal returns (but economically small).
- Low-SI, high-volume stocks show statistically and economically significant positive returns.

**Honest assessment:** Short interest is a weak standalone signal. It works better combined with other factors (e.g., avoid high-SI stocks in a momentum portfolio). The short-side signal (shorting high-SI stocks) is risky due to short squeezes.

**Verdict: USE AS FILTER, NOT PRIMARY SIGNAL.** Exclude high-SI stocks from long portfolios. Don't build a strategy around it.

### 4.4 Satellite Imagery / Credit Card Data / Web Scraping

**Reported performance:**
- Satellite data: 4-5% returns within 3 days for retail earnings predictions.
- Credit card data: 10% improvement in quarterly earnings prediction accuracy.
- Social media sentiment: 87% forecast accuracy reported.
- 78% of hedge funds use some form of alternative data.
- Alternative data market worth $7.2B, projected $135B by 2030.

**Honest assessment:** These data sources genuinely work but are expensive ($50K-$500K+ annually for quality feeds), require significant infrastructure, and the alpha decays as more participants access the same data. Not viable for retail or small-scale quant operations. The 87% sentiment accuracy claim is misleading -- this is in-sample classification accuracy, not out-of-sample trading profitability.

**Verdict: NOT VIABLE for retail/small-scale.** Only relevant if you have $100K+ annual data budget.

---

## 5. Cross-Sectional Models

### 5.1 The Gu, Kelly, Xiu Framework (THE GOLD STANDARD)

**What was done:** Comprehensive horse race of ML models predicting monthly stock returns using 94 characteristics covering 8 themes (momentum, value, profitability, volatility, etc.). Tested on full US stock universe 1957-2016.

**Models tested:** OLS, elastic net, principal components regression, partial least squares, random forest, gradient boosted trees, neural networks (1-5 layers).

**Key results:**
- Best models: Trees and neural networks, with gains from capturing non-linear predictor interactions.
- Neural networks achieved out-of-sample R-squared of 0.4% monthly (sounds small, but this is economically large for stock returns).
- Long-short decile spread portfolios achieved Sharpe ratios of ~1.4-2.1 depending on model.
- Tidy Finance replication: Random forest long-short Sharpe of 2.14, monthly excess return of 0.36%.

**Most important features (consensus across all models):**
1. Price momentum variants (most predictive)
2. Liquidity (market cap, turnover, bid-ask)
3. Volatility (return vol, idiosyncratic vol, beta)
4. Value and profitability (secondary importance)

**Honest assessment:** This is the most rigorous academic study of ML in asset pricing. The Sharpe ratios are impressive but come from a very long backtest period (60 years). Recent sub-periods likely show lower Sharpe as more quants adopt these methods. The dataset is publicly available for replication (stock-level characteristics through 2021). This paper is ground truth for what works.

**Verdict: THE FOUNDATION. Build on this.** Start with the Gu-Kelly-Xiu feature set and gradient boosting. Everything else is incremental.

### 5.2 Learn-to-Rank for Stock Selection

**What was done:** Instead of predicting returns (regression), directly optimize the ranking of stocks using learning-to-rank algorithms (LambdaMART, ListNet).

**Reported performance:**
- Approximately threefold boosting of Sharpe ratios compared to traditional cross-sectional approaches.
- LambdaMART outperforms pointwise regression because the loss function directly optimizes for what matters: the relative ordering of stocks.

**Honest assessment:** This is a genuinely clever insight. You don't need accurate return predictions; you need accurate rankings. Learn-to-rank handles this directly. Implementation is straightforward with LightGBM's ranking mode. The 3x Sharpe claim likely overstates the real improvement, but the direction is correct.

**Verdict: WORTH IMPLEMENTING.** Use LightGBM in LambdaRank mode instead of regression mode. Small implementation change, potentially meaningful improvement.

### 5.3 Post-Earnings Announcement Drift (PEAD) with ML

**What was done:** Traditional PEAD (buying after positive earnings surprises) enhanced with ML to better predict which surprises will persist.

**Reported performance:**
- ML-enhanced PEAD nearly doubles Sharpe ratios compared to simple 1-quarter SUE.
- Using 12 quarters of earnings history (instead of just 1) with elastic net models, alphas remain significant after controlling for standard factors.
- Gains strongest among large-cap stocks where recent surprises are quickly priced in.

**Honest assessment:** PEAD is one of the most robust anomalies in finance, documented since 1968. ML enhancement genuinely helps because it can identify which patterns of earnings history predict future drift. The quarterly rebalancing frequency is practical. This is implementable with EOD data and standard fundamental data.

**Verdict: HIGH CONVICTION. Implement this.** Combine earnings surprises with ML-based pattern recognition across multiple quarters.

### 5.4 Multi-Factor Models with ML Combination

**What was done:** Instead of equal-weighting factors, use ML to dynamically weight value, momentum, quality, size, and volatility factors.

**Reported performance:**
- Quality + Momentum combination: 93% outperformance rate with positive worst-quartile outcome of 2.57%.
- ML-based factor timing achieves higher Sharpe ratios than static combinations.
- AQR, Two Sigma, Renaissance: hundreds of signals with ML overlays and dynamic weighting.

**Honest assessment:** The improvement from ML factor combination over simple equal-weighting is real but modest (10-30% Sharpe improvement). The main benefit is reducing drawdowns through dynamic allocation rather than boosting returns.

**Verdict: INCREMENTAL BUT RELIABLE.** Use ML to combine factors rather than picking one factor.

---

## 6. Volatility and Options-Informed Equity Trading

### 6.1 VIX as Equity Trading Filter

**What was done:** Use VIX levels and VIX regime (high/low volatility) as on/off switch for equity strategies.

**Key findings:**
- VIX-based filters improve Sharpe and Calmar ratios.
- The volatility risk premium (implied > realized) is more consistent in low-vol regimes.
- Mean reversion strategies work best when VIX is elevated (buy oversold in high-fear environments).
- Trend strategies work best in low-moderate VIX environments.

**Practical implementation with EOD data:**
- VIX < 15: Favor trend-following, momentum strategies
- VIX 15-25: Standard positioning
- VIX 25-35: Favor mean reversion, reduce position size
- VIX > 35: Cash or aggressive mean reversion on highly oversold levels

**Honest assessment:** VIX regime filtering is one of the simplest and most effective improvements to any equity strategy. It requires zero ML. Just classifying the regime and adjusting strategy parameters delivers meaningful improvement.

**Verdict: IMPLEMENT IMMEDIATELY.** This is free alpha from a publicly available indicator.

### 6.2 Hidden Markov Model Regime Detection

**What was done:** HMM trained on returns and volatility to classify market into 2-3 regimes (bull/bear/neutral). Strategy adapts based on detected regime.

**Reported performance:**
- Regime-switching models delivered higher absolute returns and better benchmarking metrics vs individual factor models (Sep 2017 - Apr 2020 out-of-sample).
- Avoiding trades in high-volatility regimes eliminated many losing trades and improved Sharpe.

**Practical implementation:**
- Train 2-3 state HMM on daily returns + volatility.
- State 0 (low-vol bull): Full position, favor momentum
- State 1 (high-vol bear): Reduced position or short
- State 2 (neutral): Standard positioning

**Honest assessment:** HMMs are simple to implement (sklearn, hmmlearn) and provide genuine value. The main risk is regime detection lag -- HMMs are backward-looking and may identify a regime change after it's already happened. Using 3 states tends to be more robust than 2.

**Verdict: WORTH IMPLEMENTING.** Simple HMM regime detection improves most strategies. Use as overlay, not standalone.

---

## 7. Walk-Forward and Robust Backtesting

### 7.1 Lopez de Prado's Framework (CRITICAL READING)

**The 10 Reasons Most ML Funds Fail:**

1. **The Sisyphus Paradigm**: Developing ML strategies alone is nearly impossible because it takes almost as much effort to produce one true strategy as a hundred. You need a production pipeline, not one-off experiments.

2. **Research Through Backtesting**: "Backtesting is not a research tool. Feature importance is." Never use backtests to develop strategies. Use feature importance to find signals, THEN backtest once.

3. **Chronological Sampling**: Using time bars introduces arbitrary sampling. Use volume/dollar bars instead.

4. **Integer Differentiation**: Differencing time series by integer amounts destroys memory. Use fractional differentiation.

5. **Fixed-Time Horizon Labeling**: Labeling returns over fixed periods ignores path (stop losses, take profits). Use triple barrier method instead.

6. **Learning Side and Size Simultaneously**: Don't train one model to predict both direction and magnitude. Use meta-labeling: first model predicts direction, second model predicts bet size.

7. **Weighting Non-IID Samples**: Financial labels overlap in time. Weight samples by uniqueness.

8. **Cross-Validation Leakage**: Standard k-fold CV leaks information because financial samples are serially correlated. Use purged k-fold CV.

9. **Backtest Overfitting**: After 1,000 independent backtests, expected maximum Sharpe is 3.26 even if true Sharpe is 0. Use Deflated Sharpe Ratio to correct for multiple testing.

10. **Lack of Experimental Framework**: ML in finance should be treated as experimental physics, not data mining.

### 7.2 Combinatorial Purged Cross-Validation (CPCV)

**What it does:** Creates multiple train-test splits while purging overlapping samples and enforcing embargo periods.

**Why it's better than walk-forward:**
- Walk-forward tests a single historical path (high variance).
- CPCV constructs C(N,k) paths, providing much lower variance estimates.
- Probability of Backtest Overfitting (PBO) and Deflated Sharpe Ratio (DSR) can be computed.

**Comparative results:**
- CPCV shows lower PBO and superior DSR compared to walk-forward.
- Walk-forward exhibits notable shortcomings in false discovery prevention.

**Honest assessment:** CPCV is mathematically superior to walk-forward but harder to implement. For practical purposes, use expanding-window walk-forward with purging as a minimum standard. Use CPCV if you're running many strategy variants and need to control false discovery rate.

**Verdict: USE PURGED WALK-FORWARD as minimum. CPCV for production.** This is not optional; it's the difference between fooling yourself and finding real signals.

### 7.3 Alpha Decay and Signal Half-Life

**Key findings from recent research:**
- Alpha on new trades decays in ~12 months on average, even at professional quant funds.
- **Mechanical factors** (momentum, reversal) crowd fastest because signals are unambiguous:
  - Momentum Sharpe: ~1.5 in mid-1990s, ~0.25 today
  - Momentum R-squared with crowding model: 0.65
- **Judgment factors** (value, quality) show minimal decay because implementation differs:
  - Value R-squared with crowding model: 0.05
- Crowding accelerated post-2015 with factor ETF growth (correlation with ETF volume: -0.63).
- Crowding predicts crashes, not returns. Useful for risk management, not alpha generation.

**Practical implications:**
- Prefer judgment factors (value, quality, profitability) over mechanical factors (momentum, reversal) for longer-lived alpha.
- Use ML to find non-obvious signal combinations that are harder to replicate (resist crowding).
- Monitor factor ETF flows as a crowding indicator.
- Expect any simple signal's Sharpe to halve within 3-5 years of publication.

---

## 8. Open-Source Quant Frameworks

### 8.1 Microsoft Qlib (RECOMMENDED)

**What it is:** AI-oriented quantitative investment platform with full ML pipeline.

**Key features:**
- 40+ SOTA models implemented (LightGBM, LSTM, Transformer, HIST, TRA, GATs, etc.)
- Alpha158 and Alpha360 feature datasets
- Full pipeline: data processing, model training, backtesting, portfolio optimization, order execution
- RD-Agent: LLM-driven automated factor discovery

**Benchmark results (CSI300):**
- Best model (DoubleEnsemble): 11.58% annualized excess, IR 1.34, max DD -9.2%
- LightGBM: 9.01% excess, IR 1.02, max DD -10.4%
- 20+ models benchmarked with 20-run statistics

**Limitations:** Primarily tested on Chinese market (CSI300). Yahoo Finance data quality is imperfect. Production use requires own data pipeline.

**Verdict: Best starting point for ML trading research.** Fork it, adapt features, test on your markets.

### 8.2 Stefan Jansen's "Machine Learning for Algorithmic Trading"

**What it is:** 800+ page book with complete code covering linear models through deep RL.

**Covers:** Factor models, sentiment analysis, gradient boosting, deep learning, reinforcement learning, alternative data.

**Code:** github.com/stefan-jansen/machine-learning-for-trading (23 chapters of Python notebooks).

**Verdict: Best educational resource.** Read this before implementing anything.

### 8.3 QuantConnect (Lean Engine)

**What it is:** Open-source event-driven backtesting and live trading engine (Python/C#).

**Key features:** Multi-asset, multi-timeframe, paper trading, live trading integration.

**Available research:** Fundamental factor analysis, Kalman Filter pairs trading, mean-variance optimization, EMA cross strategies.

**Verdict: Best for live deployment** once you have a strategy from research.

### 8.4 Other Notable Frameworks

| Framework | Best For | Language | Notes |
|---|---|---|---|
| Backtrader | Quick prototyping | Python | Simple API, live trading support |
| Zipline | Quantopian-style research | Python | Event-driven, good for EOD |
| QuantRocket | Data + backtesting + live | Python | Commercial, but good data pipeline |
| finmarketpy | Multi-asset backtesting | Python | Good for cross-asset strategies |
| awesome-quant (GitHub) | Resource aggregation | - | Curated list of 100+ quant tools |

---

## 9. Practical Recommendations

### 9.1 What to Implement (Ranked by Evidence Strength)

**Tier 1 -- Strong evidence, implement first:**

1. **Gradient boosting cross-sectional model** (LightGBM/XGBoost)
   - Features: Gu-Kelly-Xiu canonical set (momentum, liquidity, volatility, value, quality)
   - Target: Next-month stock returns (cross-sectional ranking)
   - Rebalance: Monthly
   - Expected Sharpe: 0.7-1.2 (developed markets), 1.0-1.5 (emerging markets)
   - Use LambdaRank mode for direct ranking optimization

2. **ML-enhanced PEAD (Post-Earnings Announcement Drift)**
   - Use elastic net or gradient boosting on 12 quarters of earnings history
   - Signal: Buy after positive earnings surprises, short after negative
   - Expected Sharpe: ~1.0-1.5

3. **VIX regime filtering** on any equity strategy
   - Free, no ML needed, just conditional logic
   - Improves Sharpe and reduces drawdowns on any base strategy

4. **Mean reversion with RSI** (already in your strategy-backtester)
   - 2-day RSI below 10-15: buy. Above 85-90: sell.
   - Add trend filter (only buy when above 200-day MA)
   - Historical CAGR: 10-12%, max DD: 20-30%

**Tier 2 -- Moderate evidence, implement second:**

5. **HMM regime detection** as overlay
   - 2-3 state HMM on returns + volatility
   - Adjust position sizing per regime

6. **Multi-factor ML combination**
   - Value + Momentum + Quality combined via gradient boosting
   - Better than equal-weighting, especially for drawdown reduction

7. **Sentiment scoring** (FinBERT) as overlay
   - Free model, apply to news headlines pre-market
   - Use as confirmation signal, not primary

**Tier 3 -- Promising but unproven for retail:**

8. **TFT / advanced deep learning** for raw price prediction
9. **Pairs trading with cointegration** (real-world Sharpe often disappoints)
10. **Insider trading signals** (use as filter only)

### 9.2 What to Avoid

- **Deep reinforcement learning** for EOD equity trading (terrible complexity/performance ratio)
- **Pure price prediction** models (predict returns, not prices)
- **Cryptocurrency results** applied to equities (different market structure)
- **Any single backtest** as evidence (use CPCV or at minimum purged walk-forward)
- **Satellite/credit card data** unless you have $100K+ data budget
- **Simple momentum** as standalone signal (Sharpe decayed from 1.5 to 0.25 due to crowding)
- **In-sample accuracy** as success metric (use out-of-sample Sharpe after transaction costs)

### 9.3 Backtesting Discipline

1. **Never research through backtesting.** Use feature importance first.
2. **Always use purged walk-forward** at minimum. CPCV for production.
3. **Include transaction costs.** 10-20 bps per trade for liquid stocks, 30-50 bps for small caps.
4. **Report Deflated Sharpe Ratio** when comparing strategies.
5. **Track number of trials.** After 100 trials, expected max Sharpe for a null strategy is ~2.5.
6. **Use fractional differentiation** for time series features.
7. **Triple barrier labeling** instead of fixed-horizon returns.
8. **Weight samples by uniqueness** (overlapping labels are not independent).
9. **Meta-labeling:** Separate direction prediction from bet sizing.
10. **Expect 50% Sharpe degradation** from backtest to live.

### 9.4 Realistic Expectations

| Metric | Academic Backtest | Realistic Live | Exceptional Live |
|---|---|---|---|
| Annual Return (long-only) | 15-25% | 8-15% | 15-20% |
| Annual Return (long-short) | 20-40% | 10-20% | 20-30% |
| Sharpe Ratio | 1.5-3.0 | 0.5-1.0 | 1.0-2.0 |
| Max Drawdown | -10 to -20% | -15 to -30% | -10 to -20% |
| Win Rate (monthly) | 55-65% | 50-58% | 55-62% |
| Alpha Decay Half-Life | - | 6-18 months | 2-5 years (for complex signals) |

---

## Key Sources

### Landmark Papers
- [Gu, Kelly, Xiu (2020) - Empirical Asset Pricing via Machine Learning](https://academic.oup.com/rfs/article/33/5/2223/5758276)
- [Lopez de Prado - 10 Reasons ML Funds Fail](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816)
- [Lopez de Prado - Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Building Cross-Sectional Strategies by Learning to Rank](https://arxiv.org/pdf/2012.07149)
- [Alpha Decay: Not All Factors Crowd Equally](https://arxiv.org/html/2512.11913v1)

### ML Model Benchmarks
- [Microsoft Qlib Benchmarks](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)
- [Qlib Platform Paper](https://arxiv.org/pdf/2009.11189)
- [Tidy Finance - Gu Kelly Xiu Replication](https://www.tidy-finance.org/blog/gu-kelly-xiu-replication/)

### Practical Implementation
- [Stefan Jansen - ML for Algorithmic Trading (Code)](https://github.com/stefan-jansen/machine-learning-for-trading)
- [Lopez de Prado - Advances in Financial ML (Notes)](https://reasonabledeviations.com/notes/adv_fin_ml/)
- [QuantConnect LEAN Engine](https://github.com/QuantConnect/Lean)
- [Awesome Systematic Trading (GitHub)](https://github.com/paperswithbacktest/awesome-systematic-trading)

### Feature Engineering & Signals
- [Survey of Feature Selection for Stock Prediction](https://link.springer.com/article/10.1186/s40854-022-00441-7)
- [ML-enhanced PEAD](https://www.sciencedirect.com/science/article/abs/pii/S1544612325020057)
- [LightGBM Cross-Sectional Stock Returns](https://dl.acm.org/doi/full/10.1145/3727353.3727490)

### Sentiment & Alternative Data
- [MarketSenseAI (GPT-4 Trading)](https://link.springer.com/article/10.1007/s00521-024-10613-4)
- [LLMs in Equity Markets](https://pmc.ncbi.nlm.nih.gov/articles/PMC12421730/)
- [Alternative Data for Algorithmic Trading](https://www.luxalgo.com/blog/alternative-data-for-algorithmic-trading-what-works/)

### Volatility & Regime
- [Regime-Switching Factor Investing with HMM](https://www.mdpi.com/1911-8074/13/12/311)
- [VIX Trading Strategies](https://www.quantifiedstrategies.com/vix-trading-strategy/)
- [Volatility Trading Strategies](https://www.quantifiedstrategies.com/volatility-trading-strategies/)

### Deep Learning
- [Hybrid CNN-LSTM-TFT](https://www.iieta.org/journals/isi/paper/10.18280/isi.301122)
- [LSTM Trend Forecasting for Equities](https://arxiv.org/html/2603.14453)
- [TFT for Stock Prediction](https://www.mdpi.com/1424-8220/25/3/976)
- [Deep RL for Trading - Survey](https://arxiv.org/html/2512.10913v1)

### Backtesting Methodology
- [CPCV - Wikipedia](https://en.wikipedia.org/wiki/Purged_cross-validation)
- [Backtest Overfitting Comparison](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)
- [CPCV Implementation with Code](https://www.quantbeckman.com/p/with-code-combinatorial-purged-cross)

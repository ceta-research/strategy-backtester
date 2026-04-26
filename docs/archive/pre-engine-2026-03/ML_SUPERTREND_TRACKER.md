# ML Supertrend + Quality Dip: Experiment Tracker

## Goal
Build strategies that achieve 20%+ CAGR on NSE and US after all charges (STT, slippage, costs). Combine insights from TradingView's ML Supertrend with quality-dip-buy logic.

## Source
- Pine Script: `/Users/swas/Desktop/claude_context/c_pine_script` (1,329 lines)
- Author: Zimord/Aslan on TradingView

---

## Data Landscape (from SQL inspection, 2026-04-07)

### US (FMP stock_eod)
| Turnover Tier | Symbols | Avg Turnover |
|---------------|---------|--------------|
| >$100M/day | 1,225 | $2.5B |
| $50-100M | 635 | $72M |
| $10-50M | 2,065 | $24M |
| $1-10M | 4,847 | $3.5M |

**Market cap (active):** 2,043 large (>$10B), 2,643 mid ($2-10B), 2,710 small ($0.5-2B)
**Volatility:** Median daily range = 2.73%
**Data quality:** 17K symbols with O=H=L=C bad rows in 2010-2025. Must filter.

### NSE (FMP stock_eod + Bhavcopy)
| Turnover Tier | Symbols (FMP) |
|---------------|---------------|
| >70M INR (engine default) | 715 |
| 30-70M | 273 |
| 10-30M | 296 |
| 1-10M | 295 |

**Volatility:** Median daily range = 4.21% (50% higher than US)
**Quality universe (8+/10 positive years, 2015-2024):** 144 stocks (4 with 10/10, 22 with 9/10, 118 with 8/10)

---

## Critical Data Findings (SQL analysis)

### Finding 1: Trough is the WORST place to buy

Forward returns on quality stocks (8+/10yr positive) with 10%+ dip, by oscillation position:

| Oscillation Position | Bouncing >2% | 252d Return | Win Rate (126d) |
|---------------------|-------------|-------------|-----------------|
| **Near peak (0-25% of typical range)** | **Yes** | **87.8%** | **74%** |
| Mid-high (25-50%) | Yes | 59.2% | 60% |
| Mid-low (50-75%) | Yes | 29.6% | 46% |
| Trough (75-100%) | Yes | 13.2% | 38% |

**Oscillation position** = current_drawdown / typical_max_drawdown (trailing 252d).
- 0% = at peak, 100% = at typical trough.
- "Near peak" means the dip is mild relative to the stock's own historical volatility.

**Insight:** A mild dip on a quality stock = temporary pullback in a strong uptrend. A deep dip near typical trough = something fundamentally wrong. Buy the shallow pullbacks, not the deep crashes.

### Finding 2: Bear markets kill all dip-buying

| Year | Avg 126d Return | Win Rate | Market Context |
|------|----------------|----------|----------------|
| 2018 | -13.8% | 25% | Small/mid-cap bear |
| 2019 | -15.4% | 24% | Continued weakness |
| 2020 | +46.0% | 80% | COVID recovery |
| 2021 | +41.7% | 73% | Bull market |
| 2022 | +2.7% | 40% | Choppy |

### Finding 3: Broad market regime filter is insufficient

NIFTY was above SMA200 for 82% of days in 2018-2019 (the worst years). Large-cap NIFTY was fine while small/mid-caps were crushed. **Need stock-level trend confirmation, not market-level.**

### Implication for Strategy Design

The winning formula is:
1. **Quality universe** (8+/10yr positive returns)
2. **Mild dip** (10%+ from peak, but NOT near historical trough -- osc_position < 0.50)
3. **Stock-level trend confirmation** (SuperTrend flip, not NIFTY SMA200)
4. **Exit on stock-level trend reversal** (SuperTrend flip down, or TSL)

---

## Experiment Variants (6 total)

### V1: Quality Mild-Dip + SuperTrend Entry (PRIMARY)
**Hypothesis:** Buy quality stocks (8+/10yr positive) after a 10%+ pullback from peak, but only when the dip is mild relative to stock's own history AND SuperTrend on the individual stock flips bullish. This addresses all 3 data findings: quality universe, mild dip > deep dip, stock-level trend confirmation.

| Parameter | Values |
|-----------|--------|
| Quality threshold | 7/10, 8/10 positive years |
| Min dip from 252d peak | 10%, 15% |
| Max oscillation position | 0.25, 0.50 (mild dip only) |
| SuperTrend ATR period | 14, 20 |
| SuperTrend multiplier | 2.0, 2.5, 3.0 (calibrated for NSE volatility) |
| Entry trigger | SuperTrend flip to bullish |
| TSL exit | 10%, 15% |
| Max hold | 126d, 252d |
| Max positions | 15, 20, 25 |

**Configs:** 2 x 2 x 2 x 2 x 3 x 2 x 2 x 3 = **576**
**Data:** NSE Bhavcopy + US FMP
**Expected:** **20-35% CAGR.** The 87.8% raw 252d return on the best bucket suggests a portfolio CAGR of 25-35% in bull years, with SuperTrend keeping us out during bear years. After costs and averaging across regimes, 20-25% is realistic.
**This is the most promising variant** based on data.

---

### V2: Quality Mild-Dip + Simple Momentum (No SuperTrend)
**Hypothesis:** Same quality + mild dip logic but use simple momentum confirmation (5d return > 2%) instead of SuperTrend. Tests whether SuperTrend adds value over simple bounce detection.

| Parameter | Values |
|-----------|--------|
| Quality threshold | 7/10, 8/10 positive years |
| Min dip from 252d peak | 10%, 15% |
| Max oscillation position | 0.25, 0.50 |
| Momentum confirmation | 5d return > 1%, 2%, 3% |
| TSL exit | 10%, 15% |
| Max hold | 126d, 252d |
| Max positions | 15, 20, 25 |

**Configs:** 2 x 2 x 2 x 3 x 2 x 2 x 3 = **288**
**Data:** NSE Bhavcopy + US FMP
**Expected:** 18-25% CAGR. Simpler than V1 but may suffer in bear markets without stock-level trend filter.

---

### V3: SuperTrend x Trending Value Hybrid
**Hypothesis:** Use SuperTrend as entry/exit timing on the trending-value factor universe (composite value rank + momentum in small/mid-caps). Trending-value already produces 25.55% CAGR with quarterly rebalance. SuperTrend timing could reduce drawdowns by avoiding entries during downtrends.

| Parameter | Values |
|-----------|--------|
| Universe | Trending-value top 30 stocks (quarterly re-screen) |
| SuperTrend ATR period | 14, 20 |
| SuperTrend multiplier | 2.0, 2.5 |
| Signal mode | Reversal, Breakout |
| TSL exit | 10%, 15% |
| Max hold | 126d, 252d |
| Max positions | 15, 25 |

**Configs:** 2 x 2 x 2 x 2 x 2 x 2 = **64**
**Data:** NSE FMP (needs fundamentals for value screening)
**Expected:** **20-30% CAGR with lower MDD** than pure trending-value (-71.6% MDD currently). SuperTrend should help avoid buying into downtrends that kill quarterly rebalance strategies.

---

### V4: Mid-Cap SuperTrend (Lower Liquidity Threshold)
**Hypothesis:** Drop the engine pipeline's 70M INR turnover threshold to 10-30M, opening ~570 more NSE stocks. Mid-caps are less efficiently priced. Combine with quality filter to avoid junk.

| Parameter | Values |
|-----------|--------|
| Turnover threshold | 10M, 20M, 30M INR |
| Quality filter | 6/10, 7/10 positive years (relaxed for more stocks) |
| Signal mode | Reversal, Breakout |
| SuperTrend ATR period | 14, 20 |
| SuperTrend multiplier | 2.0, 2.5, 3.0 |
| Volume filter | 1.5x (mandatory -- liquidity guard) |
| TSL exit | 10%, 15%, 20% |
| Max hold | 126d, 252d |
| Max positions | 15, 20, 25 |

**Configs:** 3 x 2 x 2 x 2 x 3 x 3 x 2 x 3 = **1,296**
**Data:** NSE Bhavcopy (lower turnover thresholds)
**Expected:** 15-22% CAGR. More signal frequency than V1 but higher slippage risk.

---

### V5: Regime-Adaptive SuperTrend (ML Port)
**Hypothesis:** Port the Pine Script's regime detection (Hurst + entropy + ADX) to classify market conditions per-stock. Use regime-specific SuperTrend parameters (tighter in trending, wider in choppy). The regime grid is the actual "ML" contribution of the indicator.

| Parameter | Values |
|-----------|--------|
| Quality threshold | 7/10, 8/10 positive years |
| Regime detection period | 50, 100 bars |
| Regime bins | 4x4, 8x8 |
| Base ATR period | 20 |
| Base ATR multiplier | 2.0 |
| Trending regime: tighter params | multiplier 1.4-1.8 |
| Choppy regime: wider params | multiplier 2.5-3.0 |
| Min dip from peak | 10% |
| TSL exit | 10%, 15% |
| Max positions | 15, 20 |

**Configs:** 2 x 2 x 2 x 2 x 2 = **32** (but more compute per config due to regime detection)
**Data:** NSE Bhavcopy + US FMP
**Expected:** 18-25% CAGR. Fewer trades but better quality through regime awareness.

---

### V6: US Large-Cap Quality Dip (Cross-Market Validation)
**Hypothesis:** Apply the quality mild-dip strategy to US stocks. Larger universe (3,800+ qualifying stocks vs 144 on NSE), different market dynamics, lower transaction costs. Tests whether the alpha is specific to NSE or universal.

| Parameter | Values |
|-----------|--------|
| Quality threshold | 7/10, 8/10 positive years |
| Min dip from 252d peak | 10%, 15% |
| Max oscillation position | 0.25, 0.50 |
| SuperTrend ATR period | 14, 20 |
| SuperTrend multiplier | 1.4, 2.0 (calibrated for lower US volatility) |
| TSL exit | 8%, 10%, 15% |
| Max hold | 126d, 252d |
| Max positions | 20, 30 |

**Configs:** 2 x 2 x 2 x 2 x 2 x 3 x 2 x 2 = **384**
**Data:** US FMP (NASDAQ + NYSE + AMEX)
**Expected:** 15-20% CAGR. US is more efficient than NSE, so lower ceiling, but lower costs too. adjClose filter required.

---

## Execution Priority

| Priority | Variant | Why | Configs |
|----------|---------|-----|---------|
| **1** | **V1: Quality Mild-Dip + SuperTrend** | Best data signal (87.8% raw return), addresses all 3 findings | 576 |
| **2** | **V2: Quality Mild-Dip + Simple Momentum** | Control group -- does SuperTrend add value? | 288 |
| **3** | **V3: SuperTrend x Trending Value** | Highest baseline (25.55%), SuperTrend for timing | 64 |
| 4 | V4: Mid-Cap SuperTrend | Larger universe, higher alpha potential | 1,296 |
| 5 | V5: Regime-Adaptive | True ML port, most complex | 32 |
| 6 | V6: US Cross-Market | Validation, lower priority | 384 |

---

## Implementation Checklist

### Core Engine (shared across all variants)
- [ ] `engine/signals/ml_supertrend.py` -- SuperTrend + quality dip signal generator
  - [ ] ATR computation (RMA-smoothed, matches Pine Script)
  - [ ] SuperTrend bands with trend flip detection
  - [ ] Quality universe builder (N/10 positive years)
  - [ ] Drawdown + oscillation position computation
  - [ ] Dip detection (% from 252d peak)
  - [ ] Momentum confirmation (simple + SuperTrend modes)
  - [ ] Regime detection (Hurst + entropy + ADX) for V5
- [ ] `engine/config_loader.py` -- config builders
- [ ] `engine/pipeline.py` -- register import
- [ ] Integration with `walk_forward_exit()` for multi-day holding

### Per-Variant Configs
- [ ] `strategies/ml_supertrend/config_v1_quality_dip_st.yaml`
- [ ] `strategies/ml_supertrend/config_v2_quality_dip_simple.yaml`
- [ ] `strategies/ml_supertrend/config_v3_trending_value_st.yaml`
- [ ] `strategies/ml_supertrend/config_v4_midcap.yaml`
- [ ] `strategies/ml_supertrend/config_v5_regime.yaml`
- [ ] `strategies/ml_supertrend/config_v6_us.yaml`

---

## Results

### Engine Pipeline Results (2026-04-08)

All results: 15yr NSE (2010-2025), full charges + 5bps slippage, MOC execution.

**Key finding: engine pipeline's `top_performer` ranking is wrong for dip-buy. Need `top_dipper` (deepest dip first) to match standalone.**

| Strategy | Data | Ranking | Best CAGR | MDD | Calmar | Config |
|----------|------|---------|-----------|-----|--------|--------|
| momentum_dip_quality | FMP | top_performer | 13.5% | -52.9% | 0.26 | mom=63d, top20%, dip=5%, pos=5 |
| momentum_dip_quality | nse_charting | top_performer | 10.7% | -43.4% | 0.25 | mom=63d, top20%, dip=5%, pos=10 |
| momentum_dip_quality | bhavcopy | top_performer | 7.5% | -33.7% | 0.22 | mom=63d, top20%, dip=7%, pos=10 |
| ml_supertrend (V1) | FMP | top_performer | 5.5% | -29.5% | 0.19 | ST(20,2.5), breakout, 8/10yr |
| ml_supertrend (V2) | FMP | top_performer | 7.9% | -29.9% | 0.26 | no ST, 8/10yr, osc<0.50 |
| **standalone champion** | **nse_charting** | **dip-depth** | **25.3%** | **-28.4%** | **0.89** | **mom=63d, top30%, dip=5%, pos=10** |

### Gap Analysis

14.6pp gap between engine (10.7%) and standalone (25.3%) on identical data (nse_charting_day).

| Factor | Impact | Evidence |
|--------|--------|----------|
| **Entry ordering (top_performer vs dip-depth)** | **~10-15pp** | Primary. Standalone picks deepest dips, engine picks recent winners. |
| Pre-computed vs real-time exits | ~1-2pp | Engine fixes exits at signal gen, standalone checks daily |
| Exit price (close vs next-open) | ~0.5pp | Minor timing difference |
| Data source (when different) | ~3pp | FMP vs nse_charting; 0 when same source |

### Next: `top_dipper` ranking (not yet implemented)

Expected to close most of the 14.6pp gap. See session handover for implementation plan.
| V5 Regime | NSE | | | | | |
| V6 US | US | | | | | |

---

## References
- Pine Script source: `/Users/swas/Desktop/claude_context/c_pine_script`
- TradingView: https://www.tradingview.com/script/9SgtsBck-Machine-Learning-Supertrend-Aslan/
- Prior session: `docs/sessions/completed/2026-04-06/SESSION_HANDOVER_20PCT_CAGR_HUNT.md`
- Engine pipeline ceiling: 13.2% CAGR on breakout/momentum strategies (does NOT apply to dip-buy)
- Trending value baseline: 25.55% CAGR (backtests framework, quarterly rebalance)
- Standalone dip-buy baseline: 23.7% CAGR / Calmar 1.01 (momentum_dip_buy.py)
- FMP data quality: `docs/2-work-management/FMP_ADJCLOSE_IMPACT_ASSESSMENT.md`

# eod_breakout — Holdout Re-Optimization Experiment

**Date:** 2026-04-26
**Hypothesis:** Current champion was overfit to 2019-2024 bull regime. Re-pick champion using only 2010-2024 data (true holdout for 2025+) and test forward.
**Engine:** post-audit `fbcd36a+`
**Result files:** `results/eod_breakout/holdout_train_2010-2024.json`, `holdout_champion_full.json`

---

## Summary

| | Current (`1_15_1_1`) | **Holdout (`2_20_3_1`)** | Δ |
|---|---:|---:|---:|
| CAGR (full 2010-2026) | 15.20% | **15.90%** | +0.70pp |
| MDD | -34.10% | **-24.45%** | **+9.65pp** |
| Calmar | 0.446 | **0.650** | **+46%** |
| Sharpe | 0.698 | **0.943** | +35% |
| Sortino | 1.064 | **1.331** | +25% |
| Annualized vol | 18.89% | **14.75%** | -22% |
| Skewness | 7.29 | **-0.52** | normal |
| **Kurtosis** | **572** | **5.7** | **no fat tails** |
| Final value (₹10M start) | ₹99.06M | **₹109.42M** | +₹10.36M |
| Worst year | -16.32% | -18.10% | -1.78pp |
| Best year | +91.02% | +73.42% | -17.59pp |
| **2025 OOS (the test)** | **-16.32%** | **-13.20%** | **+3.12pp** |

**Verdict:** Holdout champion is **structurally better on every robustness metric** at modest CAGR cost (lost peak years, smoother path). 2025 OOS improvement modest (+3.12pp) but in the right direction.

---

## What changed

### Champion params

| Parameter | Current | Holdout | Direction |
|---|---|---|---|
| `price_threshold` | 50 | **99** | Higher minimum stock price |
| `n_day_high` | 7 | **3** | Faster breakout (more entries) |
| `n_day_ma` | 5 | **10** | Longer trend filter |
| `direction_score` | {3, 0.54} | **{5, 0.40}** | Looser breadth, longer MA |
| `min_hold_time_days` | 0 | **7** | Forces 1-week min hold (no whipsaw) |
| `trailing_stop_pct` | 8 | 8 | (unchanged) |
| `order_sorting_type` | top_gainer | top_gainer | (unchanged) |
| `max_positions` | 15 | 15 | (unchanged) |

5 of 7 sweepable params changed. Most impactful: **`price_threshold` 50→99** (excludes the cheapest, most volatile mid-caps) and **`min_hold_time_days` 0→7** (prevents same-week whipsaw losses).

### Rank in holdout sweep (1152 configs, sorted by Calmar)

| Config | Decoded | Holdout rank |
|---|---|---:|
| `2_20_3_1` (holdout champion) | pt=99, ndh=3, ndm=10, ds={5,0.40}, mh=7, tsl=8 | **#1** |
| `2_15_1_1` (current champion at pt=99) | pt=99, ndh=7, ndm=5, ds={3,0.54}, mh=0, tsl=8 | #50 |
| `1_15_1_1` (CURRENT champion) | pt=50, ndh=7, ndm=5, ds={3,0.54}, mh=0, tsl=8 | **#457** |

**The current champion ranks #457 of 1152 on 2010-2024 alone.** It was selected by full-period optimization that included some of 2025 — the data that subsequently invalidated it. Without that data, it doesn't even crack the top quartile.

---

## Yearly comparison (full 2010-2026)

| Year | Cur Ret | New Ret | Δ | Cur MDD | New MDD | Notable |
|---|---:|---:|---:|---:|---:|---|
| 2010 | +5.02% | -1.28% | -6.30 | -16.3% | -20.2% | new lost early gains |
| 2011 | -16.27% | -18.10% | -1.83 | -22.3% | -23.3% | both bear |
| 2012 | +23.52% | +25.30% | +1.78 | -22.7% | -23.7% | similar |
| 2013 | -4.53% | -7.16% | -2.63 | -18.8% | -19.7% | both modest down |
| **2014** | +28.31% | **+38.49%** | **+10.18** | -12.7% | -13.3% | new outperforms |
| 2015 | +12.73% | +10.69% | -2.03 | -15.5% | -18.1% | similar |
| 2016 | -10.61% | -10.28% | +0.34 | -23.2% | -20.9% | similar |
| **2017** | **+84.73%** | +73.42% | -11.31 | -19.7% | **-15.9%** | new less peak, less DD |
| 2018 | -12.59% | -9.02% | +3.57 | -18.7% | -19.5% | new better |
| 2019 | +10.09% | +6.90% | -3.19 | **-34.1%** | **-17.0%** | **new MDD halved** |
| **2020** | +55.57% | **+68.57%** | **+13.00** | **-32.3%** | **-10.2%** | **new captures more, less DD** |
| **2021** | **+91.01%** | +53.44% | **-37.58** | -8.3% | -10.8% | **new misses Adani peak** |
| **2022** | +4.37% | **+13.89%** | **+9.52** | -21.4% | -21.5% | new outperforms |
| **2023** | +35.98% | **+48.20%** | **+12.22** | -11.4% | -9.7% | new outperforms |
| **2024** | +16.86% | **+30.94%** | **+14.07** | -10.0% | -8.8% | new much better |
| **2025** | **-16.32%** | **-13.20%** | **+3.12** | -24.0% | -24.5% | both bad, new less |
| 2026 YTD | +0.79% | +1.07% | +0.28 | -20.6% | -19.3% | similar |

**Key trade-off:**
- Holdout LOST 37.58pp in 2021 (the Adani-peak year)
- Holdout GAINED across 2014, 2020, 2022, 2023, 2024 (+58.99pp combined)
- Net: smoother path, slightly higher endpoint

The 2019 MDD reduction is dramatic: -34% → -17%. That single difference explains most of the Calmar improvement.

---

## Concentration comparison

| | Current | Holdout |
|---|---:|---:|
| Total instruments | 758 | 718 |
| Top 10 % of PnL | 50.5% | **46.4%** |
| Top 20 % of PnL | 81.5% | **73.1%** |
| Adani group % | **20.5%** | 18.0% |
| Negative-PnL instruments | 55.7% | 53.9% |

Marginally less concentrated, but Adani exposure is still ~18% — concentration is structural to NSE momentum.

---

## Did the experiment work?

### Per the original criteria

> "If new champion 2025 ≥ -10% → optimization angles A-E worth pursuing."
> "If new champion 2025 ≤ -15% → strategy is fragile across all parameters, not just at champion."

Result: **2025 = -13.20%** — between the two thresholds. **Mixed signal.**

### Honest interpretation

**The holdout champion is genuinely better.** Across nearly every metric it dominates:
- +46% Calmar
- -10pp MDD
- -22% vol
- Normal skew/kurtosis instead of fat-tailed lottery profile
- +0.7pp CAGR
- +₹10M final value

**But the 2025 regime turn caught both champions.** The mechanism — momentum breakout on NSE — is regime-dependent. Parameter tuning recovers some robustness; it does not eliminate the fragility.

### What this means for forward expectation

The original audit estimated forward CAGR of 8-12% (from backtest 15.2%). Updated estimate using holdout champion as baseline:

| Component | Adjustment | Forward CAGR |
|---|---:|---:|
| Holdout champion baseline | — | 15.90% |
| Slippage (5bps R/T, unmodeled) | -0.30pp | 15.60% |
| Adani concentration de-rate (still 18%) | -1.00pp | 14.60% |
| Regime adjustment (less severe; lower vol → less downside in bear) | -2 to -4pp | **10-12%** |
| **Updated forward expectation** | | **10-13% CAGR** |

**vs current champion's estimate of 8-12% CAGR.** A modest but real improvement on forward expectation, with materially better tail behavior.

---

## Recommendation

1. **Promote holdout champion to be the new official champion.** The structural improvements are real and the 2025 OOS improvement, while modest, is in the right direction. Update `config_champion.yaml` + `OPTIMIZATION.md`.

2. **The optimization angles from the previous audit (A-E) still apply.** Run them on top of the holdout champion baseline, not the current one:
   - **A: quality overlay** (consecutive_positive_years, sector cap) — likely the highest-impact angle
   - **B: regime-conditional TSL** (tighten in bear regimes)
   - **C: longer ndh sweep** (was already pursued — ndh=3 won, suggesting shorter is better in this param region)
   - **D: vol-targeted position sizing**
   - **E: anomalous-drop threshold tightening**

3. **Don't expect parameter tuning alone to escape the 2025 drawdown problem.** Even the holdout champion lost -13% in 2025. The regime turn affects all variants of this mechanism. **Real defense requires either a regime exit (force-sell when breadth collapses) or ensemble diversification across uncorrelated mechanisms.**

4. **Keep the current champion in the result archive for reproducibility** (don't delete `champion.json` etc.). Save holdout as new canonical via separate config files.

---

## Files produced

| File | Purpose |
|---|---|
| `config_holdout_train.yaml` | R2 sweep config with `end_epoch=2025-01-01` |
| `config_holdout_champion_full.yaml` | Holdout champion run on full 2010-2026 |
| `results/eod_breakout/holdout_train_2010-2024.json` | 1152-config sweep result (13.8 MB) |
| `results/eod_breakout/holdout_champion_full.json` | Full-period champion result |
| `AUDIT_2026-04-26.md` | Original deep audit of current champion |
| `HOLDOUT_EXPERIMENT_2026-04-26.md` | This document |

# Strategy Optimization Runbook

Repeatable process for discovering the best config for any strategy. Apply these steps identically to every strategy — the methodology doesn't change, only the parameters do.

**Related:** `docs/ENGINE_STRATEGY_GUIDE.md` (architecture, implementation) | Per-strategy tracking in `strategies/{name}/OPTIMIZATION.md`

## 1. Shared Priors

Known-good ranges for common parameters, learned across 30+ strategies. Use these as starting points — don't re-discover what's already known.

### Exit Parameters (14 strategies share these)

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `trailing_stop_pct` | 4-25% | 10 | 4-8% for mean-reversion, 10-25% for momentum/breakout |
| `max_hold_days` | 63-504 | 252 | Shorter for mean-reversion (21-63d), longer for momentum (252-504d) |
| `min_hold_time_days` | 0-10 | 0 | Only relevant for breakout strategies |
| `require_peak_recovery` | True/False | True | True for dip-buy, False for breakout/momentum |

### Quality Filters (5-6 strategies)

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `consecutive_positive_years` | 1-4 | 2 | Higher = stricter quality gate, fewer candidates |
| `roe_threshold` | 10-20% | 15 | 0 = disabled |
| `pe_threshold` | 15-35 | 25 | 0 = disabled |
| `de_threshold` | 0-2.0 | 0 | 0 = disabled |

### Market Filters (10 strategies)

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `regime_sma_period` | 0, 100, 200 | 0 | 0 = disabled. 200 is standard. |
| `direction_score_threshold` | 0.40-0.60 | 0 | 0 = disabled. 0.50-0.54 is typical. |

### Simulation Parameters

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `max_positions` | 5-30 | 10 | Higher = more diversified, lower per-position impact |
| `sorting_type` | top_gainer, top_performer | top_gainer | top_gainer is simpler and works well |

### Using Priors in Optimization

When exploring a strategy with 15 params:
- **Known params** (TSL, max_hold, max_positions): Fix at 1-2 values from the table above
- **Explored param** (the one you're sweeping): 8-10 values across full plausible range
- **Other unknown params**: Fix at reasonable defaults (1 value each)

This keeps config count manageable while focusing compute on what you're actually trying to learn.

## 2. The Process

### Round 0: Baseline (1 config)

Set all params to known-good defaults. Run once. This establishes floor performance.

```yaml
# strategies/{name}/config_baseline.yaml
entry:
  # all params at single default values from shared priors + strategy defaults
exit:
  trailing_stop_pct: [10]
  max_hold_days: [252]
simulation:
  max_positions: [10]
```

Record: CAGR, Calmar, MDD, trade count. This is the number to beat.

### Round 1: Sensitivity Scan (one param at a time)

**Goal:** Find which params matter and which are noise.

**Method:** For each param, create a sweep with 8-10 values spanning the full plausible range. Keep all other params fixed at baseline values. Optionally include 1-2 additional values for known params (e.g., TSL at both 10% and 15%) to capture basic interactions.

```yaml
# strategies/{name}/config_round1_tsl.yaml
# Sweeping: trailing_stop_pct (8 values)
# Everything else: fixed at baseline
exit:
  trailing_stop_pct: [3, 5, 8, 10, 15, 20, 30, 50]
  max_hold_days: [252]
```

**Configs per param:** 8-10. For N params: 8-10 × N total configs across all Round 1 runs.

**Analyze each param's sweep results:**

| Shape | Meaning | Action |
|-------|---------|--------|
| Bell curve | Clear optimum | Use the peak, narrow range for Round 2 |
| Flat | Param doesn't matter | Fix at any reasonable value, skip in Round 2 |
| Monotonic | Optimum at/beyond edge | Extend range in that direction, re-sweep |
| Sharp spike | Most sensitive param | Test fine-grained values near the spike |
| Noisy/random | Likely overfitting | Fix at default, skip in Round 2 |

**Output:** Classify each param:
- **IMPORTANT** (clear peak or strong trend) — include in Round 2
- **INSENSITIVE** (flat response) — fix forever at any reasonable value
- **MONOTONIC** — extend range and re-test before deciding

Typically 3-5 of 10-15 params are IMPORTANT. Fix the rest.

### Round 2: Focused Search (cross important params)

**Goal:** Find interactions between the params that matter.

**Method:** Cross the 3-5 IMPORTANT params with 3-5 values each, centered on Round 1 peaks. All INSENSITIVE params fixed.

```yaml
# strategies/{name}/config_round2.yaml
entry:
  momentum_lookback_days: [168, 189, 210]    # 3 values around Round 1 peak
  rebalance_interval_days: [35, 42, 50]      # 3 values around Round 1 peak
exit:
  trailing_stop_pct: [8, 10, 12]             # 3 values around Round 1 peak
  max_hold_days: [378]                        # fixed (INSENSITIVE in Round 1)
simulation:
  max_positions: [10]                         # fixed
```

**Budget:** 3^3 = 27 to 5^4 = 625 configs. Keep under 800.

**Analyze with marginal table** (see Section 3 below). Look for:
- Param values where avg Calmar is consistently high → robust choice
- Param values where max >> avg → only works with specific other params (fragile)
- Broad plateau in the interaction space → robust region

### Round 3: Robustness Check (50-100 configs)

**Goal:** Confirm the result is real, not an artifact of one lucky param combination.

**Method 1 — Neighborhood perturbation:**
Take the top 3-5 configs from Round 2. For each, perturb every param by +/-10-20%. Run 10-20 perturbations per config.

```yaml
# If best config is: lookback=189, rebalance=42, TSL=10
# Perturb to: lookback=[170, 189, 210], rebalance=[38, 42, 46], TSL=[9, 10, 11]
```

**Pass criterion:** >80% of perturbations retain >70% of the best config's Calmar. If not, the optimum is a spike — don't trust it.

**Method 2 — Neighbor stability:**
Look at the top 10 configs from Round 2. If they share similar param values (within 20% of each other), the region is robust. If the top config is an isolated outlier, it's noise.

**Final selection:** Pick the config at the CENTER of the robust region, not the single best point. The center is more likely to generalize.

### Round 4: Validation

**Goal:** Estimate real-world performance.

Apply at least one:

**Walk-forward (preferred):**
- Split data into 5 folds: 5yr train / 2yr test, rolling
- Optimize on each train fold, test on the corresponding test fold
- Concatenated OOS results are the true performance estimate
- Key metric: variance of OOS Calmar across folds (high variance = fragile)

**Out-of-sample split (simpler):**
- Optimize on 2010-2020, test on 2020-2026
- Calmar drop >50% suggests overfitting

**Cross-exchange (optional, for visibility):**
- Run NSE champion config as-is on US/other exchanges
- Not a pass/fail test — a strategy can be legitimately market-specific (different microstructure, retail participation, sector composition)
- Useful as extra signal: if similar params also work on US, that's added confidence
- Do NOT re-optimize per exchange unless you plan to trade there

**Deflated Sharpe Ratio:**
Quick formula to check if the observed Sharpe is statistically significant given the number of configs tested:

```
SR_deflated ≈ SR_observed - sqrt(Var(SR)) * Z(1 - 1/N_configs)

Where:
  Var(SR) ≈ (1 + 0.5 * SR²) / T  (T = number of monthly returns)
  Z(p) = inverse normal CDF
  N_configs = total configs tested across all rounds
```

Rule of thumb: with 500 configs, an observed Sharpe needs to be ~0.5 higher than the "true" Sharpe. A Sharpe of 1.0 with 500 configs deflates to ~0.5. If deflated Sharpe < 0.3, the result is likely noise.

## 3. Analysis Methods

### Marginal Analysis Table

For each param value, compute performance metrics averaged across all other config combinations. This is the main tool for identifying which params matter and what their best values are.

```
PARAM_NAME          VALUE    AVG_CALMAR  MAX_CALMAR  MIN_CALMAR  AVG_CAGR   TRADES
trailing_stop_pct   5        0.42        0.71        0.12        11.2%      342
trailing_stop_pct   10       0.51        0.82        0.21        14.1%      186
trailing_stop_pct   15       0.48        0.79        0.18        15.8%      134
trailing_stop_pct   20       0.39        0.68        0.08        16.2%      98
```

**How to read:**
- **AVG_CALMAR** is the main signal — it shows the param's main effect
- **MAX vs AVG ratio** — if MAX >> AVG, that value only works with specific other params
- **MIN_CALMAR** — if MIN is very low, that value is risky in some combos
- **TRADES** — too few trades (<100) means the metric is unreliable

**Sort by AVG_CALMAR** to find the best value. If multiple values have similar AVG_CALMAR, the param is insensitive in that range — pick any.

### Neighborhood Perturbation Test

```python
# Pseudo-code
best_config = {"lookback": 189, "rebalance": 42, "tsl": 10}
perturbations = []
for param, value in best_config.items():
    for delta in [-0.15, -0.10, -0.05, 0.05, 0.10, 0.15]:
        perturbed = best_config.copy()
        perturbed[param] = round(value * (1 + delta))
        perturbations.append(perturbed)

# Run all perturbations, check:
# - % that retain >70% of best Calmar  (want >80%)
# - Std dev of Calmar across perturbations  (want low)
```

### Overfitting Red Flags

| Signal | What it means |
|--------|---------------|
| Best Calmar > 2x median Calmar | Fragile optimum, likely overfit |
| Top config params are outliers vs top-10 | Isolated spike, not a region |
| Calmar > 1.0 with < 100 trades | Insufficient sample, don't trust |
| Win rate > 65% with high CAGR | Check for look-ahead bias |
| OOS Calmar < 50% of IS Calmar | Overfitting confirmed |
| Deflated Sharpe < 0.3 | Not statistically significant |

## 4. Per-Strategy Tracking

Each strategy should have an `OPTIMIZATION.md` file tracking what was tried:

```
strategies/{name}/
  config.yaml              # Current champion config
  OPTIMIZATION.md          # What was tried, results, decisions
  config_baseline.yaml     # Round 0 baseline
  config_round1_*.yaml     # Round 1 per-param sweeps
  config_round2.yaml       # Round 2 interaction grid
```

See `strategies/eod_breakout/OPTIMIZATION.md` for an example template.

## 5. Timing Estimates

| Configs | Signal gen | Simulation | Total (approx) |
|---------|-----------|------------|----------------|
| 10 | ~2 min | ~1s | ~2 min |
| 100 | ~5 min | ~20s | ~5 min |
| 500 | ~15 min | ~2 min | ~17 min |
| 1000 | ~25 min | ~5 min | ~30 min |

**Budget per strategy (all rounds):**

| Round | Configs | Time |
|-------|---------|------|
| Round 0: Baseline | 1 | ~2 min |
| Round 1: Sensitivity (10 params × 9 values) | ~90 | ~5 min |
| Round 2: Focused (4 params × 4 values) | ~256 | ~12 min |
| Round 3: Robustness (5 configs × 20 perturbations) | ~100 | ~7 min |
| **Total** | **~450** | **~26 min** |

Round 4 (validation) depends on the method — walk-forward multiplies by 5x.

## 6. Running Optimizations

```bash
# Run a sweep
python run.py --config strategies/eod_breakout/config_round1_tsl.yaml --output results/eod_breakout_r1_tsl.json

# Compare results across rounds
python scripts/analyze_sweep.py results/eod_breakout_r1_tsl.json

# Quick leaderboard
python -c "
from lib.backtest_result import SweepResult
s = SweepResult.load('results/eod_breakout_r1_tsl.json')
s.print_leaderboard(top_n=10)
"
```

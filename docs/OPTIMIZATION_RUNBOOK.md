# Strategy Optimization Runbook

Repeatable process for discovering the best config for any strategy. Apply these steps identically to every strategy — the methodology doesn't change, only the parameters do.

**Related:** `docs/ENGINE_STRATEGY_GUIDE.md` (architecture, implementation) | Per-strategy tracking in `strategies/{name}/OPTIMIZATION.md`

## 0. Critical Rules

1. **Track BOTH best-CAGR and best-Calmar configs** at every round. Never discard a high-CAGR config because it has lower Calmar. Report both leaderboards.
2. **Cross ALL sweepable params in R2** — entry, exit, AND simulation (including `order_sorting_type`). Do NOT sweep params sequentially and lock them. Interactions matter.
3. **Never permanently fix a param after R1.** "Insensitive" in R1 may be important when other params change. Include at least 2 values for every param in R2.
4. **direction_score is a compound param.** Test as complete `{n_day_ma, score}` configs, not two independent sweeps. Note: ds_ma=1 produces zero entries (code bug — `close > 1-day MA of close` is always false). Minimum useful value is 2.
5. **Run on both time windows.** Full period (2010-2026) for robustness, plus 2015-2025 (ATO_Simulator window) for CAGR comparison. Report both.

## 1. Shared Priors

Known-good ranges learned from eod_breakout optimization (1500+ configs tested). Use as starting points.

### Universal Filters (use with EVERY strategy)

These filters are the most impactful across all strategies tested. Include them in every strategy config, even if the strategy doesn't explicitly require them.

| Filter | Config | Why it matters |
|--------|--------|----------------|
| **direction_score** | `{n_day_ma: 3, score: 0.54}` | Market breadth gate. Only enter when >54% of stocks are above 3-day MA. Without it, you enter in bear markets. This is the single most important binary filter. Score=0.40 is too loose (barely filters). ds_ma=3 is the sweet spot (2 is too noisy, 5+ is too slow). |
| **Scanner liquidity** | `avg_txn > 70M, price > 50` | Prevents entries in illiquid stocks where slippage dominates. Already shared across all strategies. |
| **trailing_stop_pct** | 8-15% | Most impactful continuous param. 8% for breakout (tighter control), 15% for momentum (let winners run). Below 5% = whipsaw. Above 20% = drawdowns run. |
| **order_sorting_type** | Sweep it, don't assume | Determines which signals get priority. top_gainer for breakout/momentum. top_dipper for dip-buy. Must be swept — often a top-3 param. Available: top_gainer, top_performer, top_average_txn, top_dipper. |

### Exit Parameters (14 strategies share these)

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `trailing_stop_pct` | 4-25% | 10 | 4-8% for mean-reversion, 8-15% for breakout, 10-25% for long momentum |
| `max_hold_days` | 63-504 | 252 | Shorter for mean-reversion (21-63d), longer for momentum (252-504d) |
| `min_hold_time_days` | 0-10 | 0 | Generally insensitive. 0 is fine for most strategies. |
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
| `direction_score` | `{n_day_ma: 3, score: 0.54}` | disabled | **Recommended for all strategies.** See Universal Filters above. |

### Simulation Parameters

| Parameter | Known-good range | Default | Notes |
|-----------|-----------------|---------|-------|
| `max_positions` | 5-30 | 15 | 15-20 is optimal for most strategies. 5 = concentrated risk, 30+ = diluted alpha |
| `order_sorting_type` | all 4 types | top_gainer | **MUST sweep.** top_gainer for breakout/momentum, top_dipper for dip-buy |

## 2. The Process

**This process is iterative, not a single pass.** Rounds are numbered for structure, but expect to loop back:
- Round 1 monotonic results → extend range and re-sweep that param before moving to Round 2
- Round 2 reveals a param you dismissed in Round 1 → go back and sweep it properly
- Round 3 fails robustness → revisit Round 2 with a wider grid or different param set
- A strategy may span multiple sessions. Track state in `strategies/{name}/OPTIMIZATION.md` so any session can pick up where the last left off.

### Round 0: Baseline (1 config)

Set all params to known-good defaults. Run once. This establishes floor performance.

```yaml
# strategies/{name}/config_baseline.yaml
entry:
  # all params at single default values from shared priors + strategy defaults
  direction_score:
    - {n_day_ma: 3, score: 0.54}    # always include
exit:
  trailing_stop_pct: [10]
  max_hold_days: [252]
simulation:
  order_sorting_type: [top_gainer]
  max_positions: [15]
```

Record: CAGR, Calmar, MDD, trade count. This is the number to beat.

### Round 1: Sensitivity Scan (one param at a time)

**Goal:** Find which params matter and which are noise.

**Scope: ALL sweepable parameters.** This includes entry params, exit params, AND simulation params. In particular:
- `order_sorting_type` MUST be swept: [top_gainer, top_performer, top_average_txn, top_dipper]
- `direction_score` variants MUST be swept as compound configs
- `max_positions` MUST be swept

**Method:** For each param, create a sweep with 8-10 values spanning the full plausible range. Keep all other params fixed at baseline values.

```yaml
# strategies/{name}/config_round1_tsl.yaml
# Sweeping: trailing_stop_pct (8 values)
# Everything else: fixed at baseline
exit:
  trailing_stop_pct: [3, 5, 8, 10, 15, 20, 30, 50]
  max_hold_days: [252]
```

**Configs per param:** 8-10. For N params: 8-10 x N total configs across all Round 1 runs.

**Analyze each param's sweep results:**

| Shape | Meaning | Action |
|-------|---------|--------|
| Bell curve | Clear optimum | Use the peak, narrow range for Round 2 |
| Flat | Param doesn't matter in isolation | Still include 2 values in Round 2 (may interact) |
| Monotonic | Optimum at/beyond edge | Extend range in that direction, re-sweep |
| Sharp spike | Most sensitive param | Test fine-grained values near the spike |
| Noisy/random | Likely overfitting | Fix at default, skip in Round 2 |

**Output:** Classify each param as IMPORTANT, MODERATE, or INSENSITIVE. But remember: INSENSITIVE params still get 2 values in Round 2.

### Round 2: Full Cross-Parameter Search

**Goal:** Find the best config by crossing ALL params together.

**Method:** Cross ALL sweepable params. IMPORTANT params get 3-5 values centered on R1 peaks. MODERATE and INSENSITIVE params get at least 2 values (R1 best + baseline default). This is critical — param interactions can change what looks "insensitive" in isolation.

```yaml
# strategies/{name}/config_round2.yaml — cross EVERYTHING
entry:
  momentum_lookback_days: [168, 189, 210]    # IMPORTANT: 3 values
  rebalance_interval_days: [35, 42, 50]      # IMPORTANT: 3 values
  direction_score:                             # Compound: test as complete configs
    - {n_day_ma: 3, score: 0.54}
    - {n_day_ma: 5, score: 0.40}
exit:
  trailing_stop_pct: [8, 10, 15]             # IMPORTANT: 3 values
  max_hold_days: [252, 378]                   # INSENSITIVE: still include 2 values
simulation:
  order_sorting_type: [top_gainer, top_performer]  # MUST sweep
  max_positions: [10, 15, 20]                # IMPORTANT: 3 values
```

**Budget:** Target 500-2000 configs. Simulation is ~1s/config, but signal generation scales with entry combos. A 1000-config sweep typically takes 30-120 min depending on entry complexity.

**Analysis — report TWO leaderboards:**

1. **Top 20 by CAGR** — the configs that make the most money
2. **Top 20 by Calmar** — the configs with best risk-adjusted returns

Also produce marginal analysis tables (see Section 3) sorted by both AVG_CAGR and AVG_CALMAR.

**After R2, run the top configs on the 2015-2025 window** to compare with ATO_Simulator benchmarks.

### Round 3: Robustness Check

**Goal:** Confirm the result is real, not an artifact of one lucky param combination.

**Method 1 — Fine grid around winner:**
Take the best config from R2. Run a fine grid of the top 2-3 entry params (e.g., ndh=[best-2..best+2] x ndm=[best-2..best+2]) with 2 values each for exit/sim params.

**Method 2 — Neighborhood perturbation:**
Take the top 3-5 configs from Round 2. For each, perturb every param by +/-10-20%.

**Pass criterion:** >80% of perturbations retain >70% of the best config's Calmar. If not, the optimum is a spike — don't trust it.

**Method 3 — Neighbor stability:**
Look at the top 10 configs from Round 2. If they share similar param values (within 20% of each other), the region is robust. If the top config is an isolated outlier, it's noise.

### Round 4: Validation

**Goal:** Estimate real-world performance.

**All four tests below are REQUIRED.** Do not skip any.

**4a. Out-of-sample split:**
- Optimize on 2010-2020, test on 2020-2026
- Calmar drop >50% suggests overfitting

**4b. Walk-forward:**
- Split data into 5-6 rolling folds: ~3yr train / ~2yr test
- Run champion config on each test fold (fixed params, not re-optimized)
- Key metrics: avg OOS Calmar, std dev, # positive folds
- High variance (std dev > 0.5) or <60% positive folds = fragile

**4c. Cross-data-source:**

Run the champion config against alternative NSE data sources:
- `nse.nse_charting_day` — primary (used for optimization)
- `fmp.stock_eod` with `.NS` suffix — FMP's NSE data (known quality issues)
- `nse.nse_bhavcopy_historical` — unadjusted prices

**4d. Cross-exchange (ALL listed exchanges):**

Run the champion config on ALL exchanges below via fmp.stock_eod:
- US, UK, Canada, China (SHH, SHZ), Euronext, Hong Kong, South Korea, Germany, Saudi Arabia, Taiwan
- Not a pass/fail test — a strategy can be legitimately market-specific
- But ALL must be run for the record

**Deflated Sharpe Ratio:**
```
SR_deflated ≈ SR_observed - sqrt(Var(SR)) * Z(1 - 1/N_configs)

Where:
  Var(SR) ≈ (1 + 0.5 * SR²) / T  (T = number of monthly returns)
  Z(p) = inverse normal CDF
  N_configs = total configs tested across all rounds
```

Rule of thumb: with 500 configs, an observed Sharpe needs to be ~0.5 higher than the "true" Sharpe. If deflated Sharpe < 0.3, the result is likely noise.

## 3. Analysis Methods

### Marginal Analysis Table

For each param value, compute performance metrics averaged across all other config combinations.

```
PARAM_NAME          VALUE    AVG_CAGR  MAX_CAGR  AVG_CALMAR  MAX_CALMAR  TRADES
trailing_stop_pct   5         7.1%     11.2%     0.185       0.276       2680
trailing_stop_pct   8         8.2%     13.3%     0.289       0.425       1624
trailing_stop_pct   15       10.6%     13.3%     0.344       0.461        607
```

**How to read:**
- **AVG_CAGR** — average return for this param value across all other param combos
- **AVG_CALMAR** — average risk-adjusted return
- **MAX_CAGR vs AVG_CAGR** — if MAX >> AVG, that value only works with specific combos (fragile)
- **TRADES** — too few trades (<100) means the metric is unreliable

**Produce BOTH a CAGR heatmap and a Calmar heatmap** when crossing 2 entry params (e.g., ndh x ndm). This reveals the response surface and where the robust region is.

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
  config_champion.yaml     # Champion config (standalone)
  OPTIMIZATION.md          # What was tried, results, decisions
  config_baseline.yaml     # Round 0 baseline
  config_round1_*.yaml     # Round 1 per-param sweeps
  config_round2.yaml       # Round 2 full cross
```

See `strategies/eod_breakout/OPTIMIZATION.md` for a complete example.

## 5. Timing Estimates

| Configs | Signal gen | Simulation | Total (approx) |
|---------|-----------|------------|----------------|
| 10 | ~2 min | ~1s | ~2 min |
| 100 | ~5 min | ~20s | ~5 min |
| 500 | ~15 min | ~10 min | ~25 min |
| 1000 | ~25 min | ~20 min | ~45 min |
| 1000 (with top_performer) | ~25 min | ~90 min | ~2 hours |

Note: `top_performer` sorting is much slower than `top_gainer` because it computes walk-forward instrument scores. Budget 3-5x more time when crossing sorting types.

**Budget per strategy (all rounds):**

| Round | Configs | Time |
|-------|---------|------|
| Round 0: Baseline | 1 | ~2 min |
| Round 1: Sensitivity (8 params x 8 values) | ~64 | ~5 min |
| Round 2: Full cross (all params) | ~500-2000 | ~30-120 min |
| Round 3: Fine grid / perturbation | ~100-200 | ~10-30 min |
| Round 4: Validation (OOS + WF + cross) | ~25 | ~30 min |
| **Total** | **~700-2400** | **~1.5-3 hours** |

## 6. Running Optimizations

### Result naming convention

Save results to `results/{strategy}/round{N}_{description}.json`:

```
results/eod_breakout/round0_baseline.json
results/eod_breakout/round1_tsl.json
results/eod_breakout/round1_sorting.json
results/eod_breakout/round2_full.json
results/eod_breakout/round3_fine_grid.json
results/eod_breakout/round4_oos.json
results/eod_breakout/champion.json
results/eod_breakout/champion_2015.json
```

### Commands

```bash
# Run a sweep
python run.py --config strategies/eod_breakout/config_round2.yaml \
  --output results/eod_breakout/round2_full.json

# View optimization status across ALL strategies
python scripts/optimization_status.py
python scripts/optimization_status.py --verbose  # per-round detail
```

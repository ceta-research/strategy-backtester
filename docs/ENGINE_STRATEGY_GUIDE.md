# Strategy Backtester: Engine Strategy Guide

How to implement, configure, and optimize strategies in the strategy-backtester pipeline.

## 1. Architecture Overview

```
YAML Config → Data Fetch → Signal Generation → Scanner Filter → Order Ranking → Simulation → Metrics
```

**Key files:**
- `engine/pipeline.py` -- orchestrator
- `engine/signals/*.py` -- signal generators (one per strategy)
- `engine/config_loader.py` -- YAML parsing + config builders
- `engine/simulator.py` -- position-level simulation with broker charges
- `engine/signals/base.py` -- shared utilities (TSL, scanner, direction score, fundamentals)
- `lib/backtest_result.py` -- BacktestResult + SweepResult

**Config structure:**
```yaml
static:     # Fixed params: strategy_type, start/end epoch, capital, data provider
scanner:    # Liquidity filter: exchange, price threshold, avg turnover
entry:      # Strategy-specific entry params (each value is a LIST for sweeping)
exit:       # Exit params: TSL, max hold days
simulation: # Position sizing: max_positions, order_value, sorting
```

Total configs = product of all list lengths across all sections.

## 1b. Pipeline vs Standalone Scripts

There are two ways to run a strategy: the **engine pipeline** (`engine/signals/`) and **standalone scripts** (`scripts/`). They serve different purposes.

| | Pipeline (`engine/signals/`) | Standalone (`scripts/`) |
|---|---|---|
| **Results** | Honest (per-day liquidity filter) | Upper bound (no liquidity filter) |
| **Config sweeps** | Automatic via YAML lists | Manual (`itertools.product` loops) |
| **Metrics** | Standardized across all 30 strategies | Each script reimplements its own |
| **Data** | CRDataProvider or Bhavcopy (pipeline-managed) | Any source (custom fetch code) |
| **Flexibility** | Must fit `generate_orders()` protocol | Arbitrary Python, any logic |
| **Debugging** | Harder (runs all configs in one pass) | Easier (single file, run directly) |
| **Setup** | 3 touchpoints (signal file, config builder, pipeline import) | Single self-contained file |
| **Portability** | Needs the engine installed | Copy-paste and run |

**When to use which:**

- **Standalone first** for rapid prototyping and exploring a new idea. No boilerplate, total flexibility. Good for: multi-asset pairs, index-level strategies, custom cost models, one-off experiments.
- **Pipeline** once the strategy works and you want honest results with config sweeps. Good for: systematic optimization, comparing strategies, production-quality results.

**Typical workflow:** Prototype in `scripts/`, validate the idea, then port to `engine/signals/` for honest evaluation. Example: `scripts/momentum_dip_buy.py` (standalone champion, Calmar 1.01) was ported to `engine/signals/momentum_dip_quality.py` (pipeline result was lower due to per-day liquidity filtering).

**Why pipeline results are lower:** The scanner phase checks that each stock has sufficient daily turnover on the day of entry. Standalone scripts typically filter by average turnover across the whole period, so they allow entries on low-liquidity days that wouldn't be tradeable in practice.

## 2. Implementing a New Strategy

### Step 1: Create signal generator

Create `engine/signals/my_strategy.py`:

```python
import time
import polars as pl
from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import (
    register_strategy, add_next_day_values, run_scanner,
    walk_forward_exit, finalize_orders, build_regime_filter,
    compute_direction_score,
)

class MyStrategySignalGenerator:

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "lookback_days": entry_cfg.get("lookback_days", [10]),
            "threshold_pct": entry_cfg.get("threshold_pct", [5]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [252]),
        }

    def generate_orders(self, context, df_tick_data):
        # ... signal generation logic ...
        return finalize_orders(all_order_rows, elapsed)

register_strategy("my_strategy", MyStrategySignalGenerator)
```

Required output columns: `instrument, entry_epoch, exit_epoch, entry_price, exit_price, entry_volume, exit_volume, scanner_config_ids, entry_config_ids, exit_config_ids`

### Step 2: Register the import

Add one line to `engine/signals/__init__.py`:

```python
from engine.signals import my_strategy  # noqa: F401
```

### Step 3: Create YAML config

Create `strategies/my_strategy/config.yaml`. See section 3 for sweep methodology.

### Step 4: Run

```bash
python run.py --strategy my_strategy
python run.py --config strategies/my_strategy/config.yaml --output results.json
```

## 3. Parameter Optimization Methodology

### The Problem

Every parameter combination you test increases the risk of overfitting. With 1000 configs, the best result's apparent Sharpe is inflated by ~3.7 standard deviations (Bailey & Lopez de Prado). A strategy that "works" in 1 out of 1000 configs is noise, not alpha.

### Rules of Thumb

| Guideline | Rule |
|-----------|------|
| Min trades per config | 200+ for meaningful Sharpe/Calmar |
| Max configs (conservative) | sqrt(total_trades). 400 trades -> max ~20 configs |
| Max configs (practical) | Keep under 500 per phase. Document why. |
| Overfitting red flag | Best config Calmar is 2x+ the median config Calmar |
| Robustness check | Top config's neighbors should also perform well |

### Three-Phase Sweep

#### Phase 1: Dimensional Scan (isolate what matters)

**Goal:** Find which parameters matter and which are noise.

**Method:** Fix all parameters at reasonable defaults. Sweep ONE parameter at a time across its full plausible range (8-10 values, from extreme low to extreme high). This produces N_params x ~9 configs per run.

```yaml
# Example: sweep momentum lookback while fixing everything else
entry:
  momentum_lookback_days: [21, 63, 126, 189, 252, 378, 504]  # SWEEP THIS
  top_n_pct: [0.20]           # fixed
  rebalance_interval_days: [42]  # fixed
  # ... all other params fixed at single values
```

**What to look for:**
- Parameters that produce a clear peak (bell curve) = important, use the peak
- Parameters that are flat = insensitive, fix at any reasonable value
- Parameters that are monotonic = the optimum may be at the edge you tested, extend the range
- Parameters with noisy/random pattern = likely overfitting, fix at default

**Budget:** ~8-10 configs per parameter x N_params. For 8 params, that's ~64-80 configs total.

**Real example (momentum_top_gainers, 7 params, 762 total Phase 1 configs):**

```
LOOKBACK (21d-504d)  : bell curve, peak 189d        -> IMPORTANT
REBALANCE (1d-252d)  : sharp spike at 42d           -> MOST IMPORTANT
TSL (2%-60%)         : monotonic CAGR, Calmar peak  -> IMPORTANT
HOLD (21d-1008d)     : peak at 378d                 -> MODERATE
POSITIONS (1-40)     : monotonically increasing!     -> IMPORTANT (extend range)
TOP N% (2%-70%)      : flat from 10-50%             -> INSENSITIVE (fix at 25%)
DIRECTION (0-0.70)   : peak at 0.45-0.50            -> MODERATE
```

**Interpreting shapes:**
- **Bell curve** (lookback, direction): clear optimum, use the peak
- **Sharp spike** (rebalance): most important param, test fine-grained values nearby
- **Monotonic** (positions): optimum is at/beyond the range edge, extend it
- **Flat** (top_n%): parameter doesn't matter, fix at any reasonable value
- **Monotonic then plateau** (TSL): higher CAGR but diminishing Calmar returns

#### Phase 2: Interaction Sweep (find the best combo)

**Goal:** Test interactions between the 2-4 parameters that mattered in Phase 1.

**Method:** Cross the important parameters with 3-5 values each around their Phase 1 peaks. Keep all unimportant parameters fixed.

```yaml
# Example: cross the 3 params that mattered
entry:
  momentum_lookback_days: [168, 189, 210]    # 3 values around peak
  rebalance_interval_days: [35, 42, 50]      # 3 values around peak
  direction_score_threshold: [0.40, 0.45, 0.50]  # 3 values around peak
  # everything else: fixed at single values
exit:
  tsl_pct: [18, 20, 22]  # 3 values if TSL was important
  max_hold_days: [378]    # fixed if not important
simulation:
  max_positions: [12]     # fixed if not important
```

**Budget:** 3^3 to 5^3 = 27 to 125 configs for 3 important params. Up to 500 if 4 params.

**Analysis:** Look at parameter importance (average Calmar by parameter value). Confirm Phase 1 findings hold in combination. Check robustness: the top config's neighbors should have similar performance.

#### Phase 3: Validation (is it real?)

**Goal:** Confirm the result is not overfit.

**Methods (pick at least one):**

1. **Out-of-sample split:** Optimize on 2010-2020, test on 2020-2026. If Calmar drops >50%, suspect overfitting.
2. **Walk-forward:** Rolling 5yr train / 2yr test windows. Concatenated OOS results are the true estimate.
3. **Neighbor stability:** Check top-10 configs. If they all share similar parameters and performance, the result is robust. If the top config is an isolated spike, it is likely noise.
4. **Multiple exchanges:** If the strategy works on NSE, does a similar parameterization work on US/other exchanges?

### Worked Example: momentum_top_gainers Optimization

**Phase 1 (762 configs, 4 parallel 1D sweeps):**
Each sweep varies one dimension across extreme-to-extreme range. Results:
- Identified 3 important params: lookback (bell curve at 189d), rebalance (spike at 42d), TSL (peak Calmar at 22%)
- Identified 1 monotonic param: positions (keeps improving to 40, needs extended range)
- Identified 2 insensitive params: top_n% (flat 10-50%), hold (broad peak 126-504d)
- Identified 1 moderate param: direction threshold (peak at 0.45-0.50)

**Phase 2 (972 configs, crossing important params):**
Swept lookback x top_n x rebalance x direction_threshold (3-5 values each near Phase 1 peaks), with TSL and positions. Confirmed:
- 42d rebalance dominates (avg Calmar 0.52 vs 0.27-0.32 for others)
- Direction threshold 0.50 optimal
- 189d lookback confirmed

**Result: 3 config profiles emerged:**

| Profile | CAGR | MDD | Calmar | Trades | Key params |
|---------|------|-----|--------|--------|------------|
| Aggressive | 20.2% | -24.6% | 0.82 | 134 | TSL=35%, pos=12 |
| Balanced | 17.8% | -22.6% | 0.79 | 186 | TSL=22%, pos=12 |
| Conservative | 15.3% | -19.3% | 0.79 | 408 | TSL=22%, pos=30 |

All three share: mom=189d, rebal=42d, dir>0.45-0.50, hold=378d.

### How Many Configs Per Run?

The bottleneck is data fetch (~60s) + signal generation (~1-10s per entry config) + simulation (~0.1-0.5s per config).

| Total configs | Signal gen time | Sim time | Total (approx) |
|---------------|-----------------|----------|-----------------|
| 12 | ~2 min | ~2s | ~2 min |
| 100 | ~5 min | ~20s | ~5 min |
| 400 | ~10 min | ~2 min | ~12 min |
| 1000 | ~20 min | ~5 min | ~25 min |
| 3000 | ~60 min | ~15 min | ~75 min |

**Practical limit:** 500-1000 configs per run is comfortable. Beyond 2000, consider splitting into focused runs.

**Signal gen scaling:** Each unique entry config re-computes indicators and walks forward all orders. Exit configs are cheap (same orders, different walk-forward params). So 3 entry configs x 100 exit configs (300 total) is much faster than 100 entry configs x 3 exit configs (also 300 total).

## 4. Common Patterns

### Direction Score Filter

Market breadth filter that gates entries based on what fraction of stocks are above their N-day MA. Ported from ATO_Simulator.

```python
from engine.signals.base import compute_direction_score

# Pre-compute once
direction_scores = compute_direction_score(df_tick_data, n_day_ma=3)
# direction_scores[epoch] = 0.65 means 65% of stocks in uptrend

# Gate entries
if direction_scores.get(epoch, 0) <= threshold:
    continue  # skip entry on bearish days
```

Config params: `direction_score_n_day_ma` (MA period), `direction_score_threshold` (min fraction, 0=disabled).

### Regime Filter

Benchmark must be above its SMA for entries to be allowed.

```python
from engine.signals.base import build_regime_filter
bull_epochs = build_regime_filter(df_tick_data, "NSE:NIFTYBEES", 200)
if epoch not in bull_epochs:
    continue
```

### Walk-Forward Exit (TSL)

Trailing stop-loss with optional peak recovery gate.

```python
from engine.signals.base import walk_forward_exit
exit_epoch, exit_price = walk_forward_exit(
    epochs, closes, start_idx,
    entry_epoch, entry_price, peak_price,
    tsl_pct=0.20, max_hold_days=378,
    opens=opens,
    require_peak_recovery=False,  # True for dip-buy, False for breakout/momentum
)
```

- `require_peak_recovery=True`: TSL only activates after price recovers to `peak_price` (dip-buy strategies)
- `require_peak_recovery=False`: TSL active immediately from entry (breakout/momentum strategies)

### Period-Average Turnover Filter

Fixed universe based on average turnover across the entire sim range. Matches standalone behavior.

```python
period_avg = (
    df_ind.group_by("instrument").agg(
        (pl.col("close") * pl.col("volume")).mean().alias("avg_turnover"),
        pl.col("close").mean().alias("avg_close"),
    )
    .filter((pl.col("avg_turnover") > 70_000_000) & (pl.col("avg_close") > 50))
)
period_universe_set = set(period_avg["instrument"].to_list())
```

### MOC Execution Model

Signal evaluated at close[T], entry at open[T+1]. This is the standard for honest backtesting. Use `add_next_day_values()` to get next-day open/volume.

## 5. Registered Strategies

| Strategy | Type | Key Idea |
|----------|------|----------|
| `eod_technical` | Breakout | N-day high + direction score |
| `momentum_dip_quality` | Dip-buy | Quality + momentum universe, buy dips, TSL exit |
| `momentum_top_gainers` | Momentum | Buy trailing top gainers at rebalance, TSL exit |
| `momentum_rebalance` | Momentum | Pure Jegadeesh-Titman, exit at next rebalance |
| `momentum_cascade` | Momentum | Accelerating momentum + breakout |
| `connors_rsi` | Mean reversion | RSI(2) oversold + SMA trend |
| `enhanced_breakout` | Breakout | Multi-layer: quality + momentum + volume + fundamentals |
| `quality_dip_buy` | Dip-buy | Quality gate + dip from peak |
| `factor_composite` | Factor | Multi-factor ranking (momentum + profitability + value) |

See `engine/signals/` for full list (~30 strategies).

## 6. Interpreting Results

### Key Metrics

| Metric | Good | Great | What it means |
|--------|------|-------|---------------|
| CAGR | >10% | >15% | Compound annual growth (after costs) |
| Max Drawdown | >-30% | >-20% | Worst peak-to-trough decline |
| Calmar | >0.3 | >0.5 | CAGR / abs(MDD). Risk-adjusted return |
| Sharpe | >0.7 | >1.0 | Return per unit volatility |
| Sortino | >1.0 | >1.5 | Return per unit downside volatility |
| Win Rate | >45% | >55% | Fraction of profitable trades |

### Red Flags

- Calmar >1.0 with <100 trades: likely overfit or data artifact
- Win rate >65% with high CAGR: suspiciously good, check for look-ahead bias
- Huge gap between top config and median: fragile optimum, overfitting risk
- Works only on one exchange/period: regime-specific, not generalizable

## 7. File Structure

```
strategy-backtester/
  engine/
    pipeline.py              # Main orchestrator
    config_loader.py         # YAML config parsing
    config_sweep.py          # Cartesian product iterator
    simulator.py             # Position simulation
    ranking.py               # Order ranking
    data_provider.py         # CR API / Bhavcopy / NSE data
    signals/
      base.py                # Protocol + shared utilities
      momentum_top_gainers.py
      momentum_dip_quality.py
      ...
  strategies/
    momentum_top_gainers/
      config_champion.yaml   # Best single config
      config.yaml            # Standard sweep
      config_sweep_*.yaml    # 1D parameter sweeps
    ...
  lib/
    backtest_result.py       # BacktestResult + SweepResult
    cr_client.py             # Ceta Research API client
  run.py                     # CLI entry point
```

# Engine Strategy Guide

Rules for building, testing, and maintaining strategies on the engine pipeline.

## Architecture

```
engine/
  pipeline.py              Orchestrator. Loads data, calls signal gen, ranks, simulates. DO NOT MODIFY.
  simulator.py             Position management, charges, slippage, MTM. DO NOT MODIFY.
  ranking.py               Order sorting (top_gainer, top_dipper, etc). DO NOT MODIFY.
  data_provider.py         Data loading (CRDataProvider, Bhavcopy, NseCharting). DO NOT MODIFY.
  config_loader.py         Shared YAML parsing + dispatch to signal generator builders. DO NOT MODIFY.
  config_sweep.py          Cartesian product of config params. DO NOT MODIFY.
  charges.py               Exchange-specific transaction costs. DO NOT MODIFY.
  constants.py             Shared constants. DO NOT MODIFY.

  signals/
    __init__.py            Imports all signal generators (add yours here). APPEND ONLY.
    base.py                Protocol + shared utilities. DO NOT MODIFY (see exceptions below).
    my_strategy.py         YOUR strategy. All logic + config schema lives here.

strategies/
  my_strategy/
    config_nse.yaml        YOUR config. All parameter values live here.
```

### The contract

Every signal generator implements three methods:

```python
def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame

@staticmethod
def build_entry_config(entry_cfg: dict) -> dict   # YAML entry section -> config dict

@staticmethod
def build_exit_config(exit_cfg: dict) -> dict      # YAML exit section -> config dict
```

**Receives**: `df_tick_data` with columns `[instrument, date_epoch, open, high, low, close, volume, average_price, exchange, symbol]`. Raw OHLCV. No pre-filtering except date range (start - prefetch to end).

**Returns**: DataFrame with required columns:

```python
ORDER_COLUMNS = [
    "instrument",           # "NSE:RELIANCE"
    "entry_epoch",          # epoch of entry (next-day open for MOC)
    "exit_epoch",           # epoch of exit
    "entry_price",          # entry price
    "exit_price",           # exit price
    "entry_volume",         # volume on entry day (for liquidity checks)
    "exit_volume",          # volume on exit day
    "scanner_config_ids",   # which scanner config qualified this entry
    "entry_config_ids",     # which entry config generated this signal
    "exit_config_ids",      # which exit config computed this exit
]
```

Extra columns (e.g., `dip_pct` for custom ranking) are preserved.

### What the base engine does for you

- Loads OHLCV data from configured exchanges (with prefetch for warmup)
- Runs your `generate_orders()` to get entry/exit pairs
- Sanitizes orders (removes zero-price, caps extreme returns)
- Ranks orders per day (configurable: top_gainer, top_dipper, etc)
- Simulates position-by-position with real charges, slippage, position limits
- Computes metrics (CAGR, MDD, Calmar, Sharpe, trade stats)
- Sweeps all config combinations automatically

### What the base engine does NOT do

- Filter by quality, momentum, fundamentals, or any other strategy-specific criteria
- Decide which stocks are "good" or "bad"
- Apply regime filters or benchmark-relative logic
- Compute any strategy-specific indicators

ALL of that lives in your signal generator file.

---

## Creating a new strategy

### Step 1: Create the signal generator

```python
# engine/signals/my_strategy.py
"""One-line description of the strategy thesis.

Entry: [what triggers an entry]
Exit: [what triggers an exit]
"""

import time
import polars as pl

from engine.config_loader import (
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import (
    register_strategy,
    add_next_day_values,
    run_scanner,
    walk_forward_exit,
    finalize_orders,
)

class MyStrategySignalGenerator:

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        """Define which YAML entry params this strategy uses and their defaults."""
        return {
            "lookback_days": entry_cfg.get("lookback_days", [10]),
            "threshold_pct": entry_cfg.get("threshold_pct", [5]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        """Define which YAML exit params this strategy uses and their defaults."""
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [252]),
        }

    def generate_orders(self, context, df_tick_data):
        print("\n--- My Strategy Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (shared per-day liquidity filter)
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute YOUR indicators on the full data range
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # ... compute strategy-specific indicators here ...
        # Example: rolling averages, dip detection, momentum, etc.

        # Phase 3: Trim to sim range, merge scanner tags
        df_signals = df_ind.filter(pl.col("date_epoch") >= start_epoch)
        df_signals = df_signals.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") +
             pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )
        scanner_ids_df = df_trimmed.select(
            ["uid", "scanner_config_ids"]
        ).unique(subset=["uid"])
        df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

        # Phase 4: Generate entry/exit pairs
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            # ... apply YOUR entry logic using entry_config params ...

            for exit_config in get_exit_config_iterator(context):
                # ... compute exits for each entry using exit_config params ...

                # For each valid entry/exit pair:
                all_order_rows.append({
                    "instrument": inst,
                    "entry_epoch": entry_epoch,
                    "exit_epoch": exit_epoch,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "entry_volume": entry_volume,
                    "exit_volume": 0,
                    "scanner_config_ids": scanner_id or "1",
                    "entry_config_ids": str(entry_config["id"]),
                    "exit_config_ids": str(exit_config["id"]),
                })

        elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, elapsed)


register_strategy("my_strategy", MyStrategySignalGenerator)
```

### Step 2: Register the import

Add one line to `engine/signals/__init__.py`:

```python
from engine.signals import my_strategy  # noqa: F401
```

### Step 3: Create the YAML config

```yaml
# strategies/my_strategy/config_nse.yaml

static:
  strategy_type: my_strategy
  start_margin: 10000000
  start_epoch: 1262304000     # 2010-01-01
  end_epoch: 1773878400       # 2026-03-17
  prefetch_days: 500           # warmup for indicators
  data_granularity: day

scanner:
  instruments:
    - [{exchange: NSE, symbols: []}]    # empty = all symbols
  price_threshold: [50]
  avg_day_transaction_threshold:
    - {period: 125, threshold: 70000000}
  n_day_gain_threshold:
    - {n: 360, threshold: -999}         # -999 = no filter

entry:
  # YOUR strategy-specific params (lists = sweep values)
  lookback_days: [10, 20]
  threshold_pct: [5, 7]

exit:
  tsl_pct: [10, 15]
  max_hold_days: [252, 504]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [30]
  max_positions: [5, 10]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1.0]
  max_order_value:
    - {type: fixed, value: 1000000000}
```

### Step 4: Run

All backtests run on CR cloud compute via the Projects API. Never run production sweeps locally — local is only for debugging small configs.

```bash
# ── Cloud execution (default for all real runs) ──

# First time: set up the cloud project and upload all engine/lib files
python run_remote.py --setup

# Run a pipeline config on cloud compute
python run_remote.py strategies/my_strategy/config_nse.yaml

# With options
python run_remote.py strategies/my_strategy/config_nse.yaml --timeout 600 --ram 16384
python run_remote.py strategies/my_strategy/config_nse.yaml -o results/my_strategy.json

# Run a standalone exploration script on cloud
python run_remote.py scripts/my_exploration.py
python run_remote.py scripts/my_exploration.py --env MARKET=us

# Skip file sync (if you just uploaded)
python run_remote.py strategies/my_strategy/config_nse.yaml --no-sync


# ── Local execution (debugging only, small configs) ──

source ../.venv/bin/activate
python3 -c "
from engine.pipeline import run_pipeline
r = run_pipeline('strategies/my_strategy/config_nse.yaml')
r.print_leaderboard()
"
```

### How cloud execution works

1. **`run_remote.py`** auto-detects the target type (.yaml = pipeline, .py = standalone script)
2. **`CloudOrchestrator`** manages the CR Projects API: creates project, syncs files (hash-based diff — only uploads changed files), submits run
3. **`backtest_main.py`** is the cloud entry point for pipeline configs — it calls `run_pipeline()` and writes `result.json`
4. Results are downloaded automatically after the run completes

The CR Projects API provides on-demand compute with access to the full data warehouse (FMP, NSE, etc.) without needing local data files. Data queries inside signal generators (via `CRDataProvider` or `cr_client.CetaResearch()`) execute against the warehouse directly.

For large sweeps with many configs, use `cloud_sweep.py` which splits configs into parallel batches:

```bash
# Run a large sweep in 3 parallel cloud containers
python scripts/cloud_sweep.py --parallel 3 --batch-size 24
```

---

## Rules

### 1. Never modify the base engine for a strategy

These files are shared by 30 signal generators. Changes here affect everything:

- `pipeline.py`, `simulator.py`, `ranking.py`, `data_provider.py`
- `signals/base.py` (except adding new shared utilities that don't break existing signatures)
- `config_loader.py`, `config_sweep.py`, `charges.py`

**config_loader.py is truly DO NOT MODIFY.** It contains only shared config parsing (scanner, simulation, static) and dispatches to your signal generator's `build_entry_config()` / `build_exit_config()` methods automatically. Adding new strategy params requires editing only your signal generator file.

If your strategy needs different simulation behavior (e.g., exit-before-entry ordering, dynamic position sizing), that's a config option on the simulator, not a code change in simulator.py. Propose the config option, don't hardcode the behavior.

### 2. One file per strategy, all logic inside

Your signal generator file contains ALL strategy-specific logic:

- **Config schema** (`build_entry_config`, `build_exit_config`) — defines your YAML params and defaults
- Universe filtering (quality screens, turnover filters, sector filters)
- Indicator computation (momentum, dip detection, regime filters)
- Entry signal logic
- Exit computation (walk_forward_exit or custom)
- Fundamental overlays

Do not export strategy helpers for other strategies to import. If two strategies share logic, extract it to `signals/base.py` as a generic utility with a stable signature.

### 3. Config values are lists, not scalars

Every entry/exit/simulation parameter must be a list in YAML. This enables sweeping:

```yaml
# WRONG - hardcoded
entry:
  lookback_days: 10

# RIGHT - sweepable (even for a single value)
entry:
  lookback_days: [10]
```

### 4. MOC execution model

All strategies must use next-day execution:

- Signal evaluated at close[T]
- Entry at open[T+1] (via `add_next_day_values`)
- Exit signal at close[T], exit at open[T+1] or close[T] depending on strategy

Using same-bar entry (signal at close, enter at close) inflates returns by 15-20pp CAGR for mean-reversion strategies. This is a hard rule.

### 5. Scanner is for liquidity, not strategy logic

The shared `run_scanner()` filters by:
- Price > threshold (avoid penny stocks)
- Rolling avg turnover > threshold (ensure executable position sizes)
- Optional: n-day gain filter

It answers: "Can I trade this stock today?" Not: "Should I trade this stock today?"

Strategy-specific filtering (quality screens, momentum ranking, fundamental gates) goes in your signal generator, not the scanner.

---

## Config exploration methodology

You cannot sweep all parameters simultaneously. A strategy with 10 params at 3 values each = 59,049 configs. Use greedy optimization:

### Round 1: Fix and sweep

1. Set all params to sensible defaults (from prior strategies or domain knowledge)
2. Pick the 2-3 params most specific to your strategy thesis
3. Sweep those (3 values each = 9 configs)
4. Lock the winners

### Round 2: Repeat

5. Pick the next 2-3 most impactful params
6. Sweep with the Round 1 winners locked
7. Lock the new winners

### Round 3: Validate

8. Final combined sweep of the top 3-5 most sensitive params (max 243 configs)
9. Check that the best config is stable (neighbors should also perform well)

### Known good defaults

These transfer across NSE equity strategies:

| Parameter | Default | Why |
|-----------|---------|-----|
| `price_threshold` | 50 | Avoid penny stocks, sufficient for NSE |
| `avg_day_transaction_threshold` | 70M INR | Ensures ~1L order can execute without impact |
| `prefetch_days` | 500-1500 | Warmup for yearly return lookbacks |
| `max_positions_per_instrument` | 1 | Avoid concentration |
| `order_value_multiplier` | 1.0 | Equal weight default |
| `max_order_value` | 1B (no cap) | Let position sizing handle it |

### What to sweep first per strategy type

**Dip-buy**: dip_threshold, peak_lookback, tsl_pct
**Momentum**: lookback_days, percentile, rebalance_interval
**Mean-reversion**: entry_z_score, exit_z_score, lookback
**Breakout**: n_day_high, direction_score, min_hold_days

---

## Development workflow

### Phase 1: Exploration (standalone scripts, cloud)

Use `scripts/` + `quality_dip_buy_lib.py` for rapid iteration:
- Pure Python loops, easy to debug line by line
- No config machinery overhead
- Fast turnaround on new ideas
- Run on cloud: `python run_remote.py scripts/my_exploration.py`
- Result: "Does this thesis work at all?"

### Phase 2: Validation (engine pipeline, cloud)

Port to `engine/signals/` for honest validation:
- Per-day liquidity filter (scanner)
- Real charges and slippage (5 bps)
- Position limits and sizing
- Config sweeps for parameter sensitivity
- Run on cloud: `python run_remote.py strategies/my_strategy/config_nse.yaml`
- Result: "Does this work with realistic execution?"

### Phase 3: Sweep (engine pipeline, parallel cloud)

Find optimal config using greedy parameter search:
- Start with 2-3 key params, lock winners, iterate
- Run on cloud: `python scripts/cloud_sweep.py --parallel 3`
- Result: "What is the best config for this strategy?"

### When to run locally vs cloud

| Scenario | Where | Why |
|----------|-------|-----|
| Debug a signal generator (1-2 configs) | Local | Fast iteration, breakpoints |
| Test a new strategy (3+ configs) | Cloud | Needs data warehouse access |
| Parameter sweep (10+ configs) | Cloud | Compute + data |
| Quick sanity check after code change | Local | Speed |

### The gap between standalone and engine is expected

The standalone produces an upper bound. The engine produces an honest estimate. A typical gap is 2-5pp CAGR due to:
- Scanner filtering out illiquid entry days
- Integer position sizing (can't buy fractional shares)
- Position limits rejecting entries when full
- Different entry ordering (ranking)

A gap larger than 5pp means the engine has a bug or the standalone has a bias. Debug before proceeding.

---

## Shared utilities available from base.py

| Utility | Purpose | When to use |
|---------|---------|-------------|
| `add_next_day_values(df)` | Adds `next_epoch`, `next_open`, `next_volume` columns | Always (MOC execution) |
| `run_scanner(context, df)` | Per-day liquidity filter | Always (unless strategy has custom scanner) |
| `walk_forward_exit(epochs, closes, ...)` | Pre-compute exit from entry using TSL + peak recovery | Dip-buy, breakout strategies |
| `finalize_orders(rows, elapsed)` | Convert order dicts to sorted DataFrame | Always (last line of generate_orders) |
| `build_regime_filter(df, instrument, sma)` | Bull/bear regime epochs | Optional, when strategy uses regime |
| `sanitize_orders(df, min_price, max_mult)` | Remove bad data (called by pipeline, not by you) | Automatic |

---

## Checklist for new strategies

Before submitting a new signal generator:

- [ ] Single file in `engine/signals/`, registered via `register_strategy()`
- [ ] Defines `build_entry_config()` and `build_exit_config()` as `@staticmethod` on the class
- [ ] Import added to `engine/signals/__init__.py`
- [ ] YAML config in `strategies/{name}/`
- [ ] All params are lists in YAML (sweepable)
- [ ] Uses `add_next_day_values` for MOC execution (no same-bar entry)
- [ ] Uses `run_scanner` for liquidity (or documents why not)
- [ ] Uses `finalize_orders` to return standard DataFrame
- [ ] Does NOT import from other signal generators
- [ ] Does NOT modify base engine files (especially `config_loader.py`)
- [ ] Tested with at least 2 values per key param to verify sweep works
- [ ] Runs on CR cloud via `run_remote.py` (not just locally)
- [ ] Standalone exploration script exists in `scripts/` for reference

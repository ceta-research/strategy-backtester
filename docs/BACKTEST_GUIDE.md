# Backtest Development Guide

How to write, run, and analyze backtests in this repo.

## Quick Start

```bash
# Local
source /Users/swas/Desktop/Swas/Kite/ATO_SUITE/.venv/bin/activate
python scripts/buy_2day_high.py          # writes result.json

# Remote (cloud compute)
python run_remote.py --setup             # first time: link repo
python run_remote.py scripts/buy_2day_high.py   # run on prod
```

## Architecture

```
scripts/buy_2day_high.py     ← Your strategy (entry/exit logic)
    ↓ uses
lib/backtest_result.py       ← Standardized output (BacktestResult, SweepResult)
    ↓ uses
lib/metrics.py               ← 17+ risk/return metrics (pure Python)
engine/charges.py            ← Exchange-specific transaction costs
lib/cr_client.py             ← Data fetching + remote execution API
```

Every script writes a single `result.json` file containing all metrics, equity curves, trades, and breakdowns. This file is the contract between the backtest engine and the UI.

## Writing a New Strategy

### 1. Create the script

```python
#!/usr/bin/env python3
"""One-line description of strategy."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Cloud container fallback (scripts run from /session/scripts/ but libs are at /session/)
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult

CAPITAL = 10_000_000
SLIPPAGE = 0.0005  # 5 bps
```

### 2. Fetch data

Use `CetaResearch.query()` for data. Always add a warmup period for indicators.

```python
cr = CetaResearch()
sql = """SELECT date_epoch, open, high, low, close, volume
         FROM nse.nse_charting_day
         WHERE symbol = 'NIFTYBEES'
           AND date_epoch >= {start} AND date_epoch <= {end}
         ORDER BY date_epoch"""
results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                   memory_mb=16384, threads=6)
```

### 3. Simulate with BacktestResult

```python
result = BacktestResult(
    strategy_name="my_strategy",
    params={"lookback": 20, "threshold": 5},
    instrument="NIFTYBEES",
    exchange="NSE",
    capital=CAPITAL,
    slippage_bps=5,
    description="Buy when RSI < 30, sell when RSI > 70",
)

# During simulation loop:
for i in range(start_idx, len(data)):
    # ... your entry/exit logic ...

    # Record completed trades
    if sold_today:
        result.add_trade(
            entry_epoch=entry_epoch, exit_epoch=exit_epoch,
            entry_price=entry_price, exit_price=exit_price,
            quantity=qty, side="LONG",
            charges=buy_charges + sell_charges,
            slippage=buy_slippage + sell_slippage,
        )

    # Record daily portfolio value (EVERY day)
    result.add_equity_point(epoch, portfolio_value)
```

### 4. Set benchmark and save

```python
# Buy-and-hold benchmark
bm_values = [close[i] / close[start] * CAPITAL for i in range(start, end)]
result.set_benchmark_values(bm_epochs, bm_values)

# Compute metrics and save
result.compute()
result.save("result.json")
result.print_summary()
```

### 5. For parameter sweeps, use SweepResult

```python
sweep = SweepResult("my_strategy", "NIFTYBEES", "NSE", CAPITAL)

for lookback in [10, 20, 30]:
    for threshold in [3, 5, 7]:
        r = simulate(data, start_idx, bm_epochs, bm_values,
                     lookback=lookback, threshold=threshold)
        sweep.add_config({"lookback": lookback, "threshold": threshold}, r)

sweep.print_leaderboard(top_n=20)
sweep.save("result.json", top_n=20, sort_by="calmar_ratio")
```

SweepResult stores summary metrics for ALL configs but only full detail (equity curves, trades, monthly/yearly breakdowns) for the top N. This keeps the output under 50 MB even for 2000+ config sweeps.

## Execution Rules (Non-Negotiable)

These rules prevent the biases that inflated our early results by 15-20pp CAGR.

### MOC Execution Model

Signal from today's close, execute at tomorrow's open. Never use same-bar entry.

```python
# CORRECT: signal → pending → execute next bar
if signal_triggered(close[i]):
    pending_buy = True          # signal at today's close

# Next iteration:
if pending_buy:
    buy_at(open[i])             # execute at next day's open
    pending_buy = False
```

### Real Transaction Costs

Always use `engine/charges.py`. Never skip charges or use flat estimates.

```python
from engine.charges import calculate_charges

# NSE: STT 0.1% + brokerage + GST + stamp duty
ch = calculate_charges("NSE", order_value, "EQUITY", "DELIVERY", "BUY_SIDE")

# US: SEC fee + FINRA TAF (sell side only)
ch = calculate_charges("US", order_value, "EQUITY", "DELIVERY", "SELL_SIDE")
```

### Slippage

5 bps minimum. Always.

```python
SLIPPAGE = 0.0005
slippage_cost = order_value * SLIPPAGE
```

### Integer Quantities

No fractional shares.

```python
qty = int(cash_to_invest / price)  # not cash_to_invest / price
```

### Currency Normalization

Cross-exchange ratios must normalize to a common currency.

```python
# WRONG: ratio = SPY_close / NIFTYBEES_close  (USD vs INR)
# RIGHT: ratio = SPY_close / (NIFTYBEES_close / USDINR_rate)
```

## Output Schema (result.json)

### Single Config (`type: "single"`)

```json
{
  "version": "1.0",
  "type": "single",
  "strategy": {
    "name": "buy_nday_high_tsl",
    "description": "...",
    "params": {"lookback_days": 3, "trailing_sl_pct": 5, "buy_fraction": 0.95},
    "instrument": "NIFTYBEES",
    "exchange": "NSE",
    "capital": 10000000,
    "slippage_bps": 5
  },
  "summary": {
    "cagr": 0.133,
    "total_return": 12.5,
    "max_drawdown": -0.41,
    "max_dd_duration_periods": 450,
    "annualized_volatility": 0.18,
    "sharpe_ratio": 0.63,
    "sortino_ratio": 0.96,
    "calmar_ratio": 0.32,
    "var_95": -0.025,
    "cvar_95": -0.035,
    "skewness": -0.5,
    "kurtosis": 3.2,
    "best_day": 0.08,
    "worst_day": -0.12,
    "best_month": 0.15,
    "worst_month": -0.25,
    "best_year": 0.45,
    "worst_year": -0.35,
    "total_trades": 110,
    "winning_trades": 65,
    "losing_trades": 45,
    "win_rate": 0.59,
    "avg_win_pct": 8.5,
    "avg_loss_pct": -4.2,
    "profit_factor": 1.8,
    "payoff_ratio": 2.02,
    "expectancy": 15000,
    "avg_hold_days": 45,
    "max_consecutive_wins": 8,
    "max_consecutive_losses_trades": 5,
    "kelly_criterion": 0.30,
    "time_in_market": 0.85,
    "final_value": 125000000,
    "peak_value": 130000000
  },
  "benchmark": {
    "cagr": 0.125,
    "max_drawdown": -0.597
  },
  "comparison": {
    "excess_cagr": 0.008,
    "alpha": 0.01,
    "beta": 0.85,
    "win_rate": 0.52,
    "information_ratio": 0.3,
    "tracking_error": 0.05,
    "up_capture": 0.9,
    "down_capture": 0.7
  },
  "equity_curve": [
    {"epoch": 1104537600, "date": "2005-01-01", "value": 10000000.00},
    {"epoch": 1104624000, "date": "2005-01-02", "value": 10050000.00}
  ],
  "trades": [
    {
      "entry_epoch": 1104624000, "entry_date": "2005-01-02",
      "exit_epoch": 1107302400, "exit_date": "2005-02-02",
      "entry_price": 100.5, "exit_price": 105.2,
      "quantity": 950, "side": "LONG",
      "gross_pnl": 4465, "net_pnl": 4200,
      "pnl_pct": 4.68, "hold_days": 31,
      "charges": 215, "slippage": 50
    }
  ],
  "monthly_returns": {
    "2005": {"1": 0.05, "2": -0.02, "3": 0.08},
    "2006": {"1": 0.03, "2": 0.01}
  },
  "yearly_returns": [
    {"year": 2005, "return": 0.15, "mdd": -0.12, "end_value": 11500000, "trades": 8}
  ],
  "costs": {
    "total_charges": 1500000,
    "total_slippage": 50000,
    "total_cost": 1550000,
    "cost_pct_of_capital": 15.5
  }
}
```

### Config Sweep (`type: "sweep"`)

```json
{
  "version": "1.0",
  "type": "sweep",
  "meta": {
    "strategy_name": "buy_nday_high_tsl",
    "instrument": "NIFTYBEES",
    "exchange": "NSE",
    "capital": 10000000
  },
  "sort_by": "calmar_ratio",
  "total_configs": 48,
  "top_n_detailed": 20,
  "all_configs": [
    {
      "params": {"lookback_days": 3, "trailing_sl_pct": 5, "buy_fraction": 0.95},
      "cagr": 0.133,
      "max_drawdown": -0.41,
      "calmar_ratio": 0.32,
      "sharpe_ratio": 0.63,
      "total_trades": 110,
      "win_rate": 0.59
    }
  ],
  "detailed": [
    {
      "rank": 1,
      "params": {"lookback_days": 3, "trailing_sl_pct": 5},
      "summary": {},
      "benchmark": {},
      "comparison": {},
      "equity_curve": [],
      "trades": [],
      "monthly_returns": {},
      "yearly_returns": [],
      "costs": {}
    }
  ]
}
```

## Running Remotely

### Setup (once)

```bash
# Create project and upload all files
python run_remote.py --setup
```

This creates `.remote_project.json` with the project ID. Don't commit this file. The runner uploads all scripts, `lib/`, and `engine/` files, and injects your API key (from `CR_API_KEY` env var) before each run.

Optionally, if you've connected GitHub OAuth on cetaresearch.com, you can use git-sync:

```bash
python run_remote.py --setup --repo https://github.com/ceta-research/strategy-backtester
```

### Running

```bash
# Standard run (syncs changed files first)
python run_remote.py scripts/buy_2day_high.py

# Custom resources for heavy sweeps
python run_remote.py scripts/buy_2day_high.py --timeout 600 --ram 8192

# Skip file sync (use project files as-is)
python run_remote.py scripts/buy_2day_high.py --no-sync

# Save to specific path
python run_remote.py scripts/buy_2day_high.py -o results/niftybees_sweep.json
```

### What happens

1. `run_remote.py` uploads changed files (hash-based diff, only modified files)
2. Injects `.env` with `CR_API_KEY` so the container can fetch data
3. Submits `run_project(entry_path="scripts/buy_2day_high.py")`
4. Script runs on prod (fetches data via CR API, simulates, writes result.json)
5. Runner downloads result.json to `results/` locally

### Resource defaults

| Resource | Default | For heavy sweeps |
|----------|---------|-----------------|
| RAM | 4096 MB | 8192 MB |
| Disk | 1024 MB | 2048 MB |
| Timeout | 600s | 600s |

## File Size Estimates

| Scenario | Configs | result.json size |
|----------|---------|-----------------|
| Single config | 1 | ~500 KB |
| Small sweep (top 20 detailed) | 48 | ~12 MB |
| Medium sweep (top 20 detailed) | 500 | ~15 MB |
| Large sweep (top 20 detailed) | 2000 | ~18 MB |

Size stays manageable because only top 20 configs get full equity curves and trade lists. The rest get summary metrics only (~200 bytes per config).

## Checklist for New Strategies

- [ ] MOC execution: signal from prior close, execute at current open
- [ ] Real charges via `engine/charges.py`
- [ ] 5 bps slippage (`SLIPPAGE = 0.0005`)
- [ ] Integer quantities (`int(cash / price)`)
- [ ] `BacktestResult.add_equity_point()` called every trading day
- [ ] `BacktestResult.add_trade()` called on every trade close
- [ ] `result.set_benchmark_values()` with buy-and-hold baseline
- [ ] `result.save("result.json")` at the end
- [ ] stdout kept under 50 KB (use `print_summary()`, not huge tables)
- [ ] For sweeps: use `SweepResult` with `top_n=20`

## Reference Implementation

`scripts/buy_2day_high.py` is the canonical example. Copy it and modify the entry/exit logic.

## Metrics Reference

### From lib/metrics.py (return-series level)

| Metric | Key | Description |
|--------|-----|-------------|
| CAGR | `cagr` | Compound annual growth rate |
| Total Return | `total_return` | Cumulative return |
| Max Drawdown | `max_drawdown` | Maximum peak-to-trough decline |
| Max DD Duration | `max_dd_duration_periods` | Longest drawdown in trading days |
| Annualized Volatility | `annualized_volatility` | Std dev of returns, annualized |
| Sharpe Ratio | `sharpe_ratio` | (CAGR - RFR) / volatility |
| Sortino Ratio | `sortino_ratio` | (CAGR - RFR) / downside deviation |
| Calmar Ratio | `calmar_ratio` | CAGR / |max drawdown| |
| VaR 95% | `var_95` | 5th percentile daily return |
| CVaR 95% | `cvar_95` | Expected shortfall below VaR |
| Skewness | `skewness` | Return distribution asymmetry |
| Kurtosis | `kurtosis` | Return distribution tail weight |

### From BacktestResult (trade-level)

| Metric | Key | Description |
|--------|-----|-------------|
| Win Rate | `win_rate` | Winning trades / total trades |
| Profit Factor | `profit_factor` | Gross profit / gross loss |
| Payoff Ratio | `payoff_ratio` | Avg win % / avg loss % |
| Expectancy | `expectancy` | Average net PnL per trade |
| Kelly Criterion | `kelly_criterion` | Optimal bet fraction |
| Time in Market | `time_in_market` | Days holding / total days |
| Avg Hold Days | `avg_hold_days` | Average trade duration |
| Max Consecutive Wins | `max_consecutive_wins` | Longest winning streak |
| Max Consecutive Losses | `max_consecutive_losses_trades` | Longest losing streak |

### Time breakdowns

| Metric | Key | Description |
|--------|-----|-------------|
| Best/Worst Day | `best_day`, `worst_day` | Single-day extremes |
| Best/Worst Month | `best_month`, `worst_month` | Single-month extremes |
| Best/Worst Year | `best_year`, `worst_year` | Single-year extremes |
| Monthly Returns | `monthly_returns` | Year → month → return (heatmap) |
| Yearly Returns | `yearly_returns` | Year, return, MDD, trades |

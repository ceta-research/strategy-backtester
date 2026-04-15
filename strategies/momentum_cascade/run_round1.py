"""Generate and run all Round 1 sensitivity sweeps for momentum_cascade."""
import yaml
import subprocess
import sys
import json
import os

BASE_DIR = "/Users/swas/Desktop/Swas/Kite/ATO_SUITE/strategy-backtester"
STRATEGY_DIR = f"{BASE_DIR}/strategies/momentum_cascade"
RESULTS_DIR = f"{BASE_DIR}/results/momentum_cascade"

# Baseline config (all defaults, single values)
BASELINE = {
    "static": {
        "strategy_type": "momentum_cascade",
        "start_margin": 10000000,
        "start_epoch": 1262304000,
        "end_epoch": 1773878400,
        "prefetch_days": 600,
        "data_granularity": "day",
        "data_provider": "nse_charting",
    },
    "scanner": {
        "instruments": [[{"exchange": "NSE", "symbols": []}]],
        "price_threshold": [50],
        "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
        "n_day_gain_threshold": [{"n": 360, "threshold": -999}],
    },
    "entry": {
        "fast_lookback_days": [42],
        "slow_lookback_days": [126],
        "accel_threshold_pct": [2],
        "min_momentum_pct": [20],
        "breakout_window": [63],
        "regime_instrument": [""],
        "regime_sma_period": [0],
    },
    "exit": {
        "trailing_stop_pct": [12],
        "max_hold_days": [504],
    },
    "simulation": {
        "default_sorting_type": ["top_gainer"],
        "order_sorting_type": ["top_gainer"],
        "order_ranking_window_days": [180],
        "max_positions": [10],
        "max_positions_per_instrument": [1],
        "order_value_multiplier": [0.95],
        "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
    },
}

# Round 1 sweep definitions: {name: {section: {param: [values]}}}
SWEEPS = {
    "fast_lookback": {"entry": {"fast_lookback_days": [10, 21, 30, 42, 63, 84, 105, 126]}},
    "slow_lookback": {"entry": {"slow_lookback_days": [42, 63, 84, 126, 168, 210, 252, 504]}},
    "accel_threshold": {"entry": {"accel_threshold_pct": [0, 1, 2, 3, 5, 7, 10, 15]}},
    "min_momentum": {"entry": {"min_momentum_pct": [0, 5, 10, 15, 20, 30, 40, 50]}},
    "breakout_window": {"entry": {"breakout_window": [10, 21, 42, 63, 84, 126, 168, 252]}},
    "regime_sma": {
        "entry": {
            "regime_instrument": ["NSE:NIFTYBEES"],
            "regime_sma_period": [0, 50, 100, 150, 200, 300],
        }
    },
    "tsl": {"exit": {"trailing_stop_pct": [3, 5, 8, 10, 12, 15, 20, 30, 50]}},
    "max_hold": {"exit": {"max_hold_days": [42, 63, 126, 252, 378, 504, 756, 1008]}},
    "sorting": {"simulation": {"order_sorting_type": ["top_gainer", "top_performer", "top_average_txn", "top_dipper"]}},
    "max_positions": {"simulation": {"max_positions": [3, 5, 7, 10, 15, 20, 30, 50]}},
    "per_instrument": {"simulation": {"max_positions_per_instrument": [1, 2, 3, 5]}},
    "ranking_window": {"simulation": {"order_ranking_window_days": [30, 60, 90, 120, 180, 252, 360]}},
}

import copy

def make_sweep_config(sweep_name, overrides):
    """Create a config with one param swept, others at baseline."""
    cfg = copy.deepcopy(BASELINE)
    for section, params in overrides.items():
        for param, values in params.items():
            cfg[section][param] = values
    return cfg


def run_sweep(sweep_name):
    """Generate config, run pipeline, return results path."""
    overrides = SWEEPS[sweep_name]
    cfg = make_sweep_config(sweep_name, overrides)

    config_path = f"{STRATEGY_DIR}/config_round1_{sweep_name}.yaml"
    result_path = f"{RESULTS_DIR}/round1_{sweep_name}.json"

    # Write config
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    # Run pipeline
    cmd = [
        sys.executable, f"{BASE_DIR}/run.py",
        "--config", config_path,
        "--output", result_path,
    ]
    print(f"\n{'='*60}")
    print(f"Running sweep: {sweep_name}")
    print(f"  Config: {config_path}")
    print(f"  Output: {result_path}")
    print(f"{'='*60}")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR, timeout=600)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-500:]}")
        return None

    # Print key output lines
    for line in result.stdout.split("\n"):
        if any(kw in line for kw in ["CAGR", "Best", "config", "Signal gen", "Simulation"]):
            print(f"  {line.strip()}")

    return result_path


def analyze_sweep_results(result_path, sweep_name, overrides):
    """Extract marginal analysis for the swept param."""
    with open(result_path) as f:
        data = json.load(f)

    configs = data["all_configs"]
    if not configs:
        print(f"  No results for {sweep_name}")
        return

    # Sort by calmar
    configs.sort(key=lambda x: x.get("calmar_ratio", 0), reverse=True)

    print(f"\n  Results ({len(configs)} configs):")
    print(f"  {'Config':<12} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7}")
    print(f"  {'-'*55}")
    for c in configs:
        p = c["params"]
        cid = p.get("config_id", "?")
        cagr = c.get("cagr", 0) * 100
        mdd = c.get("max_drawdown", 0) * 100
        cal = c.get("calmar_ratio", 0)
        sh = c.get("sharpe_ratio", 0)
        trades = c.get("total_trades", 0)
        print(f"  {cid:<12} {cagr:>7.1f}% {mdd:>7.1f}% {cal:>8.3f} {sh:>8.3f} {trades:>7}")


def main():
    # Choose which sweeps to run
    if len(sys.argv) > 1:
        names = sys.argv[1:]
    else:
        names = list(SWEEPS.keys())

    for name in names:
        if name not in SWEEPS:
            print(f"Unknown sweep: {name}")
            continue

        result_path = run_sweep(name)
        if result_path and os.path.exists(result_path):
            analyze_sweep_results(result_path, name, SWEEPS[name])


if __name__ == "__main__":
    main()

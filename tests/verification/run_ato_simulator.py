#!/usr/bin/env python3
"""Run ATO_Simulator with monkey-patched configs matching config_ato_match.yaml.

Patches config functions to use fixed values, then calls drive().
Reads output parquet files and saves consolidated results to ato_results.json.
"""

import glob
import json
import os
import sys

import pandas as pd

# Set ATO_BASE_PATH before importing ATO_Simulator (path_constants reads it at import time)
os.environ["ATO_BASE_PATH"] = os.path.expanduser("~/ATO_DATA")

# Fixed config values matching config_ato_match.yaml exactly
FIXED_START_EPOCH = 1577836800   # 2020-01-01
FIXED_END_EPOCH = 1640995200     # 2022-01-01
FIXED_START_MARGIN = 9999999
FIXED_PREFETCH_SECONDS = 400 * 86400

NSE_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR",
    "ICICIBANK", "KOTAKBANK", "LT", "SBIN", "BHARTIARTL",
    "AXISBANK", "ITC", "ASIANPAINT", "MARUTI", "TITAN",
    "SUNPHARMA", "BAJFINANCE", "NESTLEIND", "WIPRO", "ULTRACEMCO",
    "HCLTECH", "TECHM", "POWERGRID", "NTPC", "M&M",
    "TATAMOTORS", "ONGC", "JSWSTEEL", "GRASIM", "BPCL",
]


def patched_get_scanner_config_input():
    return {
        "instruments": [
            [{"exchange": "NSE", "symbols": []}]
        ],
        "price_threshold": [45, 99],
        "avg_day_transaction_threshold": [
            {"period": 125, "threshold": 69999999},
            {"period": 125, "threshold": 369999999},
        ],
        "n_day_gain_threshold": [
            {"n": 360, "threshold": 0},
        ],
    }


def patched_get_entry_config_input():
    return {
        "n_day_high": [2],
        "direction_score": [
            {"n_day_ma": 3, "score": 0.54},
        ],
        "n_day_ma": [3],
    }


def patched_get_exit_config_input():
    return {
        "min_hold_time_days": [0, 4],
        "trailing_stop_pct": [15],
    }


def patched_get_simulation_config_input():
    return {
        "default_sorting_type": ["top_gainer"],
        "order_sorting_type": ["top_performer"],
        "order_ranking_window_days": [180],
        "max_positions": [20, 30],
        "max_positions_per_instrument": [1],
        "order_value_multiplier": [1],
        "max_order_value": [
            {"type": "percentage_of_instrument_avg_txn", "value": 4.5},
        ],
    }


def patched_get_static_simulation_config():
    return {
        "start_margin": FIXED_START_MARGIN,
        "start_epoch": FIXED_START_EPOCH,
        "end_epoch": FIXED_END_EPOCH,
        "prefetch_seconds": FIXED_PREFETCH_SECONDS,
        "data_granularity": "day",
    }


def patched_get_tradeable_symbols(data_source, exchange):
    return pd.Series(NSE_SYMBOLS if exchange.upper() == "NSE" else [])


def apply_monkey_patches():
    """Patch ATO_Simulator config functions with our fixed values."""
    import ATO_Simulator.simulator.steps.scanner_step.scanner_config as scanner_config_mod
    import ATO_Simulator.simulator.steps.order_generation_step.entry_config as entry_config_mod
    import ATO_Simulator.simulator.steps.order_generation_step.exit_config as exit_config_mod
    import ATO_Simulator.simulator.steps.simulate_step.simulation_config as sim_config_mod
    import ATO_Simulator.util.ticker_functions as ticker_mod
    import ATO_Simulator.simulator.driver as driver_mod

    # Patch at the module level where the function is defined
    scanner_config_mod.get_scanner_config_input = patched_get_scanner_config_input
    entry_config_mod.get_entry_config_input = patched_get_entry_config_input
    exit_config_mod.get_exit_config_input = patched_get_exit_config_input
    sim_config_mod.get_simulation_config_input = patched_get_simulation_config_input
    sim_config_mod.get_static_simulation_config = patched_get_static_simulation_config
    ticker_mod.get_tradeable_symbols = patched_get_tradeable_symbols

    # Also patch at the import sites (driver.py imports these directly)
    driver_mod.get_scanner_config_input = patched_get_scanner_config_input
    driver_mod.get_entry_config_input = patched_get_entry_config_input
    driver_mod.get_exit_config_input = patched_get_exit_config_input
    driver_mod.get_simulation_config_input = patched_get_simulation_config_input
    driver_mod.get_static_simulation_config = patched_get_static_simulation_config

    # Patch get_tradeable_symbols where it's used (sys_functions imports it)
    import ATO_Simulator.simulator.util.sys_functions as sys_func_mod
    sys_func_mod.get_tradeable_symbols = patched_get_tradeable_symbols

    print("Monkey patches applied:")
    print(f"  start_epoch: {FIXED_START_EPOCH} ({pd.Timestamp(FIXED_START_EPOCH, unit='s').date()})")
    print(f"  end_epoch:   {FIXED_END_EPOCH} ({pd.Timestamp(FIXED_END_EPOCH, unit='s').date()})")
    print(f"  start_margin: {FIXED_START_MARGIN}")
    print(f"  symbols: {len(NSE_SYMBOLS)} NSE")


def collect_results(base_path):
    """Read day_wise_log parquet files from ATO_Simulator output."""
    sim_path = os.path.join(base_path, "simulations/portfolio_simulation/1")

    # Find the most recent run_id
    if not os.path.isdir(sim_path):
        print(f"ERROR: No simulation output at {sim_path}")
        return {}

    run_ids = []
    for entry in os.listdir(sim_path):
        try:
            run_ids.append(float(entry))
        except ValueError:
            continue

    if not run_ids:
        print("ERROR: No run IDs found")
        return {}

    latest_run = str(max(run_ids))
    run_path = os.path.join(sim_path, latest_run, "simulation_step")
    print(f"Reading results from: {run_path}")

    results = {}
    day_wise_log_base = os.path.join(run_path, "day_wise_log")

    if not os.path.isdir(day_wise_log_base):
        print(f"ERROR: No day_wise_log at {day_wise_log_base}")
        return {}

    for config_dir in sorted(os.listdir(day_wise_log_base)):
        if not config_dir.startswith("config_id="):
            continue

        config_id = config_dir.replace("config_id=", "")
        config_path = os.path.join(day_wise_log_base, config_dir)

        parquet_files = glob.glob(os.path.join(config_path, "*.parquet"))
        if not parquet_files:
            print(f"  WARNING: No parquet files for {config_id}")
            continue

        # Read and concatenate all chunks for this config
        dfs = [pd.read_parquet(f) for f in sorted(parquet_files)]
        df = pd.concat(dfs, ignore_index=True)
        df.sort_values("epoch", inplace=True)
        df.drop_duplicates(subset=["epoch"], keep="last", inplace=True)

        # Filter to only epochs >= start_epoch (exclude prefetch)
        df = df[df["epoch"] >= FIXED_START_EPOCH]

        day_wise_log = []
        for _, row in df.iterrows():
            day_wise_log.append({
                "log_date_epoch": int(row["epoch"]),
                "invested_value": float(row["invested_value"]),
                "margin_available": float(row["margin_available"]),
            })

        results[config_id] = day_wise_log
        account_val = day_wise_log[-1]["invested_value"] + day_wise_log[-1]["margin_available"] if day_wise_log else 0
        print(f"  {config_id}: {len(day_wise_log)} days, final account={account_val:,.0f}")

    return results


def main():
    print("=== ATO_Simulator Verification Run ===\n")

    # Verify data exists
    data_path = os.path.expanduser("~/ATO_DATA/tick_data/data_source=kite/granularity=day/exchange=NSE")
    if not os.path.isdir(data_path):
        print(f"ERROR: No data at {data_path}")
        print("Run download_data.py first.")
        sys.exit(1)

    parquet_files = [f for f in os.listdir(data_path) if f.endswith(".parquet")]
    if not parquet_files:
        print(f"ERROR: No parquet files in {data_path}")
        sys.exit(1)

    print(f"Data found: {data_path} ({len(parquet_files)} files)\n")

    # Apply patches and run
    apply_monkey_patches()
    print("\nRunning ATO_Simulator.drive()...\n")

    from ATO_Simulator.simulator.driver import drive
    simulator = drive()

    print("\nCollecting results...\n")
    results = collect_results(os.path.expanduser("~/ATO_DATA"))

    # Save results
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, "ato_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nSaved {len(results)} config results to: {output_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run strategy-backtester with ParquetDataProvider and matching config.

Uses local parquet data (same as ATO_Simulator) to produce comparable results.
Saves day_wise_log per config_id to sb_results.json.
"""

import json
import os
import sys

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from engine.data_provider import ParquetDataProvider
from engine.pipeline import run_pipeline

FIXED_START_EPOCH = 1577836800   # 2020-01-01

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_ato_match.yaml")
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    print("=== Strategy-Backtester Verification Run ===\n")

    # Verify data exists
    data_path = os.path.expanduser("~/ATO_DATA/tick_data")
    nse_path = os.path.join(data_path, "data_source=kite/granularity=day/exchange=NSE")
    if not os.path.isdir(nse_path):
        print(f"ERROR: No data at {nse_path}")
        print("Run download_data.py first.")
        sys.exit(1)

    print(f"Data: {nse_path}")
    print(f"Config: {CONFIG_PATH}\n")

    # Create ParquetDataProvider pointing to the same data as ATO_Simulator
    provider = ParquetDataProvider(base_path=data_path)

    # Run pipeline
    results = run_pipeline(CONFIG_PATH, data_provider=provider)

    if not results:
        print("ERROR: No results from pipeline.")
        sys.exit(1)

    # Extract day_wise_log per config_id, filtering to epochs >= start_epoch
    output = {}
    for r in results:
        config_id = r["config_id"]
        day_wise_log = r.get("day_wise_log", [])

        # Filter to epochs >= start_epoch (match comparison window)
        filtered = [d for d in day_wise_log if d["log_date_epoch"] >= FIXED_START_EPOCH]

        output[config_id] = filtered
        account_val = filtered[-1]["invested_value"] + filtered[-1]["margin_available"] if filtered else 0
        print(f"  {config_id}: {len(filtered)} days, final account={account_val:,.0f}")

    # Save results
    output_path = os.path.join(OUTPUT_DIR, "sb_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved {len(output)} config results to: {output_path}")


if __name__ == "__main__":
    main()

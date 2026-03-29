#!/usr/bin/env python3
"""Run engine pipeline with bhavcopy data (survivorship-bias-free NSE data).

Usage:
    CONFIG_FILE=strategies/eod_technical/config_nse_sweep.yaml python scripts/run_bhavcopy.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.pipeline import run_pipeline
from engine.data_provider import BhavcopyDataProvider
from lib.backtest_result import SweepResult


def main():
    config_path = os.environ.get("CONFIG_FILE", "config.yaml")
    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Starting backtest with BHAVCOPY data (config={config_path})...")
    start = time.time()

    provider = BhavcopyDataProvider()
    result = run_pipeline(config_path, data_provider=provider)

    elapsed = round(time.time() - start, 1)

    if isinstance(result, SweepResult):
        print(f"\nSweep complete: {len(result.configs)} configs in {elapsed}s")
        result.save("result.json")
        result.print_leaderboard(top_n=10)
    else:
        print(f"Unexpected result type: {type(result)}")


if __name__ == "__main__":
    main()

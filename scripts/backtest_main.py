#!/usr/bin/env python3
"""Cloud entry point for strategy backtests.

Runs on CR compute via the Projects API. Reads config from CONFIG_FILE env var,
executes the pipeline, writes result.json (auto-uploaded to R2 by the executor).

Usage (via wrapper script):
    CONFIG_FILE=runs/config_123.yaml python backtest_main.py
"""

import os
import sys
import time
import traceback

sys.path.insert(0, os.getcwd())

from engine.pipeline import run_pipeline
from lib.backtest_result import BacktestResult, SweepResult


def main():
    config_path = os.environ.get("CONFIG_FILE", "config.yaml")
    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Starting backtest (config={config_path})...")
    start = time.time()

    try:
        result = run_pipeline(config_path)
    except Exception as e:
        print(f"FATAL: Pipeline failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    elapsed = round(time.time() - start, 1)

    try:
        if isinstance(result, SweepResult):
            print(f"\nSweep complete: {len(result.configs)} configs in {elapsed}s")
            result.save("result.json")
            print(f"Results written to result.json ({len(result.configs)} configs)")
            result.print_leaderboard(top_n=10)
        elif isinstance(result, BacktestResult):
            result.compute()
            print(f"\nBacktest complete in {elapsed}s")
            result.save("result.json")
            print(f"Results written to result.json")
            result.print_summary()
        else:
            print(f"\nPipeline returned unexpected type: {type(result)}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"FATAL: Failed to save results: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

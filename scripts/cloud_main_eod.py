#!/usr/bin/env python3
"""Cloud entry point for EOD pipeline sweeps.

Runs on CR compute. Executes the full EOD pipeline (signal gen + simulation),
writes results to results.json.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from engine.pipeline import run_pipeline


def main():
    config_path = os.environ.get("CONFIG_FILE", "config.yaml")
    if not os.path.exists(config_path):
        print(f"ERROR: {config_path} not found")
        sys.exit(1)

    print(f"Starting EOD sweep on cloud compute (config={config_path})...")
    start = time.time()

    results = run_pipeline(config_path)

    elapsed = round(time.time() - start, 1)
    print(f"\nSweep complete: {len(results)} configs in {elapsed}s")

    # Strip day_wise_log to keep results.json small
    for r in results:
        r.pop("day_wise_log", None)

    with open("results.json", "w") as f:
        json.dump(results, f, default=str)

    print(f"Results written to results.json ({len(results)} configs)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cloud entry point for intraday sweep (ORB, VWAP MR, etc.).

Runs on CR compute. Executes the full intraday pipeline, writes results
to results.json for file-based download (avoids stdout size limits).
"""

import os
import sys
import time

# Set up paths (running inside cloud project root)
sys.path.insert(0, os.getcwd())

from engine.intraday_pipeline import run_intraday_pipeline


def main():
    config_path = os.environ.get("CONFIG_FILE", "config.yaml")
    if not os.path.exists(config_path):
        print(f"ERROR: {config_path} not found")
        sys.exit(1)

    print(f"Starting intraday sweep on cloud compute (config={config_path})...")
    start = time.time()

    sweep = run_intraday_pipeline(config_path)

    elapsed = round(time.time() - start, 1)
    print(f"\nSweep complete: {len(sweep.configs)} configs in {elapsed}s")

    sweep.save("results.json")
    print(f"Results written to results.json ({len(sweep.configs)} configs)")


if __name__ == "__main__":
    main()

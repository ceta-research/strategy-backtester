#!/usr/bin/env python3
"""Cloud entry point for EOD pipeline sweeps.

Runs on CR compute. Executes the full EOD pipeline (signal gen + simulation),
writes results to results.json.
"""

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

    sweep = run_pipeline(config_path)

    elapsed = round(time.time() - start, 1)
    print(f"\nSweep complete: {len(sweep.configs)} configs in {elapsed}s")

    sweep.save("results.json")
    print(f"Results written to results.json ({len(sweep.configs)} configs)")

    # Print compact summary to stdout for parsing when file download unavailable
    import json as _json
    try:
        with open("results.json") as _f:
            _data = _json.load(_f)
        _configs = _data.get("all_configs", []) if isinstance(_data, dict) else _data
        print(f"RESULTS_START")
        print(_json.dumps(_configs, separators=(',', ':')))
        print(f"RESULTS_END")
    except Exception as _e:
        print(f"Could not print results: {_e}")


if __name__ == "__main__":
    main()

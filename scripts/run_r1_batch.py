#!/usr/bin/env python3
"""Run all Round 1 sweeps for momentum_dip_quality sequentially on cloud.

Usage:
    python scripts/run_r1_batch.py [--timeout 1200] [--ram 8192]
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from lib.cloud_orchestrator import CloudOrchestrator

STRATEGY = "momentum_dip_quality"
R1_CONFIGS = [
    ("config_round1_tsl.yaml", "round1_tsl"),
    ("config_round1_momentum.yaml", "round1_momentum"),
    ("config_round1_dip.yaml", "round1_dip"),
    ("config_round1_percentile.yaml", "round1_percentile"),
    ("config_round1_hold.yaml", "round1_hold"),
    ("config_round1_sim.yaml", "round1_sim"),
    ("config_round1_direction.yaml", "round1_direction"),
    ("config_round1_quality_regime.yaml", "round1_quality_regime"),
    ("config_round1_fundamentals.yaml", "round1_fundamentals"),
]


def run_one(orch, config_name, output_name, timeout, ram):
    """Run a single R1 sweep on cloud."""
    config_path = os.path.join(ROOT, "strategies", STRATEGY, config_name)
    output_path = os.path.join(ROOT, "results", STRATEGY, f"{output_name}.json")

    if not os.path.exists(config_path):
        print(f"SKIP: {config_path} not found")
        return False

    if os.path.exists(output_path):
        print(f"SKIP: {output_path} already exists")
        return True

    print(f"\n{'='*60}")
    print(f"Running: {config_name} -> {output_name}.json")
    print(f"{'='*60}")

    project = orch.find_or_create_project(entrypoint="_run_1.py")
    project_id = project["id"]

    # Sync files (only on first run)
    file_paths = orch.discover_files(mode="eod")
    orch.sync_files(project_id, file_paths)

    # Upload entry point and config
    entry_source = os.path.join(ROOT, "scripts", "cloud_main_eod.py")
    orch.upsert_with_retry(project_id, "cloud_main_eod.py", open(entry_source).read())
    orch.upsert_with_retry(project_id, "config.yaml", open(config_path).read())

    wrapper = orch.make_wrapper("cloud_main_eod.py", config_file="config.yaml")
    orch.upsert_with_retry(project_id, "_run_1.py", wrapper)

    # Submit and poll
    run_id = orch.submit_run(project_id, "_run_1.py", cpu=16, ram_mb=ram, timeout=timeout)
    result = orch.poll_run(project_id, run_id, timeout=timeout)

    status = result.get("status", "unknown")
    if status != "completed":
        print(f"FAILED: {status}")
        stderr = result.get("stderr", "")
        if stderr:
            print(f"Stderr: {stderr[-500:]}")
        return False

    # Download results
    results = None
    try:
        results = orch.download_results(run_id)
    except Exception:
        pass

    if not results:
        stdout = result.get("stdout", "")
        if "RESULTS_START" in stdout and "RESULTS_END" in stdout:
            json_str = stdout.split("RESULTS_START\n", 1)[1].split("\nRESULTS_END", 1)[0]
            results = json.loads(json_str)

    if not results:
        print("FAILED: no results")
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    n_configs = len(results) if isinstance(results, list) else len(results.get("all_configs", []))
    print(f"OK: {n_configs} configs -> {output_path}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--ram", type=int, default=8192)
    parser.add_argument("--start-from", type=int, default=0, help="Skip first N configs")
    args = parser.parse_args()

    cr = CetaResearch()
    orch = CloudOrchestrator(cr, project_name="sb-remote")

    total = len(R1_CONFIGS)
    ok = 0
    failed = 0

    for i, (config_name, output_name) in enumerate(R1_CONFIGS):
        if i < args.start_from:
            print(f"SKIP (start-from): {config_name}")
            continue

        t0 = time.time()
        success = run_one(orch, config_name, output_name, args.timeout, args.ram)
        elapsed = round(time.time() - t0)

        if success:
            ok += 1
        else:
            failed += 1

        print(f"  [{i+1}/{total}] {elapsed}s | OK={ok} FAIL={failed}")

    print(f"\n{'='*60}")
    print(f"R1 batch complete: {ok}/{total} OK, {failed} failed")


if __name__ == "__main__":
    main()

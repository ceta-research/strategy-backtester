#!/usr/bin/env python3
"""Upload strategy-backtester code to CR cloud project and run ORB sweep.

Supports parallel batch execution: multiple batches run concurrently on
separate cloud containers (same project, different config files).

Usage:
    python scripts/cloud_sweep.py
    python scripts/cloud_sweep.py --parallel 3 --batch-size 24
    python scripts/cloud_sweep.py --output results/orb_sweep.json
"""

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from engine.intraday_pipeline import SQL_KEYS, SIM_KEYS, SQL_KEYS_V2, SIM_KEYS_V2

PROJECT_NAME = "sb-orb-sweep"

# Files to upload (relative to strategy-backtester root)
ENGINE_FILES = [
    "engine/__init__.py",
    "engine/intraday_pipeline.py",
    "engine/intraday_sql_builder.py",
    "engine/intraday_simulator.py",
    "engine/intraday_simulator_v2.py",
    "engine/charges.py",
    "engine/constants.py",
]

LIB_FILES = [
    "lib/__init__.py",
    "lib/cr_client.py",
    "lib/metrics.py",
]

CODE_FILES = [
    "scripts/cloud_main.py",
]

TERMINAL_STATUSES = {"completed", "failed", "execution_timed_out",
                     "wait_timed_out", "cancelled"}

POLL_INTERVAL = 10  # seconds


def read_file(rel_path):
    with open(os.path.join(ROOT, rel_path)) as f:
        return f.read()


def find_or_create_project(cr):
    projects = cr.list_projects(limit=100)
    for p in projects.get("projects", []):
        if p["name"] == PROJECT_NAME:
            print(f"  Found existing project: {p['id']}")
            return p

    print(f"  Creating project: {PROJECT_NAME}")
    return cr.create_project(
        name=PROJECT_NAME,
        language="python",
        entrypoint="_run_1.py",
        dependencies=["requests", "pyyaml"],
        description="ORB intraday parameter sweep (auto-managed by cloud_sweep.py)",
    )


def make_batch_wrapper(api_key, config_filename):
    """Build per-batch entry point that sets API key and config path."""
    return f"""import sys, os
os.environ["CR_API_KEY"] = {api_key!r}
os.environ["CONFIG_FILE"] = {config_filename!r}
sys.path.insert(0, os.getcwd())
exec(open("cloud_main.py").read())
"""


def upload_code_files(cr, project_id):
    """Upload engine, lib, and cloud_main.py (everything except per-batch files)."""
    all_files = ENGINE_FILES + LIB_FILES + CODE_FILES
    for rel_path in all_files:
        content = read_file(rel_path)
        dest = "cloud_main.py" if rel_path == "scripts/cloud_main.py" else rel_path
        print(f"  Uploading {dest} ({len(content)} bytes)")
        cr.upsert_file(project_id, dest, content)
    print(f"  Uploaded {len(all_files)} code files")


# ------------------------------------------------------------------ #
# Config splitting
# ------------------------------------------------------------------ #

def _get_sql_keys(raw_config):
    """Return the correct SQL key set based on pipeline version."""
    version = raw_config.get("static", {}).get("pipeline_version", "v2")
    return SQL_KEYS_V2 if version != "v1" else SQL_KEYS


def count_sql_combos(raw_config):
    """Count number of unique SQL queries from a config."""
    sql_keys = _get_sql_keys(raw_config)
    sql_params = {}
    for section in ("scanner", "entry", "exit", "simulation"):
        if section in raw_config:
            for key, val in raw_config[section].items():
                vals = val if isinstance(val, list) else [val]
                if key in sql_keys:
                    sql_params[key] = vals
    if not sql_params:
        return 1
    count = 1
    for vals in sql_params.values():
        count *= len(vals)
    return count


def split_config_into_batches(raw_config, batch_size):
    """Split config into batches that each have <= batch_size SQL combos.

    Strategy: find the parameter with the most values and split on it.
    If that's not enough, split recursively.
    """
    n_sql = count_sql_combos(raw_config)
    if n_sql <= batch_size:
        return [raw_config]

    sql_keys = _get_sql_keys(raw_config)

    sql_params = []
    for section in ("scanner", "entry", "exit"):
        if section in raw_config:
            for key, val in raw_config[section].items():
                vals = val if isinstance(val, list) else [val]
                if key in sql_keys and len(vals) > 1:
                    sql_params.append((section, key, vals))

    if not sql_params:
        return [raw_config]

    sql_params.sort(key=lambda x: len(x[2]), reverse=True)
    section, key, vals = sql_params[0]

    batches = []
    for v in vals:
        batch_config = copy.deepcopy(raw_config)
        batch_config[section][key] = [v]
        batches.extend(split_config_into_batches(batch_config, batch_size))

    return batches


# ------------------------------------------------------------------ #
# Parallel batch execution
# ------------------------------------------------------------------ #

def download_results(cr, run_id):
    """Download results.json from a completed run via the file API."""
    content = cr.get_execution_files(run_id, path="results.json")
    return json.loads(content)


def submit_batch(cr, project_id, batch_config, batch_num, total,
                 timeout, cpu, ram, api_key):
    """Upload per-batch config + wrapper, submit run. Returns run_id."""
    config_name = f"config_{batch_num}.yaml"
    wrapper_name = f"_run_{batch_num}.py"

    config_yaml = yaml.dump(batch_config, default_flow_style=False)
    cr.upsert_file(project_id, config_name, config_yaml)

    wrapper = make_batch_wrapper(api_key, config_name)
    cr.upsert_file(project_id, wrapper_name, wrapper)

    n_sql = count_sql_combos(batch_config)
    print(f"  Submitting batch {batch_num}/{total} ({n_sql} SQL configs, "
          f"timeout={timeout}s, cpu={cpu}, ram={ram}MB)...")

    result = cr.run_project(
        project_id,
        entry_path=wrapper_name,
        cpu_count=cpu,
        ram_mb=ram,
        timeout_seconds=timeout,
        poll=False,
    )

    run_id = result.get("id") or result.get("taskId")
    print(f"  Batch {batch_num}/{total} submitted (run_id={run_id})")
    return run_id


def run_batches(cr, project_id, batches, timeout, cpu, ram,
                api_key, max_parallel):
    """Submit batches with parallelism, poll until all complete.

    Submits up to max_parallel batches at once. As runs complete, submits
    more until all batches are done. The CR server also queues excess
    submissions, so this naturally handles tier limits.
    """
    all_results = []
    active = {}  # run_id -> batch_num
    next_idx = 0
    total = len(batches)
    completed_count = 0
    failed_count = 0

    mode = "parallel" if max_parallel > 1 else "sequential"
    print(f"\nStarting {mode} sweep: {total} batches"
          + (f", max {max_parallel} concurrent" if max_parallel > 1 else ""))

    while next_idx < total or active:
        # Fill slots up to max_parallel
        while next_idx < total and len(active) < max_parallel:
            batch_num = next_idx + 1
            try:
                run_id = submit_batch(
                    cr, project_id, batches[next_idx],
                    batch_num, total, timeout, cpu, ram, api_key,
                )
                active[run_id] = batch_num
            except Exception as e:
                err = str(e).lower()
                if "concurrent" in err or "queued" in err or "too many" in err:
                    print(f"  Concurrency limit reached, waiting for slots...")
                    break
                print(f"  Batch {batch_num} submit failed: {e}")
                failed_count += 1
            next_idx += 1

        if not active:
            if next_idx < total:
                time.sleep(POLL_INTERVAL)
                continue
            break

        # Poll all active runs
        time.sleep(POLL_INTERVAL)

        for run_id in list(active.keys()):
            try:
                result = cr.get_run(project_id, run_id)
            except Exception:
                continue  # transient error, retry next poll

            status = result.get("status", "unknown")
            if status not in TERMINAL_STATUSES:
                continue

            batch_num = active.pop(run_id)
            done = completed_count + failed_count + 1

            if status == "completed":
                try:
                    batch_results = download_results(cr, run_id)
                    all_results.extend(batch_results)
                    completed_count += 1
                    print(f"  Batch {batch_num}/{total} complete: "
                          f"{len(batch_results)} results ({done}/{total} done)")
                except Exception as e:
                    print(f"  Batch {batch_num} download failed: {e}")
                    failed_count += 1
            else:
                stderr = result.get("stderr", "")[:200]
                print(f"  Batch {batch_num}/{total} {status}"
                      + (f": {stderr}" if stderr else "")
                      + f" ({done}/{total} done)")
                failed_count += 1

    print(f"\nAll batches done: {completed_count} completed, {failed_count} failed")
    return all_results


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Run ORB sweep on CR cloud compute")
    parser.add_argument("--output", type=str,
                        help="Output JSON path (default: results/orb_sweep_YYYY-MM-DD.json)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Per-batch execution timeout in seconds (default: 1800)")
    parser.add_argument("--cpu", type=int, default=2, help="CPU count (default: 2)")
    parser.add_argument("--ram", type=int, default=4096, help="RAM in MB (default: 4096)")
    parser.add_argument("--batch-size", type=int, default=48,
                        help="Max SQL configs per batch (default: 48)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="Max concurrent batches (default: 1, max depends on tier)")
    args = parser.parse_args()

    output_path = args.output or os.path.join(
        ROOT, "results", f"orb_sweep_{datetime.now().strftime('%Y-%m-%d')}.json"
    )

    # Load and split config
    config_path = os.path.join(ROOT, "strategies", "orb", "config.yaml")
    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    total_sql = count_sql_combos(raw_config)
    batches = split_config_into_batches(raw_config, args.batch_size)
    print(f"Total SQL configs: {total_sql} -> {len(batches)} batches "
          f"(max {args.batch_size} per batch)")

    # Set up cloud project
    cr = CetaResearch()
    print(f"\nSetting up cloud project...")
    project = find_or_create_project(cr)
    project_id = project["id"]

    # Upload shared code files (once)
    upload_code_files(cr, project_id)

    # Run batches (parallel or sequential based on --parallel)
    all_results = run_batches(
        cr, project_id, batches,
        timeout=args.timeout, cpu=args.cpu, ram=args.ram,
        api_key=cr.api_key, max_parallel=args.parallel,
    )

    if not all_results:
        print("\nNo results collected. Check batch errors above.")
        sys.exit(1)

    # Sort merged results by Calmar ratio (descending)
    all_results.sort(key=lambda r: r.get("calmar_ratio") or 0, reverse=True)

    # Save
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"SWEEP COMPLETE: {len(all_results)} configs")
    print(f"{'='*60}")
    print(f"Results saved to: {output_path}")

    if all_results:
        best = all_results[0]
        print(f"Best: {best.get('config_id')}")
        print(f"  CAGR={_pct(best.get('cagr'))} MaxDD={_pct(best.get('max_drawdown'))} "
              f"Calmar={_fmt(best.get('calmar_ratio'))} Sharpe={_fmt(best.get('sharpe_ratio'))}")


def _pct(v):
    return f"{v * 100:.1f}%" if v is not None else "N/A"


def _fmt(v):
    return f"{v:.3f}" if v is not None else "N/A"


if __name__ == "__main__":
    main()

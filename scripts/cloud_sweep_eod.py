#!/usr/bin/env python3
"""Upload EOD strategy code to CR cloud project and run sweep.

Usage:
    python scripts/cloud_sweep_eod.py strategies/extended_ibs/config_us_protected.yaml
    python scripts/cloud_sweep_eod.py strategies/extended_ibs/config_nse_native_protected.yaml --timeout 600
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from glob import glob

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch

PROJECT_NAME = "sb-eod-sweep"

# Files to upload for EOD pipeline
ENGINE_FILES = [
    "engine/__init__.py",
    "engine/pipeline.py",
    "engine/config_loader.py",
    "engine/config_sweep.py",
    "engine/simulator.py",
    "engine/ranking.py",
    "engine/scanner.py",
    "engine/order_generator.py",
    "engine/utils.py",
    "engine/charges.py",
    "engine/constants.py",
    "engine/data_provider.py",
]

SIGNAL_FILES = [
    "engine/signals/__init__.py",
    "engine/signals/base.py",
]

LIB_FILES = [
    "lib/__init__.py",
    "lib/cr_client.py",
    "lib/metrics.py",
    "lib/backtest_result.py",
]

TERMINAL_STATUSES = {"completed", "failed", "execution_timed_out",
                     "wait_timed_out", "cancelled"}
POLL_INTERVAL = 15


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
        entrypoint="cloud_main_eod.py",
        dependencies=["requests", "pyyaml", "polars"],
        description="EOD strategy parameter sweep (auto-managed)",
    )


def discover_signal_files():
    """Find all signal generator .py files."""
    pattern = os.path.join(ROOT, "engine", "signals", "*.py")
    files = []
    for f in sorted(glob(pattern)):
        rel = os.path.relpath(f, ROOT)
        files.append(rel)
    return files


def upload_all_files(cr, project_id, config_path):
    """Upload engine, lib, signals, config, and entry point."""
    signal_files = discover_signal_files()
    all_files = ENGINE_FILES + signal_files + LIB_FILES

    for rel_path in all_files:
        full_path = os.path.join(ROOT, rel_path)
        if not os.path.exists(full_path):
            print(f"  SKIP (missing): {rel_path}")
            continue
        content = read_file(rel_path)
        print(f"  Uploading {rel_path} ({len(content):,} bytes)")
        cr.upsert_file(project_id, rel_path, content)

    # Upload cloud entry point
    content = read_file("scripts/cloud_main_eod.py")
    cr.upsert_file(project_id, "cloud_main_eod.py", content)
    print(f"  Uploading cloud_main_eod.py ({len(content)} bytes)")

    # Upload config
    config_content = open(config_path).read()
    cr.upsert_file(project_id, "config.yaml", config_content)
    print(f"  Uploading config.yaml ({len(config_content)} bytes)")

    # Upload wrapper (CR_API_KEY injected by Nomad Variables in container env)
    wrapper = """import sys, os
os.environ["CONFIG_FILE"] = "config.yaml"
sys.path.insert(0, os.getcwd())
exec(open("cloud_main_eod.py").read())
"""
    cr.upsert_file(project_id, "_run_1.py", wrapper)
    print(f"  Uploaded wrapper _run_1.py")

    total = len(all_files) + 3  # +3 for entry point, config, wrapper
    print(f"  Total: {total} files uploaded")


def main():
    parser = argparse.ArgumentParser(description="Run EOD strategy sweep on CR cloud")
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--output", type=str, help="Output JSON path")
    parser.add_argument("--timeout", type=int, default=43200,
                        help="Execution timeout in seconds (default: 43200)")
    parser.add_argument("--cpu", type=int, default=16, help="CPU count (default: 16)")
    parser.add_argument("--ram", type=int, default=61440, help="RAM in MB (default: 61440)")
    args = parser.parse_args()

    config_path = os.path.join(ROOT, args.config) if not os.path.isabs(args.config) else args.config
    strategy_name = os.path.basename(os.path.dirname(config_path))
    config_name = os.path.splitext(os.path.basename(config_path))[0]

    output_path = args.output or os.path.join(
        ROOT, "results", f"{strategy_name}_{config_name}_{datetime.now().strftime('%Y-%m-%d')}.json"
    )

    cr = CetaResearch()
    print(f"\nSetting up cloud project...")
    project = find_or_create_project(cr)
    project_id = project["id"]

    print(f"\nUploading code + config...")
    upload_all_files(cr, project_id, config_path)

    print(f"\nSubmitting run (timeout={args.timeout}s, cpu={args.cpu}, ram={args.ram}MB)...")
    result = cr.run_project(
        project_id,
        entry_path="_run_1.py",
        cpu_count=args.cpu,
        ram_mb=args.ram,
        timeout_seconds=args.timeout,
        poll=False,
    )
    run_id = result.get("id") or result.get("taskId")
    print(f"  Run submitted: {run_id}")

    # Poll until complete
    print(f"\nPolling for results...")
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            status_result = cr.get_run(project_id, run_id)
        except Exception as e:
            print(f"  Poll error: {e}")
            continue

        status = status_result.get("status", "unknown")
        stdout = status_result.get("stdout", "")

        # Print last line of stdout as progress
        lines = stdout.strip().split("\n") if stdout else []
        last_line = lines[-1] if lines else ""
        print(f"  Status: {status} | {last_line[:100]}")

        if status in TERMINAL_STATUSES:
            break

    if status == "completed":
        print(f"\nRun completed. Downloading results...")
        try:
            content = cr.get_execution_files(run_id, path="results.json")
            data = json.loads(content)
            # SweepResult format: extract all_configs list
            if isinstance(data, dict) and data.get("type") == "sweep":
                all_results = data.get("all_configs", [])
            elif isinstance(data, list):
                all_results = data  # Legacy flat list
            else:
                all_results = []
        except Exception as e:
            print(f"  Download failed: {e}")
            print(f"  Stdout:\n{stdout[-2000:]}")
            sys.exit(1)

        all_results.sort(key=lambda r: r.get("calmar_ratio") or 0, reverse=True)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(all_results, f, indent=2, default=str)

        print(f"\n{'='*100}")
        print(f"SWEEP COMPLETE: {len(all_results)} configs")
        print(f"{'='*100}")
        print(f"Results saved to: {output_path}")
        print(f"\n{'Config':<50} {'CAGR':>7} {'MaxDD':>7} {'Calmar':>7} {'Sharpe':>7} {'Sortino':>7}")
        print(f"{'-'*50} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
        for r in all_results[:15]:
            cagr = (r.get('cagr') or 0) * 100
            dd = (r.get('max_drawdown') or 0) * 100
            calmar = r.get('calmar_ratio') or 0
            sharpe = r.get('sharpe_ratio') or 0
            sortino = r.get('sortino_ratio') or 0
            print(f"{r.get('config_id','?'):<50} {cagr:>6.1f}% {dd:>6.1f}% {calmar:>7.2f} {sharpe:>7.2f} {sortino:>7.2f}")
    else:
        print(f"\nRun {status}.")
        stderr = status_result.get("stderr", "")
        if stderr:
            print(f"Stderr:\n{stderr[-2000:]}")
        if stdout:
            print(f"Stdout (last 2000 chars):\n{stdout[-2000:]}")
        sys.exit(1)


if __name__ == "__main__":
    main()

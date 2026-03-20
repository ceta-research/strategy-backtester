#!/usr/bin/env python3
"""Run a backtest script on Ceta Research cloud compute.

Usage:
    # First time: import repo as a project
    python run_remote.py --setup

    # Run a script (syncs latest code from git first)
    python run_remote.py scripts/buy_2day_high.py

    # Run without git sync (use project files as-is)
    python run_remote.py scripts/buy_2day_high.py --no-sync

    # Custom resources
    python run_remote.py scripts/buy_2day_high.py --timeout 600 --ram 4096

    # Download result to custom path
    python run_remote.py scripts/buy_2day_high.py -o results/my_run.json

Prerequisites:
    - Set CR_API_KEY in environment
    - Push code to GitHub (private repo: ceta-research/strategy-backtester)
    - Run --setup once to link the repo as a project
"""

import sys
import os
import json
import argparse
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.cr_client import CetaResearch

PROJECT_CONFIG_FILE = ".remote_project.json"
DEFAULT_REPO = "https://github.com/ceta-research/strategy-backtester"
DEFAULT_TIMEOUT = 600
DEFAULT_RAM_MB = 4096
DEFAULT_DISK_MB = 1024


def load_project_config():
    if os.path.exists(PROJECT_CONFIG_FILE):
        with open(PROJECT_CONFIG_FILE) as f:
            return json.load(f)
    return None


def save_project_config(config):
    with open(PROJECT_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Saved project config to {PROJECT_CONFIG_FILE}")


def setup(cr, repo_url):
    """Import GitHub repo as a CR project."""
    print(f"  Importing {repo_url}...")
    project = cr.import_project_from_git(repo_url)
    project_id = project.get("id") or project.get("projectId")
    print(f"  Project created: {project_id}")

    # Set dependencies
    cr.update_project(project_id, dependencies=["requests"])
    print("  Dependencies set: [requests]")

    config = {"project_id": project_id, "repo_url": repo_url}
    save_project_config(config)
    return config


def run(cr, project_id, entry_path, timeout, ram_mb, disk_mb, sync=True, verbose=True):
    """Sync from git and run the script."""
    if sync:
        print("  Syncing from git...")
        try:
            cr.pull_project_from_git(project_id)
            print("  Synced.")
        except Exception as e:
            print(f"  Sync warning: {e} (continuing with existing files)")

    print(f"  Running {entry_path} (timeout={timeout}s, ram={ram_mb}MB)...")
    result = cr.run_project(
        project_id,
        entry_path=entry_path,
        ram_mb=ram_mb,
        disk_mb=disk_mb,
        timeout_seconds=timeout,
        install_timeout_seconds=120,
        wait_timeout_seconds=300,
        verbose=verbose,
    )

    return result


def download_result(cr, project_id, run_id, output_path):
    """Download result.json from a completed run."""
    files = cr.get_run_files(project_id, run_id)
    if isinstance(files, list):
        file_list = files
    else:
        file_list = files.get("files", [])

    result_file = None
    for f in file_list:
        name = f.get("name") or f.get("path") or ""
        if name.endswith("result.json"):
            result_file = name
            break

    if not result_file:
        print(f"  No result.json found in output files. Available: {[f.get('name', f.get('path', '?')) for f in file_list]}")
        return None

    print(f"  Downloading {result_file}...")
    content = cr.get_execution_files(run_id, path=result_file)

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(content)

    size_kb = len(content) / 1024
    print(f"  Saved to {output_path} ({size_kb:.0f} KB)")
    return output_path


def print_run_summary(result):
    """Print execution summary from the run result."""
    status = result.get("status", "unknown")
    exit_code = result.get("exitCode", "?")
    exec_time = result.get("executionTimeMs", 0)
    install_time = result.get("installTimeMs", 0)

    print(f"\n  Status: {status} (exit {exit_code})")
    print(f"  Execution: {exec_time/1000:.1f}s, Install: {install_time/1000:.1f}s")

    stdout = result.get("stdout", "")
    if stdout:
        # Print last 2000 chars of stdout (summary)
        if len(stdout) > 2000:
            print(f"\n  ... (truncated, showing last 2000 chars)")
            stdout = stdout[-2000:]
        print(stdout)

    stderr = result.get("stderr", "")
    if stderr and status != "completed":
        print(f"\n  STDERR:\n{stderr[:2000]}")


def main():
    parser = argparse.ArgumentParser(description="Run backtests on Ceta Research cloud")
    parser.add_argument("script", nargs="?", help="Script to run (e.g. scripts/buy_2day_high.py)")
    parser.add_argument("--setup", action="store_true", help="Import repo as a project (first time)")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo URL")
    parser.add_argument("--no-sync", action="store_true", help="Skip git sync before run")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Execution timeout (seconds)")
    parser.add_argument("--ram", type=int, default=DEFAULT_RAM_MB, help="RAM in MB")
    parser.add_argument("--disk", type=int, default=DEFAULT_DISK_MB, help="Disk in MB")
    parser.add_argument("-o", "--output", default=None, help="Output path for result.json")
    args = parser.parse_args()

    cr = CetaResearch()

    if args.setup:
        setup(cr, args.repo)
        return

    if not args.script:
        parser.error("Script path required (e.g. scripts/buy_2day_high.py)")

    config = load_project_config()
    if not config:
        print("  No project configured. Run: python run_remote.py --setup")
        sys.exit(1)

    project_id = config["project_id"]

    # Run
    result = run(cr, project_id, args.script,
                 timeout=args.timeout, ram_mb=args.ram, disk_mb=args.disk,
                 sync=not args.no_sync)

    print_run_summary(result)

    # Download result.json if execution succeeded
    if result.get("status") == "completed":
        run_id = result.get("id") or result.get("taskId")
        output = args.output or f"results/{os.path.basename(args.script).replace('.py', '')}_{int(time.time())}.json"
        download_result(cr, project_id, run_id, output)
    else:
        print(f"\n  Run did not complete successfully (status: {result.get('status')})")
        sys.exit(1)


if __name__ == "__main__":
    main()

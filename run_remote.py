#!/usr/bin/env python3
"""Run a backtest script on Ceta Research cloud compute.

Usage:
    # First time: create project and upload files
    python run_remote.py --setup

    # Run a script (uploads changed files first)
    python run_remote.py scripts/buy_2day_high.py

    # Skip file sync (use project files as-is)
    python run_remote.py scripts/buy_2day_high.py --no-sync

    # Custom resources
    python run_remote.py scripts/buy_2day_high.py --timeout 600 --ram 4096

    # Download result to custom path
    python run_remote.py scripts/buy_2day_high.py -o results/my_run.json

    # Link to GitHub repo (optional, for git-sync)
    python run_remote.py --setup --repo https://github.com/ceta-research/strategy-backtester

Prerequisites:
    - Set CR_API_KEY in environment
"""

import sys
import os
import json
import argparse
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.cr_client import CetaResearch

PROJECT_CONFIG_FILE = ".remote_project.json"
DEFAULT_REPO = "https://github.com/ceta-research/strategy-backtester"
DEFAULT_TIMEOUT = 600
DEFAULT_RAM_MB = 4096
DEFAULT_DISK_MB = 1024

# Files to upload to the project (relative to repo root)
PROJECT_FILES = [
    "lib/__init__.py",
    "lib/cr_client.py",
    "lib/metrics.py",
    "lib/data_utils.py",
    "lib/backtest_result.py",
    "engine/__init__.py",
    "engine/charges.py",
    "engine/constants.py",
]


def load_project_config():
    if os.path.exists(PROJECT_CONFIG_FILE):
        with open(PROJECT_CONFIG_FILE) as f:
            return json.load(f)
    return None


def save_project_config(config):
    with open(PROJECT_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Saved project config to {PROJECT_CONFIG_FILE}")


def file_hash(path):
    """SHA256 of a file's contents."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def setup(cr, repo_url=None):
    """Create a project and upload all files."""
    if repo_url:
        # Try git import first
        print(f"  Importing from {repo_url}...")
        try:
            project = cr.import_project_from_git(repo_url)
            project_id = project.get("id") or project.get("projectId")
            print(f"  Project imported: {project_id}")
            cr.update_project(project_id, dependencies=["requests"])
            config = {"project_id": project_id, "repo_url": repo_url, "mode": "git"}
            save_project_config(config)
            return config
        except Exception as e:
            print(f"  Git import failed: {e}")
            print("  Falling back to file upload mode...")

    # Create project and upload files
    print("  Creating project...")
    project = cr.create_project(
        name="strategy-backtester",
        language="python",
        entrypoint="scripts/buy_2day_high.py",
        dependencies=["requests"],
        description="Position-level trading strategy backtester",
    )
    project_id = project.get("id") or project.get("projectId")
    print(f"  Project created: {project_id}")

    # Upload all library files + scripts
    hashes = upload_files(cr, project_id)

    config = {
        "project_id": project_id,
        "mode": "upload",
        "file_hashes": hashes,
    }
    if repo_url:
        config["repo_url"] = repo_url
    save_project_config(config)
    return config


def upload_files(cr, project_id, script=None):
    """Upload library files and optionally a specific script."""
    files_to_upload = list(PROJECT_FILES)

    # Always include all scripts
    scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    if os.path.isdir(scripts_dir):
        for f in os.listdir(scripts_dir):
            if f.endswith(".py"):
                files_to_upload.append(f"scripts/{f}")

    # If a specific script was requested, make sure it's included
    if script and script not in files_to_upload:
        files_to_upload.append(script)

    hashes = {}
    uploaded = 0
    for rel_path in files_to_upload:
        abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)
        if not os.path.exists(abs_path):
            continue
        with open(abs_path) as f:
            content = f.read()
        try:
            cr.upsert_file(project_id, rel_path, content)
            hashes[rel_path] = file_hash(abs_path)
            uploaded += 1
        except Exception as e:
            print(f"  Warning: failed to upload {rel_path}: {e}")

    print(f"  Uploaded {uploaded} files")
    return hashes


def sync_files(cr, project_id, config, script=None):
    """Upload only files that changed since last sync."""
    old_hashes = config.get("file_hashes", {})

    files_to_check = list(PROJECT_FILES)
    scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
    if os.path.isdir(scripts_dir):
        for f in os.listdir(scripts_dir):
            if f.endswith(".py"):
                files_to_check.append(f"scripts/{f}")
    if script and script not in files_to_check:
        files_to_check.append(script)

    changed = []
    new_hashes = {}
    for rel_path in files_to_check:
        abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)
        if not os.path.exists(abs_path):
            continue
        h = file_hash(abs_path)
        new_hashes[rel_path] = h
        if h != old_hashes.get(rel_path):
            changed.append(rel_path)

    if not changed:
        print("  All files up to date")
        return

    print(f"  Syncing {len(changed)} changed file(s)...")
    for rel_path in changed:
        abs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)
        with open(abs_path) as f:
            content = f.read()
        try:
            cr.upsert_file(project_id, rel_path, content)
            print(f"    {rel_path}")
        except Exception as e:
            print(f"    {rel_path} FAILED: {e}")

    # Update config with new hashes
    config["file_hashes"] = new_hashes
    save_project_config(config)


def inject_api_key(cr, project_id):
    """Write .env with API key so scripts can authenticate inside the container."""
    api_key = cr.api_key
    env_content = f"CR_API_KEY={api_key}\n"
    try:
        cr.upsert_file(project_id, ".env", env_content)
    except Exception as e:
        print(f"  Warning: failed to inject API key: {e}")


def run(cr, project_id, entry_path, timeout, ram_mb, disk_mb, verbose=True):
    """Run the script on cloud compute."""
    inject_api_key(cr, project_id)
    print(f"  Running {entry_path} (timeout={timeout}s, ram={ram_mb}MB, disk={disk_mb}MB)...")
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
        names = [f.get("name", f.get("path", "?")) for f in file_list]
        print(f"  No result.json in output. Available files: {names}")
        return None

    print(f"  Downloading {result_file}...")
    content = cr.get_execution_files(run_id, path=result_file)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(content)

    size_kb = len(content) / 1024
    print(f"  Saved to {output_path} ({size_kb:.0f} KB)")
    return output_path


def print_run_summary(result):
    """Print execution summary."""
    status = result.get("status", "unknown")
    exit_code = result.get("exitCode", "?")
    exec_ms = result.get("executionTimeMs", 0)
    install_ms = result.get("installTimeMs", 0)

    print(f"\n  Status: {status} (exit {exit_code})")
    print(f"  Execution: {exec_ms/1000:.1f}s, Install: {install_ms/1000:.1f}s")

    stdout = result.get("stdout", "")
    if stdout:
        if len(stdout) > 3000:
            print(f"\n  ... (showing last 3000 chars of stdout)")
            stdout = stdout[-3000:]
        print(stdout)

    stderr = result.get("stderr", "")
    if stderr and status != "completed":
        print(f"\n  STDERR:\n{stderr[:3000]}")


def main():
    parser = argparse.ArgumentParser(description="Run backtests on Ceta Research cloud")
    parser.add_argument("script", nargs="?", help="Script to run (e.g. scripts/buy_2day_high.py)")
    parser.add_argument("--setup", action="store_true", help="Create project and upload files")
    parser.add_argument("--repo", default=None, help="GitHub repo URL (optional, for git-sync)")
    parser.add_argument("--no-sync", action="store_true", help="Skip file sync before run")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Execution timeout (seconds)")
    parser.add_argument("--ram", type=int, default=DEFAULT_RAM_MB, help="RAM in MB")
    parser.add_argument("--disk", type=int, default=DEFAULT_DISK_MB, help="Disk in MB")
    parser.add_argument("-o", "--output", default=None, help="Output path for result.json")
    args = parser.parse_args()

    cr = CetaResearch()

    if args.setup:
        setup(cr, repo_url=args.repo)
        return

    if not args.script:
        parser.error("Script path required (e.g. scripts/buy_2day_high.py)")

    config = load_project_config()
    if not config:
        print("  No project configured. Run: python run_remote.py --setup")
        sys.exit(1)

    project_id = config["project_id"]

    # Sync changed files
    if not args.no_sync:
        if config.get("mode") == "git" and config.get("repo_url"):
            print("  Syncing from git...")
            try:
                cr.pull_project_from_git(project_id)
                print("  Synced.")
            except Exception as e:
                print(f"  Git sync failed: {e}, falling back to file upload")
                sync_files(cr, project_id, config, script=args.script)
        else:
            sync_files(cr, project_id, config, script=args.script)

    # Run
    result = run(cr, project_id, args.script,
                 timeout=args.timeout, ram_mb=args.ram, disk_mb=args.disk)

    print_run_summary(result)

    # Download result.json
    if result.get("status") == "completed":
        # Prefer taskId (code execution ID) over id (project run ID) for file downloads
        run_id = result.get("taskId") or result.get("id")
        output = args.output or f"results/{os.path.basename(args.script).replace('.py', '')}_{int(time.time())}.json"
        download_result(cr, project_id, run_id, output)
    else:
        print(f"\n  Run did not complete (status: {result.get('status')})")
        sys.exit(1)


if __name__ == "__main__":
    main()

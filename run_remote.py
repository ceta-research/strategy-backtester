#!/usr/bin/env python3
"""Run scripts or pipeline configs on CR cloud compute.

Unified entry point for all remote execution. Auto-detects mode from
the target file extension (.py = standalone script, .yaml = pipeline config).

Usage:
    python run_remote.py --setup                                 # create project, upload all files
    python run_remote.py scripts/buy_2day_high.py                # run standalone script
    python run_remote.py scripts/buy_2day_high.py --timeout 600 --ram 8192
    python run_remote.py scripts/buy_2day_high.py --no-sync      # skip file upload
    python run_remote.py scripts/buy_2day_high.py -o results/out.json
    python run_remote.py scripts/buy_2day_high.py --env MARKET=jpx
    python run_remote.py strategies/ibs_mean_reversion/config.yaml   # EOD pipeline
    python run_remote.py strategies/orb/config.yaml --intraday       # intraday pipeline
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from lib.cloud_orchestrator import CloudOrchestrator


def parse_env_vars(env_list):
    """Parse ['KEY=VALUE', ...] into dict."""
    if not env_list:
        return None
    result = {}
    for item in env_list:
        if "=" not in item:
            print(f"Warning: ignoring invalid --env value: {item}")
            continue
        k, v = item.split("=", 1)
        result[k] = v
    return result or None


def detect_intraday(config_path):
    """Check if a YAML config is for the intraday pipeline."""
    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f)
        stype = config.get("static", {}).get("strategy_type", "")
        if "intraday" in stype.lower() or "orb" in stype.lower():
            return True
        granularity = config.get("static", {}).get("data_granularity", "day")
        if granularity in ("minute", "1min", "5min", "15min"):
            return True
    except Exception:
        pass
    return False


def setup_mode(orch, args):
    """Create project and upload all files."""
    project = orch.find_or_create_project(
        description="strategy-backtester remote execution",
    )
    project_id = project["id"]

    file_paths = orch.discover_files(mode="all")
    print(f"\nUploading {len(file_paths)} files...")
    orch.sync_files(project_id, file_paths, force=True)

    # Upload cloud entry points
    for entry in ["scripts/cloud_main_eod.py", "scripts/cloud_main.py"]:
        full = os.path.join(ROOT, entry)
        if os.path.exists(full):
            dest = os.path.basename(entry)
            orch.upsert_with_retry(project_id, dest, open(full).read())

    if args.repo:
        try:
            orch.cr.import_project_from_git(args.repo)
            print(f"  Linked to git repo: {args.repo}")
        except Exception as e:
            print(f"  Git link failed: {e}")

    print(f"\nSetup complete. Project: {project_id}")


def run_script(orch, target, args, env_vars):
    """Run a standalone .py script on cloud."""
    project = orch.find_or_create_project()
    project_id = project["id"]

    if not args.no_sync:
        file_paths = orch.discover_files(mode="all")
        print(f"\nSyncing {len(file_paths)} files...")
        orch.sync_files(project_id, file_paths)

    # Upload the target script itself
    print(f"\nUploading {target}...")
    orch.upsert_with_retry(project_id, target, open(os.path.join(ROOT, target)).read())

    # Generate and upload wrapper
    wrapper = orch.make_wrapper(target, env_vars=env_vars)
    orch.upsert_with_retry(project_id, "_run_remote.py", wrapper)

    # Submit and poll
    print(f"\nSubmitting (timeout={args.timeout}s, ram={args.ram}MB"
          + (f", cpu={args.cpu}" if args.cpu else "") + ")...")
    run_id = orch.submit_run(
        project_id, "_run_remote.py",
        cpu=args.cpu or 8, ram_mb=args.ram, timeout=args.timeout,
    )

    result = orch.poll_run(project_id, run_id, timeout=args.timeout)
    return _handle_result(orch, result, run_id, target, args)


def run_config(orch, target, args, env_vars):
    """Run a pipeline YAML config on cloud."""
    config_path = os.path.join(ROOT, target) if not os.path.isabs(target) else target
    is_intraday = args.intraday or detect_intraday(config_path)

    mode = "intraday" if is_intraday else "eod"
    entry_point = "cloud_main.py" if is_intraday else "cloud_main_eod.py"
    entry_source = f"scripts/{entry_point}"

    project = orch.find_or_create_project(entrypoint="_run_1.py")
    project_id = project["id"]

    if not args.no_sync:
        file_paths = orch.discover_files(mode=mode)
        print(f"\nSyncing {len(file_paths)} files ({mode} mode)...")
        orch.sync_files(project_id, file_paths)

    # Upload cloud entry point, config, and wrapper
    print(f"\nUploading config + entry point...")
    orch.upsert_with_retry(
        project_id, entry_point,
        open(os.path.join(ROOT, entry_source)).read(),
    )
    orch.upsert_with_retry(
        project_id, "config.yaml",
        open(config_path).read(),
    )

    wrapper = orch.make_wrapper(
        entry_point,
        config_file="config.yaml",
        env_vars=env_vars,
    )
    orch.upsert_with_retry(project_id, "_run_1.py", wrapper)

    # Submit and poll
    print(f"\nSubmitting {mode} pipeline (timeout={args.timeout}s, ram={args.ram}MB"
          + (f", cpu={args.cpu}" if args.cpu else "") + ")...")
    run_id = orch.submit_run(
        project_id, "_run_1.py",
        cpu=args.cpu or 16, ram_mb=args.ram, timeout=args.timeout,
    )

    result = orch.poll_run(project_id, run_id, timeout=args.timeout)
    return _handle_result(orch, result, run_id, target, args)


def _handle_result(orch, result, run_id, target, args):
    """Download results from a completed run."""
    status = result.get("status", "unknown")

    if status != "completed":
        print(f"\nRun {status}.")
        stderr = result.get("stderr", "")
        if stderr:
            print(f"Stderr:\n{stderr[-2000:]}")
        stdout = result.get("stdout", "")
        if stdout:
            print(f"Stdout (last 2000 chars):\n{stdout[-2000:]}")
        return 1

    print(f"\nDownloading results...")
    results = None
    try:
        results = orch.download_results(run_id)
    except Exception as e:
        print(f"Download failed: {e}, trying stdout fallback...")

    # Fallback: parse results from stdout (RESULTS_START/RESULTS_END markers)
    if not results:
        stdout = result.get("stdout", "")
        if "RESULTS_START" in stdout and "RESULTS_END" in stdout:
            json_str = stdout.split("RESULTS_START\n", 1)[1].split("\nRESULTS_END", 1)[0]
            results = json.loads(json_str)
            print(f"  Parsed {len(results)} configs from stdout")
        else:
            if stdout:
                print(f"Stdout (last 2000 chars):\n{stdout[-2000:]}")
            return 1

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        from datetime import datetime
        base = os.path.splitext(os.path.basename(target))[0]
        output_path = os.path.join(ROOT, "results", f"{base}_{datetime.now().strftime('%Y-%m-%d')}.json")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nDone: {len(results)} configs -> {output_path}")

    # Print top results
    if results:
        sorted_r = sorted(results, key=lambda r: r.get("calmar_ratio") or 0, reverse=True)
        for r in sorted_r[:5]:
            cagr = (r.get("cagr") or 0) * 100
            dd = (r.get("max_drawdown") or 0) * 100
            calmar = r.get("calmar_ratio") or 0
            print(f"  {r.get('config_id', '?')[:55]}: CAGR={cagr:.1f}% MDD={dd:.1f}% Cal={calmar:.2f}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Run scripts/configs on CR cloud compute",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python run_remote.py --setup
  python run_remote.py scripts/buy_2day_high.py
  python run_remote.py scripts/buy_2day_high.py --timeout 600 --ram 8192
  python run_remote.py strategies/ibs_mean_reversion/config.yaml
  python run_remote.py strategies/orb/config.yaml --intraday""",
    )

    parser.add_argument("target", nargs="?",
                        help="Script (.py) or config (.yaml) to run remotely")
    parser.add_argument("--setup", action="store_true",
                        help="Create project and upload all files")
    parser.add_argument("--repo", type=str,
                        help="GitHub repo URL for git-sync setup")
    parser.add_argument("--no-sync", action="store_true",
                        help="Skip file upload (use project files as-is)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Execution timeout in seconds (default: 600)")
    parser.add_argument("--ram", type=int, default=4096,
                        help="RAM in MB (default: 4096)")
    parser.add_argument("--cpu", type=int, default=None,
                        help="CPU count (default: auto)")
    parser.add_argument("-o", "--output", type=str,
                        help="Local output path for results")
    parser.add_argument("--env", type=str, nargs="*",
                        help="Environment variables (KEY=VALUE)")
    parser.add_argument("--intraday", action="store_true",
                        help="Force intraday pipeline mode for .yaml targets")

    args = parser.parse_args()
    env_vars = parse_env_vars(args.env)

    cr = CetaResearch()
    orch = CloudOrchestrator(cr, project_name="sb-remote")

    if args.setup:
        setup_mode(orch, args)
        return 0

    if not args.target:
        parser.print_help()
        return 1

    target = args.target

    if target.endswith(".yaml") or target.endswith(".yml"):
        return run_config(orch, target, args, env_vars)
    elif target.endswith(".py"):
        return run_script(orch, target, args, env_vars)
    else:
        print(f"Error: target must be .py or .yaml (got: {target})")
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)

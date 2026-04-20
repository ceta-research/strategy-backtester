#!/usr/bin/env python3
"""Run all R1 sweeps for momentum_dip_quality.

Handles the 4-config batch limit by splitting large sweeps automatically.
Each entry/exit sweep generates ~32K orders per config, so max 4 configs
per batch to stay under 128K orders (16GB RAM limit).

Sim-only sweeps (sorting × positions) share the same 32K orders and can
run with more configs per batch.

Usage:
    python scripts/run_mdq_r1.py [--start-from momentum] [--only tsl,momentum]
"""

import argparse
import json
import os
import sys
import time
import yaml
import copy

# Force unbuffered output so we can see progress in real time
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from lib.cloud_orchestrator import CloudOrchestrator

STRATEGY = "momentum_dip_quality"
RESULTS_DIR = os.path.join(ROOT, "results", STRATEGY)
STRATEGY_DIR = os.path.join(ROOT, "strategies", STRATEGY)

# Base config template (no-fundamentals baseline with 600d prefetch)
BASE_CONFIG = {
    "static": {
        "strategy_type": "momentum_dip_quality",
        "start_margin": 10000000,
        "start_epoch": 1262304000,
        "end_epoch": 1773878400,
        "prefetch_days": 600,
        "data_granularity": "day",
        "data_provider": "nse_charting",
    },
    "scanner": {
        "instruments": [[{"exchange": "NSE", "symbols": []}]],
        "price_threshold": [50],
        "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
        "n_day_gain_threshold": [{"n": 360, "threshold": -999}],
    },
    "entry": {
        "consecutive_positive_years": [2],
        "min_yearly_return_pct": [0],
        "momentum_lookback_days": [63],
        "momentum_percentile": [0.30],
        "rerank_interval_days": [63],
        "dip_threshold_pct": [5],
        "peak_lookback_days": [63],
        "rescreen_interval_days": [63],
        "roe_threshold": [0],
        "pe_threshold": [0],
        "de_threshold": [0],
        "fundamental_missing_mode": ["skip"],
        "regime_instrument": ["NSE:NIFTYBEES"],
        "regime_sma_period": [200],
        "direction_score_n_day_ma": [3],
        "direction_score_threshold": [0.54],
    },
    "exit": {
        "trailing_stop_pct": [10],
        "max_hold_days": [504],
        "require_peak_recovery": [True],
    },
    "simulation": {
        "default_sorting_type": ["top_gainer"],
        "order_sorting_type": ["top_gainer"],
        "order_ranking_window_days": [30],
        "max_positions": [10],
        "max_positions_per_instrument": [1],
        "order_value_multiplier": [1.0],
        "max_order_value": [{"type": "fixed", "value": 1000000000}],
    },
}

# R1 sweep definitions: param -> values to sweep
# Each sweep varies ONE param (or a small set) while fixing everything else at baseline
R1_SWEEPS = {
    "momentum": {
        "section": "entry",
        "param": "momentum_lookback_days",
        "values": [21, 42, 63, 126, 189, 252, 378, 504],
        "max_per_batch": 1,  # entry param: each config adds columns to 6M-row df, OOMs at >1
    },
    "percentile": {
        "section": "entry",
        "param": "momentum_percentile",
        "values": [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60],
        "max_per_batch": 1,
    },
    "dip": {
        "section": "entry",
        "param": "dip_threshold_pct",
        "values": [2, 3, 4, 5, 7, 10, 15, 20],
        "max_per_batch": 1,
    },
    "hold": {
        "section": "exit",
        "params": {
            "max_hold_days": [63, 126, 252, 378, 504, 756],
            "require_peak_recovery": [True, False],
        },
        "max_per_batch": 4,  # exit param: multiplies orders but shares df_signals
    },
    "sim": {
        "section": "simulation",
        "params": {
            "order_sorting_type": ["top_gainer", "top_performer", "top_average_txn", "top_dipper"],
            "max_positions": [5, 8, 10, 15, 20, 30],
        },
        "max_per_batch": 24,  # sim params share same orders, can do more
    },
    "direction": {
        "section": "entry",
        "params": {
            "direction_score_n_day_ma": [0, 3, 5],
            "direction_score_threshold": [0, 0.40, 0.54, 0.60],
        },
        "max_per_batch": 1,  # entry param
    },
    "quality_regime": {
        "section": "entry",
        "params": {
            "consecutive_positive_years": [1, 2, 3, 4],
            "regime_sma_period": [0, 100, 200],
        },
        "max_per_batch": 1,  # entry param
    },
}


def make_config(sweep_name, sweep_def, batch_values=None):
    """Build a YAML config for a sweep batch."""
    cfg = copy.deepcopy(BASE_CONFIG)

    if "param" in sweep_def:
        # Single param sweep
        section = sweep_def["section"]
        param = sweep_def["param"]
        values = batch_values or sweep_def["values"]
        cfg[section][param] = values
    elif "params" in sweep_def:
        # Multi-param sweep
        section = sweep_def["section"]
        for param, values in sweep_def["params"].items():
            cfg[section][param] = values

    return cfg


def count_configs(cfg):
    """Count total config combinations."""
    total = 1
    for section in ["scanner", "entry", "exit", "simulation"]:
        for key, val in cfg.get(section, {}).items():
            if isinstance(val, list):
                total *= len(val)
    return total


def run_batch(orch, pid, cfg, name, timeout=3600, ram_mb=32768, max_retries=3):
    """Upload config and run on cloud with retry. Returns list of config dicts or None."""
    yaml_str = yaml.dump(cfg, default_flow_style=False)
    orch.upsert_with_retry(pid, "config.yaml", yaml_str)
    wrapper = orch.make_wrapper("cloud_main_eod.py", config_file="config.yaml")
    orch.upsert_with_retry(pid, "_run_1.py", wrapper)

    n_configs = count_configs(cfg)

    for attempt in range(max_retries):
        if attempt > 0:
            print(f"  Retry {attempt}/{max_retries}...")
            time.sleep(10)  # Brief pause between retries

        print(f"  Submitting {name} ({n_configs} configs, {timeout}s, {ram_mb}MB)...")
        run_id = orch.submit_run(pid, "_run_1.py", cpu=12, ram_mb=ram_mb, timeout=timeout)
        result = orch.poll_run(pid, run_id, timeout=timeout)

        status = result.get("status", "?")
        em = result.get("executionTimeMs", "?")
        errmsg = result.get("errorMessage", "")
        print(f"  Status: {status} (exec={em}ms) {errmsg}")

        # Transient failures: retry
        if status == "failed" and (
            "Dependency" in errmsg or "Timeout" in errmsg or str(em) == "0"
        ):
            print(f"  Transient failure, will retry...")
            continue

        if status != "completed":
            out = result.get("stdout", "")
            err = result.get("stderr", "")
            if out:
                for l in out.strip().split("\n")[-15:]:
                    print(f"    {l}")
            if err and status != "completed":
                print(f"    ERR: {err[-300:]}")
            return None

        out = result.get("stdout", "")
        if "RESULTS_START" in out and "RESULTS_END" in out:
            json_str = out.split("RESULTS_START\n", 1)[1].split("\nRESULTS_END", 1)[0]
            return json.loads(json_str)

        # Try downloading
        try:
            return orch.download_results(run_id)
        except Exception:
            pass

        print(f"  WARNING: Could not parse results")
        return None

    print(f"  FAILED after {max_retries} retries")
    return None


def run_sweep(orch, pid, sweep_name, sweep_def):
    """Run a complete R1 sweep, splitting into batches if needed."""
    output_path = os.path.join(RESULTS_DIR, f"round1_{sweep_name}.json")
    if os.path.exists(output_path):
        with open(output_path) as f:
            existing = json.load(f)
        n = len(existing) if isinstance(existing, list) else len(existing.get("all_configs", []))
        print(f"\n  SKIP {sweep_name}: {output_path} exists ({n} configs)")
        return True

    max_per_batch = sweep_def.get("max_per_batch", 4)

    if "param" in sweep_def:
        # Single param: split values into batches
        values = sweep_def["values"]
        if len(values) <= max_per_batch:
            # Single batch
            cfg = make_config(sweep_name, sweep_def)
            configs = run_batch(orch, pid, cfg, sweep_name)
            if configs is None:
                return False
            all_configs = configs
        else:
            # Split into batches - continue on failure, save partial results
            all_configs = []
            failed_batches = 0
            for i in range(0, len(values), max_per_batch):
                batch_values = values[i:i + max_per_batch]
                batch_name = f"{sweep_name}_batch{i // max_per_batch + 1}"
                cfg = make_config(sweep_name, sweep_def, batch_values=batch_values)
                configs = run_batch(orch, pid, cfg, batch_name)
                if configs is None:
                    print(f"  FAILED: {batch_name} (value={batch_values})")
                    failed_batches += 1
                else:
                    all_configs.extend(configs)
            if not all_configs:
                return False
            if failed_batches:
                print(f"  WARNING: {failed_batches} batches failed, saving {len(all_configs)} partial results")
    elif "params" in sweep_def:
        if max_per_batch >= 24:
            # Sim sweep: run all at once
            cfg = make_config(sweep_name, sweep_def)
            configs = run_batch(orch, pid, cfg, sweep_name)
            if configs is None:
                return False
            all_configs = configs
        else:
            # Multi-param entry/exit sweep: need to split
            # Generate all value combinations, then batch
            section = sweep_def["section"]
            param_names = list(sweep_def["params"].keys())
            param_values = list(sweep_def["params"].values())

            from itertools import product
            all_combos = list(product(*param_values))

            all_configs = []
            failed_batches = 0
            for i in range(0, len(all_combos), max_per_batch):
                batch_combos = all_combos[i:i + max_per_batch]
                batch_name = f"{sweep_name}_batch{i // max_per_batch + 1}"

                # Build config with only this batch's values
                cfg = copy.deepcopy(BASE_CONFIG)
                # For each param, collect the unique values in this batch
                for pi, pname in enumerate(param_names):
                    batch_vals = sorted(set(combo[pi] for combo in batch_combos))
                    cfg[section][pname] = batch_vals

                configs = run_batch(orch, pid, cfg, batch_name)
                if configs is None:
                    print(f"  FAILED: {batch_name}")
                    failed_batches += 1
                else:
                    all_configs.extend(configs)
            if not all_configs:
                return False
            if failed_batches:
                print(f"  WARNING: {failed_batches} batches failed, saving {len(all_configs)} partial results")

    # Save merged results
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(all_configs, f, indent=2, default=str)
    print(f"  Saved {len(all_configs)} configs -> {output_path}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-from", type=str, help="Skip sweeps before this name")
    parser.add_argument("--only", type=str, help="Comma-separated list of sweeps to run")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--ram", type=int, default=16384)
    args = parser.parse_args()

    cr = CetaResearch()
    orch = CloudOrchestrator(cr, project_name="sb-remote")
    project = orch.find_or_create_project()
    pid = project["id"]

    # Force sync ALL files first
    print("Force-syncing all files...")
    file_paths = orch.discover_files(mode="eod")
    orch.sync_files(pid, file_paths, force=True)
    orch.upsert_with_retry(pid, "cloud_main_eod.py",
                            open(os.path.join(ROOT, "scripts/cloud_main_eod.py")).read())
    print(f"Synced {len(file_paths)} files\n")

    sweep_names = list(R1_SWEEPS.keys())
    if args.only:
        sweep_names = [s.strip() for s in args.only.split(",")]

    started = args.start_from is None
    ok = 0
    failed = 0

    for name in sweep_names:
        if not started:
            if name == args.start_from:
                started = True
            else:
                print(f"SKIP (start-from): {name}")
                continue

        if name not in R1_SWEEPS:
            print(f"SKIP (unknown): {name}")
            continue

        print(f"\n{'='*60}")
        print(f"R1 SWEEP: {name}")
        print(f"{'='*60}")

        t0 = time.time()
        success = run_sweep(orch, pid, name, R1_SWEEPS[name])
        elapsed = round(time.time() - t0)

        if success:
            ok += 1
        else:
            failed += 1
        print(f"  [{ok + failed}/{len(sweep_names)}] {elapsed}s | OK={ok} FAIL={failed}")

    print(f"\n{'='*60}")
    print(f"R1 complete: {ok} OK, {failed} failed")


if __name__ == "__main__":
    main()

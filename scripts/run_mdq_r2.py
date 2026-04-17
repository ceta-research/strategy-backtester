#!/usr/bin/env python3
"""Run R2 full cross-parameter search for momentum_dip_quality.

From R1:
  IMPORTANT: TSL (8%), momentum (126d), hold (378d), quality/regime (Cal 1.401 outlier)
  MODERATE: dip (7%), percentile (0.25), positions (10)
  INSENSITIVE: direction_score, sorting_type (locked)

R2 cross: 16 entry combos × 9 exit combos = 144 configs
Each batch: 1 entry combo × 9 exit configs (32GB RAM handles 288K orders)

Usage:
    python scripts/run_mdq_r2.py
"""

import json
import os
import sys
import time
import yaml
import copy
from itertools import product

# Force unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from lib.cloud_orchestrator import CloudOrchestrator

RESULTS_DIR = os.path.join(ROOT, "results", "momentum_dip_quality")
OUTPUT_FILE = os.path.join(RESULTS_DIR, "round2_full.json")

# R2 param grid (from R1 analysis)
ENTRY_GRID = {
    "momentum_lookback_days": [63, 126, 189],     # IMPORTANT: R1 best=126d
    "momentum_percentile": [0.25, 0.30],           # MODERATE: R1 best=0.25
    "dip_threshold_pct": [5, 7],                   # MODERATE: R1 best=7, include 5 (baseline)
    "consecutive_positive_years": [1, 2],           # INVESTIGATE: R1 Cal 1.401 at 1yr
    "regime_sma_period": [0, 200],                  # INVESTIGATE: R1 showed big impact
}

EXIT_GRID = {
    "trailing_stop_pct": [5, 8, 10],               # IMPORTANT: R1 best=8%
    "max_hold_days": [252, 378, 504],               # IMPORTANT: R1 best=378d
}

SIM_GRID = {
    "max_positions": [10, 15],                      # MODERATE: R1 best=10
}

# Fixed params
FIXED_ENTRY = {
    "min_yearly_return_pct": 0,
    "rerank_interval_days": 63,
    "peak_lookback_days": 63,
    "rescreen_interval_days": 63,
    "roe_threshold": 0,
    "pe_threshold": 0,
    "de_threshold": 0,
    "fundamental_missing_mode": "skip",
    "regime_instrument": "NSE:NIFTYBEES",
    "direction_score_n_day_ma": 3,
    "direction_score_threshold": 0.54,
}


def make_config(entry_values):
    """Build YAML config for one entry combo × all exit × all sim."""
    cfg = {
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
        "entry": {},
        "exit": {},
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [30],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1.0],
            "max_order_value": [{"type": "fixed", "value": 1000000000}],
        },
    }

    # Set entry params (single values for swept params, lists for fixed)
    for param, value in entry_values.items():
        cfg["entry"][param] = [value]
    for param, value in FIXED_ENTRY.items():
        cfg["entry"][param] = [value]

    # Exit and sim params will be set per-batch (not here)
    cfg["exit"]["require_peak_recovery"] = [True]

    return cfg


def run_batch(orch, pid, cfg, name, timeout=3600, ram_mb=32768, max_retries=3):
    """Upload config and run with retry."""
    yaml_str = yaml.dump(cfg, default_flow_style=False)
    orch.upsert_with_retry(pid, "config.yaml", yaml_str)
    wrapper = orch.make_wrapper("cloud_main_eod.py", config_file="config.yaml",
                                polars_workaround=True)
    orch.upsert_with_retry(pid, "_run_1.py", wrapper)

    for attempt in range(max_retries):
        if attempt > 0:
            print(f"  Retry {attempt}/{max_retries}...")
            time.sleep(10)

        print(f"  Submitting {name}...")
        run_id = orch.submit_run(pid, "_run_1.py", cpu=12, ram_mb=ram_mb, timeout=timeout)
        result = orch.poll_run(pid, run_id, timeout=timeout)

        status = result.get("status", "?")
        em = result.get("executionTimeMs", "?")
        errmsg = result.get("errorMessage", "")
        print(f"  {status} (exec={em}ms) {errmsg}")

        if status == "failed" and ("Dependency" in errmsg or "Timeout" in errmsg or str(em) == "0"):
            continue  # Transient, retry

        if status == "completed":
            out = result.get("stdout", "")
            if "RESULTS_START" in out and "RESULTS_END" in out:
                json_str = out.split("RESULTS_START\n", 1)[1].split("\nRESULTS_END", 1)[0]
                return json.loads(json_str)
            try:
                return orch.download_results(run_id)
            except Exception:
                pass
            print(f"  WARNING: Could not parse results")

        # Non-transient failure
        out = result.get("stdout", "")
        if out:
            for l in out.strip().split("\n")[-10:]:
                print(f"    {l}")
        return None

    print(f"  FAILED after {max_retries} retries")
    return None


def main():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        print(f"round2_full.json already exists ({len(existing)} configs). Delete to re-run.")
        return

    cr = CetaResearch()
    orch = CloudOrchestrator(cr, project_name="sb-remote")
    project = orch.find_or_create_project()
    pid = project["id"]

    # Force sync
    print("Force-syncing all files...")
    file_paths = orch.discover_files(mode="eod")
    orch.sync_files(pid, file_paths, force=True)
    orch.upsert_with_retry(pid, "cloud_main_eod.py",
                            open(os.path.join(ROOT, "scripts/cloud_main_eod.py")).read())

    # Generate all entry combinations
    entry_params = list(ENTRY_GRID.keys())
    entry_values = list(ENTRY_GRID.values())
    entry_combos = list(product(*entry_values))

    # Generate exit batches: max 3 TSL values per batch (each generates ~32K orders)
    tsl_values = EXIT_GRID["trailing_stop_pct"]
    hold_values = EXIT_GRID["max_hold_days"]
    pos_values = SIM_GRID["max_positions"]
    MAX_EXIT_PER_BATCH = 3  # 3 TSL × 3 hold = 9 exit, ×2 sim = 18 configs, ~96K orders

    # Split TSL into batches of MAX_EXIT_PER_BATCH
    tsl_batches = [tsl_values[i:i+MAX_EXIT_PER_BATCH] for i in range(0, len(tsl_values), MAX_EXIT_PER_BATCH)]

    n_exit = len(tsl_values) * len(hold_values)
    n_sim = len(pos_values)
    total_configs = len(entry_combos) * n_exit * n_sim
    total_batches = len(entry_combos) * len(tsl_batches)
    print(f"\nR2: {len(entry_combos)} entry × {n_exit} exit × {n_sim} sim = {total_configs} configs")
    print(f"Batches: {total_batches} ({len(entry_combos)} entries × {len(tsl_batches)} exit batches)\n")

    all_configs = []
    failed = 0
    batch_num = 0

    for i, combo in enumerate(entry_combos):
        entry_vals = dict(zip(entry_params, combo))
        label = ", ".join(f"{k.split('_')[-1]}={v}" for k, v in entry_vals.items())

        for tb_idx, tsl_batch in enumerate(tsl_batches):
            batch_num += 1
            batch_name = f"r2_{batch_num}/{total_batches} ({label}, tsl={tsl_batch})"

            cfg = make_config(entry_vals)
            cfg["exit"]["trailing_stop_pct"] = tsl_batch
            cfg["exit"]["max_hold_days"] = hold_values
            cfg["simulation"]["max_positions"] = pos_values

            configs = run_batch(orch, pid, cfg, batch_name)

            if configs is None:
                print(f"  FAILED: {batch_name}")
                failed += 1
            else:
                for c in configs:
                    c["entry_params"] = entry_vals
                all_configs.extend(configs)
                print(f"  +{len(configs)} (total: {len(all_configs)})")

        # Save partial results every 10 entry combos
        if (i + 1) % 5 == 0 and all_configs:
            os.makedirs(RESULTS_DIR, exist_ok=True)
            with open(OUTPUT_FILE, "w") as f:
                json.dump(all_configs, f, indent=2, default=str)
            print(f"  [checkpoint] Saved {len(all_configs)} configs")

    # Final save
    if all_configs:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(all_configs, f, indent=2, default=str)
        print(f"\nSaved {len(all_configs)} configs -> {OUTPUT_FILE}")

    print(f"\nR2 complete: {len(all_configs)} configs saved, {failed} entry combos failed")

    # Print top 20 by Calmar
    if all_configs:
        print(f"\n{'='*90}")
        print(f"TOP 20 BY CALMAR")
        print(f"{'='*90}")
        for c in sorted(all_configs, key=lambda x: x.get("calmar_ratio", 0), reverse=True)[:20]:
            cagr = (c.get("cagr") or 0) * 100
            mdd = (c.get("max_drawdown") or 0) * 100
            cal = c.get("calmar_ratio") or 0
            trades = c.get("total_trades", 0)
            ep = c.get("entry_params", {})
            cid = c.get("params", {}).get("config_id", "?")
            label = f"mom={ep.get('momentum_lookback_days','?')},pct={ep.get('momentum_percentile','?')},dip={ep.get('dip_threshold_pct','?')},q={ep.get('consecutive_positive_years','?')},reg={ep.get('regime_sma_period','?')}"
            print(f"  {cid} [{label}]: CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={cal:.3f} Trades={trades}")

        print(f"\n{'='*90}")
        print(f"TOP 20 BY CAGR")
        print(f"{'='*90}")
        for c in sorted(all_configs, key=lambda x: x.get("cagr", 0), reverse=True)[:20]:
            cagr = (c.get("cagr") or 0) * 100
            mdd = (c.get("max_drawdown") or 0) * 100
            cal = c.get("calmar_ratio") or 0
            trades = c.get("total_trades", 0)
            ep = c.get("entry_params", {})
            cid = c.get("params", {}).get("config_id", "?")
            label = f"mom={ep.get('momentum_lookback_days','?')},pct={ep.get('momentum_percentile','?')},dip={ep.get('dip_threshold_pct','?')},q={ep.get('consecutive_positive_years','?')},reg={ep.get('regime_sma_period','?')}"
            print(f"  {cid} [{label}]: CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={cal:.3f} Trades={trades}")


if __name__ == "__main__":
    main()

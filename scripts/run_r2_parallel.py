#!/usr/bin/env python3
"""Run R2 sweep in parallel batches, split by lookback x topn.

Creates 6 batch configs, runs 3 at a time, merges results.
"""

import copy
import json
import os
import subprocess
import sys
import time
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

TEMPLATE = {
    "static": {
        "strategy_type": "momentum_top_gainers",
        "start_margin": 10000000,
        "start_epoch": 1262304000,
        "end_epoch": 1773878400,
        "prefetch_days": 800,
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
        "rebalance_interval_days": [2, 5],
        "min_momentum_pct": [0, 5],
        "direction_score_n_day_ma": [3],
        "direction_score_threshold": [0, 0.45],
        "regime_instrument": ["NSE:NIFTYBEES"],
        "regime_sma_period": [0],
    },
    "exit": {
        "trailing_stop_pct": [25, 40, 60],
        "max_hold_days": [126, 252],
    },
    "simulation": {
        "default_sorting_type": ["top_gainer"],
        "order_sorting_type": ["top_gainer"],
        "order_ranking_window_days": [30],
        "max_positions": [15, 20, 30],
        "max_positions_per_instrument": [1],
        "order_value_multiplier": [1.0],
        "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
    },
}

LOOKBACKS = [63, 189]
TOPNS = [0.40, 0.50, 0.60]
MAX_PARALLEL = 3

RESULTS_DIR = os.path.join(ROOT, "results", "momentum_top_gainers")
CONFIG_DIR = os.path.join(ROOT, "strategies", "momentum_top_gainers")
os.makedirs(RESULTS_DIR, exist_ok=True)


def make_batch_config(lookback, topn):
    cfg = copy.deepcopy(TEMPLATE)
    cfg["entry"]["momentum_lookback_days"] = [lookback]
    cfg["entry"]["top_n_pct"] = [topn]
    return cfg


def run_batch(lookback, topn):
    """Run a single batch, return (lookback, topn, output_path, process)."""
    tag = f"lb{lookback}_tn{int(topn*100)}"
    config_path = os.path.join(CONFIG_DIR, f"config_r2_{tag}.yaml")
    output_path = os.path.join(RESULTS_DIR, f"round2_{tag}.json")

    cfg = make_batch_config(lookback, topn)
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)

    # Count configs
    n_entry = 1 * 1 * 2 * 2 * 1 * 2  # lb * tn * rebal * minmom * ds_ma * ds_thresh
    n_exit = 3 * 2
    n_sim = 3
    total = n_entry * n_exit * n_sim
    print(f"  [{tag}] {total} configs -> {output_path}", flush=True)

    proc = subprocess.Popen(
        [sys.executable, "run.py", "--config", config_path, "--output", output_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return tag, output_path, proc


def main():
    print(f"R2 parallel sweep: {len(LOOKBACKS)}x{len(TOPNS)} = {len(LOOKBACKS)*len(TOPNS)} batches, {MAX_PARALLEL} parallel")
    print(f"Each batch: 8 entry x 6 exit x 3 sim = 144 configs")
    print(f"Total: {len(LOOKBACKS)*len(TOPNS)*144} configs\n")

    batches = [(lb, tn) for lb in LOOKBACKS for tn in TOPNS]
    all_results = []
    batch_idx = 0
    t0 = time.time()

    while batch_idx < len(batches):
        # Launch up to MAX_PARALLEL
        running = []
        for _ in range(min(MAX_PARALLEL, len(batches) - batch_idx)):
            lb, tn = batches[batch_idx]
            tag, out_path, proc = run_batch(lb, tn)
            running.append((tag, out_path, proc, lb, tn))
            batch_idx += 1

        print(f"\nWaiting for {len(running)} batches...", flush=True)

        # Monitor: print CPU/mem every 60s until all done
        while True:
            all_done = True
            for tag, out_path, proc, lb, tn in running:
                if proc.poll() is None:
                    all_done = False
            if all_done:
                break
            time.sleep(30)
            elapsed = int(time.time() - t0)
            status = []
            for tag, out_path, proc, lb, tn in running:
                s = "DONE" if proc.poll() is not None else "running"
                status.append(f"{tag}={s}")
            print(f"  [{elapsed}s] {', '.join(status)}", flush=True)

        # Collect results
        for tag, out_path, proc, lb, tn in running:
            rc = proc.returncode
            stdout_text = proc.stdout.read() if proc.stdout else ""
            # Print last few lines
            lines = stdout_text.strip().split("\n")
            for line in lines[-5:]:
                print(f"  [{tag}] {line}", flush=True)
            if rc != 0:
                print(f"  [{tag}] FAILED (exit {rc})", flush=True)
                continue
            if os.path.exists(out_path):
                all_results.append(out_path)
                sz = os.path.getsize(out_path) / 1024 / 1024
                print(f"  [{tag}] OK ({sz:.1f} MB)", flush=True)
            else:
                print(f"  [{tag}] No output file!", flush=True)

    elapsed = int(time.time() - t0)
    print(f"\n--- All batches done in {elapsed}s ---")
    print(f"Result files: {len(all_results)}")

    # Merge all batch results into one file
    if all_results:
        merged_path = os.path.join(RESULTS_DIR, "round2_full.json")
        merged = {"all_configs": []}
        for path in all_results:
            with open(path) as f:
                data = json.load(f)
            configs = data.get("all_configs", [])
            merged["all_configs"].extend(configs)
            print(f"  {os.path.basename(path)}: {len(configs)} configs")

        # Copy metadata from first file
        first = json.load(open(all_results[0]))
        for k in first:
            if k != "all_configs":
                merged[k] = first[k]

        with open(merged_path, "w") as f:
            json.dump(merged, f, indent=2, default=str)

        total_configs = len(merged["all_configs"])
        sz = os.path.getsize(merged_path) / 1024 / 1024
        print(f"\nMerged: {total_configs} configs -> {merged_path} ({sz:.1f} MB)")

        # Print top 10 by Calmar
        sorted_configs = sorted(
            merged["all_configs"],
            key=lambda c: c.get("summary", {}).get("calmar_ratio") or 0,
            reverse=True
        )
        print(f"\nTop 10 by Calmar:")
        for i, c in enumerate(sorted_configs[:10]):
            s = c.get("summary", {})
            cid = c.get("params", {}).get("config_id", "?")
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            trades = s.get("total_trades") or 0
            print(f"  {i+1}. {cid[:60]} | CAGR={cagr:.1f}% MDD={mdd:.1f}% Cal={cal:.3f} trades={trades}")

        print(f"\nTop 10 by CAGR:")
        sorted_cagr = sorted(
            merged["all_configs"],
            key=lambda c: c.get("summary", {}).get("cagr") or 0,
            reverse=True
        )
        for i, c in enumerate(sorted_cagr[:10]):
            s = c.get("summary", {})
            cid = c.get("params", {}).get("config_id", "?")
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            trades = s.get("total_trades") or 0
            print(f"  {i+1}. {cid[:60]} | CAGR={cagr:.1f}% MDD={mdd:.1f}% Cal={cal:.3f} trades={trades}")


if __name__ == "__main__":
    main()

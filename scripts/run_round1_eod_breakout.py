#!/usr/bin/env python3
"""Run all Round 1 sensitivity sweeps for eod_breakout and produce a summary."""

import json
import os
import sys
import subprocess

STRATEGY_DIR = "strategies/eod_breakout"
RESULTS_DIR = "results/eod_breakout"

SWEEPS = [
    ("trailing_stop_pct", "config_round1_tsl.yaml", "round1_tsl.json"),
    ("n_day_high", "config_round1_n_day_high.yaml", "round1_n_day_high.json"),
    ("n_day_ma", "config_round1_n_day_ma.yaml", "round1_n_day_ma.json"),
    ("ds_score", "config_round1_ds_score.yaml", "round1_ds_score.json"),
    ("ds_ma", "config_round1_ds_ma.yaml", "round1_ds_ma.json"),
    ("min_hold_time_days", "config_round1_min_hold.yaml", "round1_min_hold.json"),
    ("max_positions", "config_round1_max_pos.yaml", "round1_max_pos.json"),
]


def run_sweep(config_name, output_name):
    config_path = os.path.join(STRATEGY_DIR, config_name)
    output_path = os.path.join(RESULTS_DIR, output_name)

    if os.path.exists(output_path):
        print(f"  [SKIP] {output_name} already exists")
        return output_path

    cmd = [sys.executable, "run.py", "--config", config_path, "--output", output_path]
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[-500:]}")
        return None
    print(result.stdout[-300:])
    return output_path


def extract_param_response(json_path, param_name):
    """Extract CAGR/Calmar/MDD for each config in the sweep."""
    with open(json_path) as f:
        data = json.load(f)

    configs = data.get("all_configs", [])
    rows = []
    for c in configs:
        rows.append({
            "config_id": c.get("params", {}).get("config_id", "?"),
            "cagr": (c.get("cagr") or 0) * 100,
            "mdd": (c.get("max_drawdown") or 0) * 100,
            "calmar": c.get("calmar_ratio") or 0,
            "sharpe": c.get("sharpe_ratio") or 0,
            "trades": c.get("total_trades") or 0,
        })
    return rows


def classify_response(rows):
    """Classify param response shape."""
    calmars = [r["calmar"] for r in rows]
    if len(calmars) < 3:
        return "INSUFFICIENT_DATA"

    cal_range = max(calmars) - min(calmars)
    avg_cal = sum(calmars) / len(calmars)

    if avg_cal == 0:
        return "FLAT"

    # Check for monotonic
    increasing = all(calmars[i] <= calmars[i+1] for i in range(len(calmars)-1))
    decreasing = all(calmars[i] >= calmars[i+1] for i in range(len(calmars)-1))

    if increasing:
        return "MONOTONIC_UP"
    if decreasing:
        return "MONOTONIC_DOWN"

    # Check for flat (range < 20% of mean)
    if cal_range < 0.2 * abs(avg_cal):
        return "FLAT/INSENSITIVE"

    # Check for bell curve (peak in middle 60%)
    peak_idx = calmars.index(max(calmars))
    n = len(calmars)
    if n * 0.2 <= peak_idx <= n * 0.8:
        return "BELL_CURVE/IMPORTANT"

    # Check for spike
    if max(calmars) > 2 * avg_cal:
        return "SPIKE"

    return "IMPORTANT"


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("=" * 80)
    print("ROUND 1: eod_breakout sensitivity scan")
    print("=" * 80)

    summary = []

    for param_name, config_name, output_name in SWEEPS:
        print(f"\n--- Sweeping: {param_name} ---")
        json_path = run_sweep(config_name, output_name)
        if json_path and os.path.exists(json_path):
            rows = extract_param_response(json_path, param_name)
            shape = classify_response(rows)
            best = max(rows, key=lambda r: r["calmar"])

            summary.append({
                "param": param_name,
                "shape": shape,
                "best_calmar": best["calmar"],
                "best_cagr": best["cagr"],
                "best_config": best["config_id"],
                "n_configs": len(rows),
            })

            print(f"\n  {param_name} response ({len(rows)} values):")
            for r in rows:
                bar = "#" * max(1, int(r["calmar"] * 20))
                print(f"    config={r['config_id']:>8}  CAGR={r['cagr']:>+6.1f}%  "
                      f"MDD={r['mdd']:>6.1f}%  Cal={r['calmar']:>6.3f}  "
                      f"Shp={r['sharpe']:>5.3f}  Trd={r['trades']:>5}  {bar}")
            print(f"  Shape: {shape}")

    # Final summary table
    print("\n" + "=" * 80)
    print("ROUND 1 SUMMARY")
    print("=" * 80)
    print(f"{'Param':<25} {'Shape':<25} {'Best Cal':>9} {'Best CAGR':>10} {'Best Config':>12}")
    print("-" * 80)
    for s in summary:
        print(f"{s['param']:<25} {s['shape']:<25} {s['best_calmar']:>9.3f} "
              f"{s['best_cagr']:>+9.1f}% {s['best_config']:>12}")


if __name__ == "__main__":
    main()

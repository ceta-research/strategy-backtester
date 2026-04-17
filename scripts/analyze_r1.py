#!/usr/bin/env python3
"""Analyze Round 1 sensitivity scan results for optimization.

Reads one or more Round 1 result JSON files and produces:
1. Marginal analysis table (avg/max CAGR and Calmar per param value)
2. Param classification (IMPORTANT/MODERATE/INSENSITIVE)
3. Monotonicity detection (needs range extension)

Usage:
    python scripts/analyze_r1.py results/momentum_dip_quality/round1_*.json
    python scripts/analyze_r1.py results/momentum_dip_quality/round1_tsl.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def load_configs(paths):
    """Load all config summaries from one or more result files."""
    all_configs = []
    for path in paths:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            all_configs.extend(data)
        elif isinstance(data, dict):
            configs = data.get("all_configs", [])
            all_configs.extend(configs)
    return all_configs


def extract_param_values(configs):
    """Extract sweepable param values from config_id strings."""
    # config_id format: "1_1_1_1" (scanner_entry_exit_sim)
    # We need to parse actual param values from the params dict
    param_groups = defaultdict(lambda: defaultdict(list))

    for cfg in configs:
        params = cfg.get("params", {})
        config_id = params.get("config_id", "")
        cagr = cfg.get("cagr")
        calmar = cfg.get("calmar_ratio")
        mdd = cfg.get("max_drawdown")
        trades = cfg.get("total_trades", 0)

        if cagr is None:
            continue

        # Parse config_id to extract individual config indices
        # The actual param values aren't in the sweep output directly
        # We need to infer from the config_id structure
        metrics = {
            "cagr": cagr,
            "calmar": calmar,
            "mdd": mdd,
            "trades": trades,
            "config_id": config_id,
        }

        # Store by config_id for now
        all_ids = config_id.split("_") if config_id else []
        if len(all_ids) >= 4:
            param_groups["scanner"][all_ids[0]].append(metrics)
            param_groups["entry"][all_ids[1]].append(metrics)
            param_groups["exit"][all_ids[2]].append(metrics)
            param_groups["sim"][all_ids[3]].append(metrics)

    return param_groups


def marginal_analysis(configs, group_by_field=None):
    """Compute marginal analysis: avg metrics per param value.

    When a sweep varies ONE param, the config_id structure allows us to
    isolate its effect. For multi-param sweeps, we group by the varying
    component of the config_id.
    """
    param_groups = extract_param_values(configs)

    print(f"\n{'='*90}")
    print(f"MARGINAL ANALYSIS ({len(configs)} configs)")
    print(f"{'='*90}")

    for section in ["entry", "exit", "sim"]:
        values = param_groups.get(section, {})
        if len(values) <= 1:
            continue

        print(f"\n  --- {section.upper()} config ---")
        print(f"  {'ID':>4}  {'N':>3}  {'AVG_CAGR':>9}  {'MAX_CAGR':>9}  {'AVG_CAL':>8}  {'MAX_CAL':>8}  {'AVG_MDD':>8}  {'TRADES':>7}")
        print(f"  {'-'*65}")

        rows = []
        for val_id, metrics_list in sorted(values.items(), key=lambda x: int(x[0])):
            cagrs = [m["cagr"] for m in metrics_list]
            calmars = [m["calmar"] for m in metrics_list if m["calmar"] is not None]
            mdds = [m["mdd"] for m in metrics_list if m["mdd"] is not None]
            trades = [m["trades"] for m in metrics_list]

            avg_cagr = sum(cagrs) / len(cagrs)
            max_cagr = max(cagrs)
            avg_cal = sum(calmars) / len(calmars) if calmars else 0
            max_cal = max(calmars) if calmars else 0
            avg_mdd = sum(mdds) / len(mdds) if mdds else 0
            avg_trades = sum(trades) / len(trades) if trades else 0

            rows.append((val_id, len(metrics_list), avg_cagr, max_cagr, avg_cal, max_cal, avg_mdd, avg_trades))

            print(f"  {val_id:>4}  {len(metrics_list):>3}  "
                  f"{avg_cagr*100:>+8.1f}%  {max_cagr*100:>+8.1f}%  "
                  f"{avg_cal:>8.3f}  {max_cal:>8.3f}  "
                  f"{avg_mdd*100:>7.1f}%  {avg_trades:>7.0f}")

        # Classification
        if len(rows) >= 2:
            cal_range = max(r[4] for r in rows) - min(r[4] for r in rows)
            cagr_range = max(r[2] for r in rows) - min(r[2] for r in rows)
            pct_range = (cal_range / max(abs(r[4]) for r in rows) * 100) if max(abs(r[4]) for r in rows) > 0 else 0

            # Check monotonicity
            cals = [r[4] for r in rows]
            is_mono_up = all(cals[i] <= cals[i+1] for i in range(len(cals)-1))
            is_mono_down = all(cals[i] >= cals[i+1] for i in range(len(cals)-1))

            if pct_range > 50:
                classification = "IMPORTANT"
            elif pct_range > 20:
                classification = "MODERATE"
            else:
                classification = "INSENSITIVE"

            mono_tag = ""
            if is_mono_up:
                mono_tag = " [MONOTONIC UP - extend range]"
            elif is_mono_down:
                mono_tag = " [MONOTONIC DOWN - extend range]"

            print(f"  -> Calmar range: {cal_range:.3f} ({pct_range:.0f}%) -> {classification}{mono_tag}")


def leaderboard(configs, top_n=20):
    """Print top configs by CAGR and Calmar."""
    print(f"\n{'='*90}")
    print(f"TOP {top_n} BY CALMAR")
    print(f"{'='*90}")
    print(f"  {'#':>3}  {'config_id':<30}  {'CAGR':>8}  {'MDD':>8}  {'Calmar':>7}  {'Trades':>6}")
    print(f"  {'-'*70}")

    by_calmar = sorted(configs, key=lambda c: c.get("calmar_ratio") or 0, reverse=True)
    for i, c in enumerate(by_calmar[:top_n]):
        cid = c.get("params", {}).get("config_id", "?")
        cagr = (c.get("cagr") or 0) * 100
        mdd = (c.get("max_drawdown") or 0) * 100
        cal = c.get("calmar_ratio") or 0
        trades = c.get("total_trades", 0)
        print(f"  {i+1:>3}  {cid:<30}  {cagr:>+7.1f}%  {mdd:>7.1f}%  {cal:>7.3f}  {trades:>6}")

    print(f"\n{'='*90}")
    print(f"TOP {top_n} BY CAGR")
    print(f"{'='*90}")
    print(f"  {'#':>3}  {'config_id':<30}  {'CAGR':>8}  {'MDD':>8}  {'Calmar':>7}  {'Trades':>6}")
    print(f"  {'-'*70}")

    by_cagr = sorted(configs, key=lambda c: c.get("cagr") or 0, reverse=True)
    for i, c in enumerate(by_cagr[:top_n]):
        cid = c.get("params", {}).get("config_id", "?")
        cagr = (c.get("cagr") or 0) * 100
        mdd = (c.get("max_drawdown") or 0) * 100
        cal = c.get("calmar_ratio") or 0
        trades = c.get("total_trades", 0)
        print(f"  {i+1:>3}  {cid:<30}  {cagr:>+7.1f}%  {mdd:>7.1f}%  {cal:>7.3f}  {trades:>6}")


def summary_stats(configs):
    """Print distribution stats."""
    valid = [c for c in configs if c.get("cagr") is not None]
    if not valid:
        print("No valid configs.")
        return

    print(f"\n{'='*90}")
    print(f"SUMMARY ({len(valid)} configs)")
    print(f"{'='*90}")

    for name, key in [("CAGR", "cagr"), ("MDD", "max_drawdown"), ("Calmar", "calmar_ratio"),
                       ("Sharpe", "sharpe_ratio"), ("Trades", "total_trades")]:
        vals = [c[key] for c in valid if c.get(key) is not None]
        if not vals:
            continue
        vals.sort()
        med = vals[len(vals)//2]
        if key in ("cagr", "max_drawdown"):
            print(f"  {name:<10}  min={vals[0]*100:>+7.1f}%  med={med*100:>+7.1f}%  max={vals[-1]*100:>+7.1f}%")
        elif key == "total_trades":
            print(f"  {name:<10}  min={vals[0]:>7.0f}   med={med:>7.0f}   max={vals[-1]:>7.0f}")
        else:
            print(f"  {name:<10}  min={vals[0]:>7.3f}   med={med:>7.3f}   max={vals[-1]:>7.3f}")


def main():
    parser = argparse.ArgumentParser(description="Analyze R1 sweep results")
    parser.add_argument("inputs", nargs="+", help="Result JSON files")
    parser.add_argument("--top", type=int, default=10, help="Top N to show")
    args = parser.parse_args()

    configs = load_configs(args.inputs)
    print(f"Loaded {len(configs)} configs from {len(args.inputs)} files")

    summary_stats(configs)
    leaderboard(configs, top_n=args.top)
    marginal_analysis(configs)


if __name__ == "__main__":
    main()

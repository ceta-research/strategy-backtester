#!/usr/bin/env python3
"""Analyze EOD pipeline sweep results (from SweepResult.save JSON).

Usage:
    python scripts/analyze_eod_sweep.py results/eod_breakout/round1_tsl.json
    python scripts/analyze_eod_sweep.py results/eod_breakout/round2.json --marginal
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def load_results(path):
    with open(path) as f:
        data = json.load(f)
    if data.get("type") == "sweep":
        return data["all_configs"], data
    elif data.get("type") == "single":
        return [{"params": data["strategy"]["params"], **data["summary"]}], data
    else:
        return data, None


def print_leaderboard(configs, top_n=20):
    print(f"\n{'='*110}")
    print(f"LEADERBOARD (top {min(top_n, len(configs))} by Calmar)")
    print(f"{'='*110}")

    valid = [c for c in configs if c.get("calmar_ratio") is not None]
    valid.sort(key=lambda c: c.get("calmar_ratio", 0), reverse=True)

    print(f"{'#':>3}  {'CAGR':>7}  {'MDD':>7}  {'Calmar':>7}  {'Sharpe':>7}  "
          f"{'WinR':>5}  {'Trades':>6}  {'AvgHold':>7}  {'PF':>5}  Config")
    print("-" * 110)

    for i, c in enumerate(valid[:top_n]):
        cagr = (c.get("cagr") or 0) * 100
        mdd = (c.get("max_drawdown") or 0) * 100
        cal = c.get("calmar_ratio") or 0
        sh = c.get("sharpe_ratio") or 0
        wr = (c.get("win_rate") or 0) * 100
        tr = c.get("total_trades") or 0
        ah = c.get("avg_hold_days") or 0
        pf = c.get("profit_factor") or 0
        params = c.get("params", {})
        config_id = params.get("config_id", str(params))
        print(f"{i+1:>3}  {cagr:>+6.1f}%  {mdd:>6.1f}%  {cal:>7.3f}  {sh:>7.3f}  "
              f"{wr:>4.0f}%  {tr:>6}  {ah:>6.0f}d  {pf:>5.2f}  {config_id}")


def print_param_sensitivity(configs):
    """Marginal analysis: avg/min/max metrics per unique param value."""
    print(f"\n{'='*110}")
    print("PARAMETER SENSITIVITY (marginal analysis)")
    print(f"{'='*110}")

    # Parse config_id to extract param values
    # Config IDs are like "1_1_1_1" (scanner_entry_exit_sim)
    # We need the actual param values from the YAML, not the IDs
    # Better approach: look at which params vary across configs

    # Collect all param values from params dict
    param_values = defaultdict(lambda: defaultdict(list))
    for c in configs:
        params = c.get("params", {})
        config_id = params.get("config_id", "")
        calmar = c.get("calmar_ratio")
        cagr = c.get("cagr")
        mdd = c.get("max_drawdown")
        trades = c.get("total_trades")
        sharpe = c.get("sharpe_ratio")
        if calmar is None:
            continue
        # Parse config_id parts
        parts = config_id.split("_")
        if len(parts) >= 4:
            param_values["scanner_id"][parts[0]].append(c)
            param_values["entry_id"][parts[1]].append(c)
            param_values["exit_id"][parts[2]].append(c)
            param_values["sim_id"][parts[3]].append(c)

    print(f"\n{'PARAM':<15}  {'VALUE':>8}  {'AVG_CAL':>8}  {'MAX_CAL':>8}  {'MIN_CAL':>8}  "
          f"{'AVG_CAGR':>9}  {'AVG_MDD':>8}  {'AVG_SHP':>8}  {'TRADES':>7}  {'N':>3}")
    print("-" * 110)

    for param_name in sorted(param_values.keys()):
        for val in sorted(param_values[param_name].keys(), key=lambda x: int(x) if x.isdigit() else x):
            entries = param_values[param_name][val]
            calmars = [e.get("calmar_ratio", 0) for e in entries if e.get("calmar_ratio") is not None]
            cagrs = [e.get("cagr", 0) for e in entries if e.get("cagr") is not None]
            mdds = [e.get("max_drawdown", 0) for e in entries if e.get("max_drawdown") is not None]
            sharpes = [e.get("sharpe_ratio", 0) for e in entries if e.get("sharpe_ratio") is not None]
            trades = [e.get("total_trades", 0) for e in entries]

            if not calmars:
                continue

            print(f"{param_name:<15}  {val:>8}  "
                  f"{sum(calmars)/len(calmars):>8.3f}  {max(calmars):>8.3f}  {min(calmars):>8.3f}  "
                  f"{sum(cagrs)/len(cagrs)*100:>8.1f}%  "
                  f"{sum(mdds)/len(mdds)*100:>7.1f}%  "
                  f"{sum(sharpes)/len(sharpes):>8.3f}  "
                  f"{sum(trades)//len(trades):>7}  "
                  f"{len(entries):>3}")


def print_summary_stats(configs):
    print(f"\n{'='*110}")
    print("SUMMARY STATS")
    print(f"{'='*110}")

    valid = [c for c in configs if c.get("cagr") is not None]
    if not valid:
        print("  No valid results.")
        return

    for name, key in [("CAGR", "cagr"), ("MaxDD", "max_drawdown"),
                       ("Calmar", "calmar_ratio"), ("Sharpe", "sharpe_ratio"),
                       ("Win Rate", "win_rate"), ("Trades", "total_trades"),
                       ("Avg Hold", "avg_hold_days")]:
        vals = [c[key] for c in valid if c.get(key) is not None]
        if not vals:
            continue
        vals.sort()
        n = len(vals)
        med = vals[n // 2]
        avg = sum(vals) / n

        if key in ("cagr", "max_drawdown", "win_rate"):
            print(f"  {name:<12}  min={vals[0]*100:>7.1f}%  avg={avg*100:>7.1f}%  "
                  f"med={med*100:>7.1f}%  max={vals[-1]*100:>7.1f}%")
        elif key in ("total_trades", "avg_hold_days"):
            print(f"  {name:<12}  min={vals[0]:>7.0f}   avg={avg:>7.0f}   "
                  f"med={med:>7.0f}   max={vals[-1]:>7.0f}")
        else:
            print(f"  {name:<12}  min={vals[0]:>7.3f}   avg={avg:>7.3f}   "
                  f"med={med:>7.3f}   max={vals[-1]:>7.3f}")


def main():
    parser = argparse.ArgumentParser(description="Analyze EOD sweep results")
    parser.add_argument("input", help="Path to sweep results JSON")
    parser.add_argument("--top", type=int, default=20, help="Top N in leaderboard")
    parser.add_argument("--marginal", action="store_true", help="Print marginal analysis")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"File not found: {args.input}")
        sys.exit(1)

    configs, raw = load_results(args.input)
    total = raw.get("total_configs", len(configs)) if raw else len(configs)
    print(f"Loaded {total} configs from {args.input}")

    print_leaderboard(configs, args.top)
    if args.marginal:
        print_param_sensitivity(configs)
    print_summary_stats(configs)


if __name__ == "__main__":
    main()

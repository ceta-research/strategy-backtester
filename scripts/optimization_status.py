#!/usr/bin/env python3
"""Optimization status tracker.

Scans results/{strategy}/ directories and prints a summary table
showing which round each strategy is at and its best metrics.

Usage:
    python scripts/optimization_status.py
    python scripts/optimization_status.py --verbose   # show per-round detail
"""

import json
import os
import re
import sys

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
SIGNALS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine", "signals")

# Round detection from filename
ROUND_PATTERN = re.compile(r"round(\d+)")


def get_all_strategies():
    """Get all registered strategies from engine/signals/*.py."""
    strategies = []
    for f in os.listdir(SIGNALS_DIR):
        if f.endswith(".py") and f not in ("__init__.py", "base.py"):
            strategies.append(f.replace(".py", ""))
    return sorted(strategies)


def parse_result_file(filepath):
    """Extract best config metrics from a result JSON file.

    Handles both formats:
    - Engine pipeline: list of dicts with metrics at top level
    - SweepResult: dict with 'all_configs' list
    """
    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    configs = []

    if isinstance(data, list):
        # Engine pipeline format
        configs = data
    elif isinstance(data, dict):
        if "all_configs" in data:
            # SweepResult format
            configs = data["all_configs"]
        elif "results" in data:
            configs = data["results"]

    if not configs:
        return None

    # Find best config by calmar_ratio
    best = None
    best_calmar = -999

    for c in configs:
        # Metrics can be at top level or in 'summary' sub-dict
        summary = c.get("summary", c)
        calmar = summary.get("calmar_ratio") or 0
        if calmar > best_calmar:
            best_calmar = calmar
            best = summary

    if best is None:
        return None

    return {
        "calmar": best.get("calmar_ratio") or 0,
        "cagr": (best.get("cagr") or 0),
        "mdd": (best.get("max_drawdown") or 0),
        "trades": best.get("total_trades") or 0,
        "configs": len(configs),
    }


def scan_strategy_results(strategy_dir):
    """Scan a strategy's results directory and return per-round metrics."""
    if not os.path.isdir(strategy_dir):
        return {}

    rounds = {}
    for f in sorted(os.listdir(strategy_dir)):
        if not f.endswith(".json"):
            continue

        filepath = os.path.join(strategy_dir, f)
        match = ROUND_PATTERN.search(f)
        if match:
            round_num = int(match.group(1))
        else:
            round_num = -1  # unknown round

        metrics = parse_result_file(filepath)
        if metrics is None:
            continue

        metrics["file"] = f
        # Keep the best result per round (in case of multiple files)
        if round_num not in rounds or metrics["calmar"] > rounds[round_num]["calmar"]:
            rounds[round_num] = metrics

    return rounds


def print_status(verbose=False):
    """Print optimization status for all strategies."""
    strategies = get_all_strategies()

    # Collect data
    rows = []
    for strategy in strategies:
        strategy_dir = os.path.join(RESULTS_DIR, strategy)
        rounds = scan_strategy_results(strategy_dir)

        if not rounds:
            rows.append((strategy, None, None))
            continue

        # Highest round number (exclude -1 = unknown)
        known_rounds = [r for r in rounds if r >= 0]
        if known_rounds:
            latest_round = max(known_rounds)
            best_round = max(rounds.keys(), key=lambda r: rounds[r]["calmar"])
            rows.append((strategy, rounds, best_round))
        else:
            # Only unknown-round files
            best_round = max(rounds.keys(), key=lambda r: rounds[r]["calmar"])
            rows.append((strategy, rounds, best_round))

    # Print table
    print()
    print(f"{'STRATEGY':<28} {'ROUND':<7} {'CALMAR':>7} {'CAGR':>8} {'MDD':>8} {'TRADES':>7} {'CONFIGS':>8}")
    print("-" * 83)

    done_count = 0
    in_progress_count = 0
    not_started_count = 0

    for strategy, rounds, best_round in rows:
        if rounds is None:
            print(f"{strategy:<28} {'--':<7} {'':>7} {'':>8} {'':>8} {'':>7} {'':>8}")
            not_started_count += 1
            continue

        m = rounds[best_round]
        round_label = f"R{best_round}" if best_round >= 0 else "?"

        # Check highest round
        known = [r for r in rounds if r >= 0]
        highest = max(known) if known else -1
        if highest >= 4:
            round_label = f"R{highest} done"
            done_count += 1
        elif highest >= 0:
            round_label = f"R{highest}"
            in_progress_count += 1
        else:
            in_progress_count += 1

        cagr_str = f"{m['cagr'] * 100:+.1f}%"
        mdd_str = f"{m['mdd'] * 100:.1f}%"
        total_configs = sum(r["configs"] for r in rounds.values())

        print(f"{strategy:<28} {round_label:<7} {m['calmar']:>7.2f} {cagr_str:>8} {mdd_str:>8} {m['trades']:>7} {total_configs:>8}")

        if verbose and len(rounds) > 1:
            for rn in sorted(rounds.keys()):
                rm = rounds[rn]
                rl = f"  R{rn}" if rn >= 0 else "  ?"
                print(f"  {'':<26} {rl:<7} {rm['calmar']:>7.2f} "
                      f"{rm['cagr'] * 100:+.1f}%  {rm['mdd'] * 100:.1f}%  "
                      f"{rm['trades']:>5}  {rm['configs']:>5}  {rm['file']}")

    print("-" * 83)
    total = len(rows)
    print(f"Total: {total} strategies | "
          f"Done: {done_count} | In progress: {in_progress_count} | "
          f"Not started: {not_started_count}")
    print()


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    print_status(verbose)

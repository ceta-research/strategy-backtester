#!/usr/bin/env python3
"""CLI entry point for strategy-backtester."""

import argparse
import json
import os
import sys

import yaml

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.pipeline import run_pipeline


def list_strategies():
    """List available strategies."""
    strategies_dir = os.path.join(os.path.dirname(__file__), "strategies")
    if not os.path.isdir(strategies_dir):
        print("No strategies directory found.")
        return

    strategies = []
    for name in sorted(os.listdir(strategies_dir)):
        config_path = os.path.join(strategies_dir, name, "config.yaml")
        if os.path.isfile(config_path):
            readme_path = os.path.join(strategies_dir, name, "README.md")
            desc = ""
            if os.path.isfile(readme_path):
                with open(readme_path) as f:
                    first_line = f.readline().strip().lstrip("# ")
                    desc = f" - {first_line}"
            strategies.append(f"  {name}{desc}")

    if strategies:
        print("Available strategies:")
        for s in strategies:
            print(s)
    else:
        print("No strategies found.")


def main():
    parser = argparse.ArgumentParser(description="Strategy Backtester")
    parser.add_argument("--strategy", type=str, help="Strategy name (from strategies/ directory)")
    parser.add_argument("--config", type=str, help="Custom config YAML path (overrides strategy config)")
    parser.add_argument("--list", action="store_true", help="List available strategies")
    parser.add_argument("--output", type=str, help="Output results to JSON file")

    args = parser.parse_args()

    if args.list:
        list_strategies()
        return

    if not args.strategy and not args.config:
        parser.print_help()
        return

    # Determine config path
    if args.config:
        config_path = args.config
    else:
        config_path = os.path.join(
            os.path.dirname(__file__), "strategies", args.strategy, "config.yaml"
        )

    if not os.path.isfile(config_path):
        print(f"Config not found: {config_path}")
        sys.exit(1)

    # Peek at config to determine pipeline type
    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    strategy_type = raw_config.get("static", {}).get("strategy_type", "eod")

    if strategy_type == "intraday":
        from engine.intraday_pipeline import run_intraday_pipeline
        results = run_intraday_pipeline(config_path)
    else:
        results = run_pipeline(config_path)

    if args.output and results:
        # Serialize results (exclude day_wise_log for JSON output)
        output_results = []
        for r in results:
            r_copy = {k: v for k, v in r.items() if k != "day_wise_log"}
            # Remove non-serializable config dicts
            for key in ["scanner_config", "entry_config", "exit_config", "simulation_config"]:
                if key in r_copy:
                    r_copy[key] = str(r_copy[key])
            output_results.append(r_copy)

        with open(args.output, "w") as f:
            json.dump(output_results, f, indent=2, default=str)
        print(f"\nResults written to: {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""CLI entry point for strategy-backtester."""

import argparse
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import hosted
from engine.config_loader import read_config
from engine.pipeline import run_pipeline


def fail(kind, reason, code):
    """End a hosted run with a non-zero exit and its reason as the last stderr line."""
    sys.stdout.flush()
    print(f"{kind}: {reason}", file=sys.stderr, flush=True)
    sys.exit(code)


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
    raw_config = read_config(config_path)

    is_hosted = hosted.is_hosted()
    if is_hosted:
        try:
            hosted.check(raw_config)
        except hosted.ConfigError as e:
            fail("ConfigError", e, 2)

    strategy_type = raw_config.get("static", {}).get("strategy_type", "eod")

    if strategy_type == "intraday":
        from engine.intraday_pipeline import run_intraday_pipeline
        sweep = run_intraday_pipeline(config_path)
    else:
        sweep = run_pipeline(config_path)

    if not sweep.configs:
        if is_hosted:
            fail("NoResultError", sweep.no_result_reason or "the run produced no result", 1)
    elif args.output:
        sweep.save(args.output, catalog=not is_hosted)
        print(f"\nResults written to: {args.output}")
    else:
        sweep.print_leaderboard()


if __name__ == "__main__":
    main()

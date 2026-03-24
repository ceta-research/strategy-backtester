#!/usr/bin/env python3
"""Run Low P/E backtest across exchanges.

Usage:
    python scripts/run_low_pe.py --variant us
    python scripts/run_low_pe.py --variant bse
    python scripts/run_low_pe.py --variant nse
    python scripts/run_low_pe.py --variant nse_native
    python scripts/run_low_pe.py --variant all          # Run US + BSE + NSE sequentially
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.pipeline import run_pipeline

STRATEGIES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "strategies", "low_pe",
)

VARIANTS = {
    "us": {
        "config": os.path.join(STRATEGIES_DIR, "config_us.yaml"),
        "provider": None,
        "label": "US (FMP stock_eod)",
    },
    "bse": {
        "config": os.path.join(STRATEGIES_DIR, "config_bse.yaml"),
        "provider": None,
        "label": "BSE (FMP stock_eod)",
    },
    "nse": {
        "config": os.path.join(STRATEGIES_DIR, "config_nse.yaml"),
        "provider": None,
        "label": "NSE (FMP stock_eod)",
    },
    "nse_native": {
        "config": os.path.join(STRATEGIES_DIR, "config_nse_native.yaml"),
        "provider": "nse_native",
        "label": "NSE (nse_charting_day)",
    },
}


def get_nse_native_provider():
    from scripts.run_nse_native import NseChartingDataProvider
    return NseChartingDataProvider()


def _get_summary(sweep, idx=0):
    """Extract summary dict from a SweepResult config entry."""
    if not sweep or not sweep.configs or idx >= len(sweep.configs):
        return {}
    params, result = sweep.configs[idx]
    return result.to_dict().get("summary", {})


def run_variant(name, variant):
    config_path = variant["config"]
    if not os.path.isfile(config_path):
        print(f"Config not found: {config_path}")
        return None

    provider = None
    if variant["provider"] == "nse_native":
        provider = get_nse_native_provider()

    print(f"\n{'='*80}")
    print(f"  LOW P/E — {variant['label']}")
    print(f"{'='*80}")

    sweep = run_pipeline(config_path, data_provider=provider)
    return sweep


def print_results(label, sweep):
    if not sweep or not sweep.configs:
        print(f"\n  {label}: No results")
        return

    # Use the built-in leaderboard
    sweep.print_leaderboard()


def main():
    parser = argparse.ArgumentParser(description="Low P/E Backtest Runner")
    parser.add_argument("--variant", type=str, default="us",
                        choices=list(VARIANTS.keys()) + ["all"],
                        help="Exchange variant to run")
    parser.add_argument("--output", type=str, help="Output results to JSON file")
    args = parser.parse_args()

    if args.variant == "all":
        variants_to_run = ["us", "bse", "nse"]
    else:
        variants_to_run = [args.variant]

    results = {}
    for name in variants_to_run:
        sweep = run_variant(name, VARIANTS[name])
        if sweep:
            results[name] = sweep
            print_results(VARIANTS[name]["label"], sweep)

    # Summary table
    if len(results) > 1:
        print(f"\n{'='*80}")
        print("  CROSS-EXCHANGE COMPARISON")
        print(f"{'='*80}")
        print(f"  {'Exchange':<30} {'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Sharpe':>8}")
        print(f"  {'-'*62}")
        for name in variants_to_run:
            if name in results:
                s = _get_summary(results[name])
                cagr = (s.get("cagr") or 0) * 100
                dd = (s.get("max_drawdown") or 0) * 100
                calmar = s.get("calmar_ratio") or 0
                sharpe = s.get("sharpe_ratio") or 0
                print(f"  {VARIANTS[name]['label']:<30} {cagr:>7.1f}% {dd:>7.1f}% "
                      f"{calmar:>8.2f} {sharpe:>8.2f}")

    if args.output and results:
        first_key = list(results.keys())[0]
        results[first_key].save(args.output)
        print(f"\nResults saved to: {args.output}")


if __name__ == "__main__":
    main()

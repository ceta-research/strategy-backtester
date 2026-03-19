#!/usr/bin/env python3
"""Run 20-year backtest and show year-wise annual returns."""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_nse_native import NseChartingDataProvider
from engine.pipeline import run_pipeline


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "strategies/eod_technical/config_20yr.yaml"
    results = run_pipeline(config_path, data_provider=NseChartingDataProvider())

    if not results:
        print("No results")
        return

    best = results[0]
    day_log = best["day_wise_log"]

    # Compute year-wise returns
    yearly = {}
    for d in day_log:
        epoch = d["log_date_epoch"]
        dt = datetime.utcfromtimestamp(epoch)
        yr = dt.year
        total_value = d["invested_value"] + d["margin_available"]
        if yr not in yearly:
            yearly[yr] = {"first": total_value, "last": total_value, "min": total_value, "max": total_value}
        yearly[yr]["last"] = total_value
        yearly[yr]["min"] = min(yearly[yr]["min"], total_value)
        yearly[yr]["max"] = max(yearly[yr]["max"], total_value)

    print(f"\n{'Year':<6} {'Start Value':>14} {'End Value':>14} {'Annual Return':>14} {'Peak':>14} {'Trough':>14} {'Intra-Yr DD':>14}")
    print("-" * 100)

    for yr in sorted(yearly.keys()):
        y = yearly[yr]
        start = y["first"]
        end = y["last"]
        ret = (end - start) / start * 100
        peak = y["max"]
        trough = y["min"]
        dd = (trough - peak) / peak * 100 if peak > 0 else 0

        print(f"{yr:<6} {start:>14,.0f} {end:>14,.0f} {ret:>13.1f}% {peak:>14,.0f} {trough:>14,.0f} {dd:>13.1f}%")

    # Summary
    first_yr = min(yearly.keys())
    last_yr = max(yearly.keys())
    total_start = yearly[first_yr]["first"]
    total_end = yearly[last_yr]["last"]
    total_ret = total_end / total_start
    print("-" * 100)
    print(f"{'TOTAL':<6} {total_start:>14,.0f} {total_end:>14,.0f} {(total_ret-1)*100:>13.1f}%")
    print(f"\n  Growth: {total_ret:.1f}x over {last_yr - first_yr} years")
    print(f"  CAGR: {best.get('cagr', 0) * 100:.1f}%")
    print(f"  Max DD: {best.get('max_drawdown', 0) * 100:.1f}%")

    wins = sum(1 for yr in yearly if (yearly[yr]["last"] - yearly[yr]["first"]) / yearly[yr]["first"] > 0)
    print(f"  Winning years: {wins}/{len(yearly)} ({wins / len(yearly) * 100:.0f}%)")


if __name__ == "__main__":
    main()

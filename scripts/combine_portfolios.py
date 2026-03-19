#!/usr/bin/env python3
"""Combine EOD Technical + Magic Formula into a 50/50 portfolio.

Uses annual returns from both strategies (2010-2024) to compute
a combined equity curve and risk metrics.
"""

import json
import os
import sys
import tempfile
import time
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from engine.pipeline import run_pipeline


def run_eod_technical():
    """Run best EOD Technical config and extract annual returns."""
    config = {
        "static": {
            "strategy_type": "eod_technical",
            "start_margin": 1000000,
            "start_epoch": 1262304000,
            "end_epoch": 1735689600,
            "prefetch_days": 400,
            "data_granularity": "day",
        },
        "scanner": {
            "instruments": [[{"exchange": "NSE", "symbols": []}]],
            "price_threshold": [50],
            "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
            "n_day_gain_threshold": [{"n": 180, "threshold": 0}],
        },
        "entry": {
            "n_day_ma": [3],
            "n_day_high": [5],
            "direction_score": [{"n_day_ma": 3, "score": 0.54}],
        },
        "exit": {
            "min_hold_time_days": [4],
            "trailing_stop_loss": [10],
        },
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_performer"],
            "order_ranking_window_days": [180],
            "max_positions": [10],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        tmp = f.name

    results = run_pipeline(tmp)
    os.unlink(tmp)

    best = results[0]
    day_log = best["day_wise_log"]

    # Extract annual returns
    year_start = {}
    year_end = {}
    for d in day_log:
        av = d["invested_value"] + d["margin_available"]
        dt = datetime.fromtimestamp(d["log_date_epoch"], tz=timezone.utc)
        y = dt.year
        if y not in year_start:
            year_start[y] = av
        year_end[y] = av

    annual = {}
    years = sorted(year_start.keys())
    for i, y in enumerate(years):
        prev = year_end[years[i - 1]] if i > 0 else year_start[y]
        annual[y] = (year_end[y] - prev) / prev if prev > 0 else 0

    return annual


def main():
    # 1. Load Magic Formula annual returns
    mf_path = "/tmp/mf_india.json"
    if not os.path.exists(mf_path):
        print("Run magic-formula backtest first: cd backtests && python3 magic-formula/backtest.py --preset india --output /tmp/mf_india.json")
        sys.exit(1)

    mf = json.load(open(mf_path))
    mf_years = {a["year"]: a["portfolio"] / 100 for a in mf["annual_returns"]}

    # 2. Run EOD Technical
    print("=" * 70)
    print("  Running EOD Technical (best config)")
    print("=" * 70)
    eod_years = run_eod_technical()

    # 3. Combine
    common = sorted(y for y in set(mf_years) & set(eod_years) if 2010 <= y <= 2024)

    eod_eq = mf_eq = comb_eq = 1.0

    print(f"\n{'Year':<6} {'EOD Tech':>10} {'MagicFmla':>10} {'Combined':>10} {'EOD Eq':>10} {'MF Eq':>10} {'Comb Eq':>10}")
    print("-" * 70)

    for y in common:
        er = eod_years[y]
        mr = mf_years[y]
        cr = 0.5 * er + 0.5 * mr
        eod_eq *= (1 + er)
        mf_eq *= (1 + mr)
        comb_eq *= (1 + cr)
        print(f"{y:<6} {er*100:>+9.1f}% {mr*100:>+9.1f}% {cr*100:>+9.1f}% {eod_eq:>10.2f} {mf_eq:>10.2f} {comb_eq:>10.2f}")

    n = len(common)
    eod_cagr = eod_eq ** (1 / n) - 1
    mf_cagr = mf_eq ** (1 / n) - 1
    comb_cagr = comb_eq ** (1 / n) - 1

    def max_dd(returns):
        eq = peak = 1.0
        dd = 0
        for r in returns:
            eq *= (1 + r)
            peak = max(peak, eq)
            dd = min(dd, (eq - peak) / peak)
        return dd

    eod_dd = max_dd([eod_years[y] for y in common])
    mf_dd = max_dd([mf_years[y] for y in common])
    comb_dd = max_dd([0.5 * eod_years[y] + 0.5 * mf_years[y] for y in common])

    eod_worst = min(eod_years[y] for y in common)
    mf_worst = min(mf_years[y] for y in common)
    comb_worst = min(0.5 * eod_years[y] + 0.5 * mf_years[y] for y in common)

    print(f"\n{'=' * 70}")
    print(f"{'Metric':<25} {'EOD Technical':>15} {'Magic Formula':>15} {'50/50 Combined':>15}")
    print(f"{'-' * 70}")
    print(f"{'CAGR':<25} {eod_cagr*100:>14.1f}% {mf_cagr*100:>14.1f}% {comb_cagr*100:>14.1f}%")
    print(f"{'Total Return':<25} {(eod_eq-1)*100:>13.0f}% {(mf_eq-1)*100:>13.0f}% {(comb_eq-1)*100:>13.0f}%")
    print(f"{'Max Drawdown (annual)':<25} {eod_dd*100:>14.1f}% {mf_dd*100:>14.1f}% {comb_dd*100:>14.1f}%")
    print(f"{'Calmar (CAGR/DD)':<25} {eod_cagr/abs(eod_dd):>15.2f} {mf_cagr/abs(mf_dd):>15.2f} {comb_cagr/abs(comb_dd):>15.2f}")
    print(f"{'Worst Year':<25} {eod_worst*100:>14.1f}% {mf_worst*100:>14.1f}% {comb_worst*100:>14.1f}%")
    print(f"{'Final Equity (1x start)':<25} {eod_eq:>14.1f}x {mf_eq:>14.1f}x {comb_eq:>14.1f}x")


if __name__ == "__main__":
    main()

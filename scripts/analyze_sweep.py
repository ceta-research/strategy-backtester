#!/usr/bin/env python3
"""Post-sweep analysis for ORB intraday results.

Reads JSON output from cloud_sweep.py and produces 4 analysis sections
matching the simulator_v2.ipynb workflow:

  Section 1: Ranked Table (all 48 configs)
  Section 2: Parameter Sensitivity (avg/min/max per param value)
  Section 3: Summary Stats (CAGR, MaxDD, Calmar distributions)
  Section 4: Best Config Detail (yearly/monthly growth, trade stats)

Usage:
    python scripts/analyze_sweep.py results/orb_sweep_2026-03-15.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone


def load_results(path):
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------------------------ #
# Section 1: Ranked Table
# ------------------------------------------------------------------ #

def print_ranked_table(results):
    print("\n" + "=" * 100)
    print("SECTION 1: RANKED TABLE (by Calmar ratio)")
    print("=" * 100)

    header = (f"{'#':>3}  {'or_win':>6}  {'entry':>5}  {'tgt%':>5}  {'stop%':>5}  "
              f"{'hold':>4}  {'CAGR':>8}  {'MaxDD':>8}  {'Calmar':>7}  "
              f"{'Sharpe':>7}  {'WinR':>6}  {'Trades':>6}")
    print(header)
    print("-" * 100)

    for i, r in enumerate(results):
        sql = r.get("sql_config", {})
        sim = r.get("sim_config", {})
        cagr = r.get("cagr")
        maxdd = r.get("max_drawdown")
        calmar = r.get("calmar_ratio")
        sharpe = r.get("sharpe_ratio")
        win_rate = r.get("win_rate")
        trades = r.get("trade_count", 0)

        print(f"{i + 1:>3}  "
              f"{sql.get('or_window', ''):>6}  "
              f"{sql.get('max_entry_bar', ''):>5}  "
              f"{_pct(sql.get('target_pct')):>5}  "
              f"{_pct(sql.get('stop_pct')):>5}  "
              f"{sql.get('max_hold_bars', ''):>4}  "
              f"{_pct(cagr):>8}  "
              f"{_pct(maxdd):>8}  "
              f"{_fnum(calmar, 3):>7}  "
              f"{_fnum(sharpe, 3):>7}  "
              f"{_fnum(win_rate, 1):>5}%  "
              f"{trades:>6}")


# ------------------------------------------------------------------ #
# Section 2: Parameter Sensitivity
# ------------------------------------------------------------------ #

def print_param_sensitivity(results):
    print("\n" + "=" * 100)
    print("SECTION 2: PARAMETER SENSITIVITY")
    print("=" * 100)

    # Collect metrics grouped by each parameter value
    param_groups = defaultdict(lambda: defaultdict(list))

    for r in results:
        cagr = r.get("cagr")
        maxdd = r.get("max_drawdown")
        calmar = r.get("calmar_ratio")
        if cagr is None:
            continue

        sql = r.get("sql_config", {})
        sim = r.get("sim_config", {})
        all_params = {**sql, **sim}

        for param_name, param_val in all_params.items():
            param_groups[param_name][param_val].append({
                "cagr": cagr, "maxdd": maxdd, "calmar": calmar,
            })

    header = f"{'PARAMETER':<20}  {'VALUE':>8}  {'AVG_CAGR':>9}  {'MIN_CAGR':>9}  {'MAX_CAGR':>9}  {'AVG_MAXDD':>9}  {'AVG_CALMAR':>10}  {'N':>3}"
    print(header)
    print("-" * 100)

    for param_name in sorted(param_groups.keys()):
        values = param_groups[param_name]
        for val in sorted(values.keys()):
            entries = values[val]
            cagrs = [e["cagr"] for e in entries]
            maxdds = [e["maxdd"] for e in entries if e["maxdd"] is not None]
            calmars = [e["calmar"] for e in entries if e["calmar"] is not None]

            avg_cagr = sum(cagrs) / len(cagrs)
            min_cagr = min(cagrs)
            max_cagr = max(cagrs)
            avg_maxdd = sum(maxdds) / len(maxdds) if maxdds else None
            avg_calmar = sum(calmars) / len(calmars) if calmars else None

            print(f"{param_name:<20}  {str(val):>8}  "
                  f"{_pct(avg_cagr):>9}  {_pct(min_cagr):>9}  {_pct(max_cagr):>9}  "
                  f"{_pct(avg_maxdd):>9}  {_fnum(avg_calmar, 3):>10}  "
                  f"{len(entries):>3}")


# ------------------------------------------------------------------ #
# Section 3: Summary Stats
# ------------------------------------------------------------------ #

def print_summary_stats(results):
    print("\n" + "=" * 100)
    print("SECTION 3: SUMMARY STATS")
    print("=" * 100)

    valid = [r for r in results if r.get("cagr") is not None]
    if not valid:
        print("  No valid results.")
        return

    for metric_name, key in [("CAGR", "cagr"), ("MaxDD", "max_drawdown"),
                              ("Calmar", "calmar_ratio"), ("Sharpe", "sharpe_ratio"),
                              ("Sortino", "sortino_ratio"), ("Win Rate", "win_rate")]:
        vals = [r[key] for r in valid if r.get(key) is not None]
        if not vals:
            continue
        vals.sort()
        n = len(vals)
        med = vals[n // 2] if n % 2 == 1 else (vals[n // 2 - 1] + vals[n // 2]) / 2

        if key in ("cagr", "max_drawdown"):
            print(f"  {metric_name:<12}  min={_pct(vals[0]):>8}  median={_pct(med):>8}  max={_pct(vals[-1]):>8}")
        elif key == "win_rate":
            print(f"  {metric_name:<12}  min={vals[0]:>7.1f}%  median={med:>7.1f}%  max={vals[-1]:>7.1f}%")
        else:
            print(f"  {metric_name:<12}  min={_fnum(vals[0], 3):>8}  median={_fnum(med, 3):>8}  max={_fnum(vals[-1], 3):>8}")

    # Trade count stats
    trades = [r["trade_count"] for r in valid]
    trades.sort()
    n = len(trades)
    med = trades[n // 2]
    print(f"  {'Trades':<12}  min={trades[0]:>8}  median={med:>8}  max={trades[-1]:>8}")


# ------------------------------------------------------------------ #
# Section 4: Best Config Detail
# ------------------------------------------------------------------ #

def print_best_config_detail(results):
    print("\n" + "=" * 100)
    print("SECTION 4: BEST CONFIG DETAIL")
    print("=" * 100)

    best = results[0] if results else None
    if not best or best.get("cagr") is None:
        print("  No valid best config.")
        return

    # Config params
    print(f"\n  Config: {best.get('config_id', 'N/A')}")
    print(f"  SQL params: {best.get('sql_config', {})}")
    print(f"  Sim params: {best.get('sim_config', {})}")
    print(f"  Trades: {best.get('trade_count', 0)}  Win rate: {_fnum(best.get('win_rate'), 1)}%")
    print(f"  CAGR: {_pct(best.get('cagr'))}  MaxDD: {_pct(best.get('max_drawdown'))}  "
          f"Calmar: {_fnum(best.get('calmar_ratio'), 3)}  Sharpe: {_fnum(best.get('sharpe_ratio'), 3)}")
    print(f"  Start value: {best.get('start_value', 'N/A'):,.0f}  "
          f"End value: {best.get('end_value', 'N/A'):,.0f}" if best.get("end_value") else "")

    # Yearly and monthly growth from day_wise_log
    day_log = best.get("day_wise_log")
    if not day_log:
        print("\n  (day_wise_log not available for yearly/monthly breakdown)")
        return

    _print_yearly_growth(day_log)
    _print_monthly_growth(day_log)


def _print_yearly_growth(day_log):
    """Aggregate day_wise_log into yearly returns."""
    print(f"\n  --- Yearly Growth ---")

    # Group by year
    yearly = defaultdict(list)
    for entry in day_log:
        epoch = entry["log_date_epoch"]
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        yearly[dt.year].append(entry["margin_available"])

    years = sorted(yearly.keys())
    if not years:
        return

    print(f"  {'Year':>6}  {'Start':>12}  {'End':>12}  {'Return':>8}")
    print(f"  {'-' * 46}")

    prev_end = day_log[0]["margin_available"]  # Use first entry as start
    for i, year in enumerate(years):
        vals = yearly[year]
        start_val = prev_end if i > 0 else vals[0]
        end_val = vals[-1]
        ret = (end_val - start_val) / start_val if start_val > 0 else 0
        print(f"  {year:>6}  {start_val:>12,.0f}  {end_val:>12,.0f}  {ret * 100:>7.1f}%")
        prev_end = end_val


def _print_monthly_growth(day_log):
    """Aggregate day_wise_log into monthly returns (last 12 months)."""
    print(f"\n  --- Monthly Growth (last 12 months) ---")

    monthly = defaultdict(list)
    for entry in day_log:
        epoch = entry["log_date_epoch"]
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        key = (dt.year, dt.month)
        monthly[key].append(entry["margin_available"])

    months = sorted(monthly.keys())
    if not months:
        return

    # Show last 12 months
    show_months = months[-12:]

    print(f"  {'Month':>8}  {'Start':>12}  {'End':>12}  {'Return':>8}")
    print(f"  {'-' * 48}")

    prev_end = None
    for ym in show_months:
        vals = monthly[ym]
        start_val = prev_end if prev_end is not None else vals[0]
        end_val = vals[-1]
        ret = (end_val - start_val) / start_val if start_val > 0 else 0
        print(f"  {ym[0]}-{ym[1]:02d}  {start_val:>12,.0f}  {end_val:>12,.0f}  {ret * 100:>7.1f}%")
        prev_end = end_val


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _pct(v):
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def _fnum(v, decimals=2):
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}"


def main():
    parser = argparse.ArgumentParser(description="Analyze ORB sweep results")
    parser.add_argument("input", help="Path to sweep results JSON")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"File not found: {args.input}")
        sys.exit(1)

    results = load_results(args.input)
    print(f"Loaded {len(results)} configs from {args.input}")

    print_ranked_table(results)
    print_param_sensitivity(results)
    print_summary_stats(results)
    print_best_config_detail(results)


if __name__ == "__main__":
    main()

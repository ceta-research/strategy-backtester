#!/usr/bin/env python3
"""Run 20-year backtest and show year-wise returns alongside NIFTY50 and NIFTY MIDCAP benchmarks."""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_nse_native import NseChartingDataProvider
from engine.pipeline import run_pipeline
from lib.cr_client import CetaResearch


def fetch_nse_yearly(cr, symbol, start_epoch, end_epoch):
    """Fetch from nse.nse_charting_day. Returns (returns_dict, dd_dict)."""
    sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
              WHERE symbol = '{symbol}' AND date_epoch >= {start_epoch} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return {}, {}
    return _compute_yearly(results, "date_epoch")


def fetch_fmp_yearly(cr, symbol, start_epoch, end_epoch):
    """Fetch from fmp.stock_eod (uses dateEpoch, adjClose). Returns (returns_dict, dd_dict)."""
    sql = f"""SELECT dateEpoch, adjClose AS close FROM fmp.stock_eod
              WHERE symbol = '{symbol}' AND dateEpoch >= {start_epoch} AND dateEpoch <= {end_epoch}
              ORDER BY dateEpoch"""
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return {}, {}
    return _compute_yearly(results, "dateEpoch")


def _compute_yearly(results, epoch_col):
    yearly = {}
    for row in results:
        epoch = int(row[epoch_col])
        close = float(row["close"])
        yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": close, "last": close, "peak": close, "trough": close}
        yearly[yr]["last"] = close
        yearly[yr]["peak"] = max(yearly[yr]["peak"], close)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], close)
    returns = {yr: (v["last"] - v["first"]) / v["first"] * 100 for yr, v in yearly.items()}
    dd = {yr: (v["trough"] - v["peak"]) / v["peak"] * 100 if v["peak"] > 0 else 0 for yr, v in yearly.items()}
    return returns, dd


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "strategies/eod_technical/config_20yr.yaml"

    # Run the strategy backtest
    results = run_pipeline(config_path, data_provider=NseChartingDataProvider())
    if not results:
        print("No results")
        return

    best = results[0]
    day_log = best["day_wise_log"]

    # Extract strategy year-wise returns + drawdowns
    yearly = {}
    for d in day_log:
        epoch = d["log_date_epoch"]
        yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
        total_value = d["invested_value"] + d["margin_available"]
        if yr not in yearly:
            yearly[yr] = {"first": total_value, "last": total_value, "peak": total_value, "trough": total_value}
        yearly[yr]["last"] = total_value
        yearly[yr]["peak"] = max(yearly[yr]["peak"], total_value)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], total_value)

    strategy_returns = {}
    strategy_dd = {}
    for yr in sorted(yearly.keys()):
        y = yearly[yr]
        strategy_returns[yr] = (y["last"] - y["first"]) / y["first"] * 100
        strategy_dd[yr] = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0

    cr = CetaResearch()
    start_epoch = 1104537600  # 2005-01-01
    end_epoch = 1773878400    # 2026-03-19

    # NIFTY 50: Use NIFTYBEES from NSE charting (tracks NIFTY 50, data from 2002)
    print("\n--- Fetching NIFTY 50 proxy (NIFTYBEES) ---")
    nifty50_ret, nifty50_dd = fetch_nse_yearly(cr, "NIFTYBEES", start_epoch, end_epoch)
    nifty_label = "NIFTYBEES"

    # Also get ^NSEI from FMP for cross-check (from 2007)
    print("\n--- Fetching ^NSEI from FMP ---")
    nsei_ret, nsei_dd = fetch_fmp_yearly(cr, "^NSEI", start_epoch, end_epoch)

    # Use ^NSEI where available (more accurate for index), NIFTYBEES for earlier years
    nifty_combined_ret = {}
    nifty_combined_dd = {}
    for yr in sorted(set(list(nifty50_ret.keys()) + list(nsei_ret.keys()))):
        if yr in nsei_ret:
            nifty_combined_ret[yr] = nsei_ret[yr]
            nifty_combined_dd[yr] = nsei_dd[yr]
        elif yr in nifty50_ret:
            nifty_combined_ret[yr] = nifty50_ret[yr]
            nifty_combined_dd[yr] = nifty50_dd[yr]
    nifty50_ret = nifty_combined_ret
    nifty50_dd = nifty_combined_dd
    nifty_label = "NIFTY 50"

    # NIFTY MIDCAP: Use MIDCAPIETF from NSE charting (from 2020)
    print("\n--- Fetching NIFTY MIDCAP proxy (MIDCAPIETF) ---")
    midcap_ret, midcap_dd = fetch_nse_yearly(cr, "MIDCAPIETF", start_epoch, end_epoch)

    # Print comparison table
    all_years = sorted(set(list(strategy_returns.keys()) + list(nifty50_ret.keys()) + list(midcap_ret.keys())))

    hdr = (f"{'Year':<6} "
           f"{'Strategy':>10} {'DD':>8} "
           f"{'NIFTY 50':>10} {'DD':>8} "
           f"{'MIDCAP':>10} {'DD':>8} "
           f"{'vs N50':>10}")
    sep = "=" * len(hdr)
    print(f"\n{sep}")
    print(hdr)
    print(sep)

    strat_cum = 1.0
    nifty_cum = 1.0
    midcap_cum = 1.0

    for yr in all_years:
        sr = strategy_returns.get(yr)
        sdd = strategy_dd.get(yr)
        nr = nifty50_ret.get(yr)
        ndd = nifty50_dd.get(yr)
        mr = midcap_ret.get(yr)
        mdd = midcap_dd.get(yr)

        sr_s = f"{sr:>+9.1f}%" if sr is not None else f"{'—':>10}"
        sdd_s = f"{sdd:>7.1f}%" if sdd is not None else f"{'—':>8}"
        nr_s = f"{nr:>+9.1f}%" if nr is not None else f"{'—':>10}"
        ndd_s = f"{ndd:>7.1f}%" if ndd is not None else f"{'—':>8}"
        mr_s = f"{mr:>+9.1f}%" if mr is not None else f"{'—':>10}"
        mdd_s = f"{mdd:>7.1f}%" if mdd is not None else f"{'—':>8}"

        vs = f"{sr - nr:>+9.1f}%" if (sr is not None and nr is not None) else f"{'':>10}"

        if sr is not None:
            strat_cum *= (1 + sr / 100)
        if nr is not None:
            nifty_cum *= (1 + nr / 100)
        if mr is not None:
            midcap_cum *= (1 + mr / 100)

        print(f"{yr:<6} {sr_s} {sdd_s} {nr_s} {ndd_s} {mr_s} {mdd_s} {vs}")

    print(sep)
    print(f"{'MULT':>6} {strat_cum:>9.1f}x {'':>8} {nifty_cum:>9.1f}x {'':>8} {midcap_cum:>9.1f}x")

    n_years = all_years[-1] - all_years[0]
    strat_cagr = (strat_cum ** (1 / n_years) - 1) * 100 if strat_cum > 0 else 0
    nifty_cagr = (nifty_cum ** (1 / n_years) - 1) * 100 if nifty_cum > 0 else 0
    midcap_cagr = (midcap_cum ** (1 / n_years) - 1) * 100 if midcap_cum > 0 else 0
    print(f"{'CAGR':>6} {strat_cagr:>+9.1f}% {'':>8} {nifty_cagr:>+9.1f}% {'':>8} {midcap_cagr:>+9.1f}%   (over {n_years} years)")

    wins_vs_nifty = sum(1 for yr in all_years if strategy_returns.get(yr) is not None and nifty50_ret.get(yr) is not None and strategy_returns[yr] > nifty50_ret[yr])
    total_vs_nifty = sum(1 for yr in all_years if strategy_returns.get(yr) is not None and nifty50_ret.get(yr) is not None)

    print(f"\n  Strategy beats NIFTY 50: {wins_vs_nifty}/{total_vs_nifty} years ({wins_vs_nifty/total_vs_nifty*100:.0f}%)" if total_vs_nifty > 0 else "")


if __name__ == "__main__":
    main()

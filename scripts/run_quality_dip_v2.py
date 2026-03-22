#!/usr/bin/env python3
"""Quality Dip-Buy runner with always-invested post-processing.

Runs the pipeline, then computes adjusted equity curves where idle cash
earns NIFTYBEES returns instead of 0%. This simulates parking cash in
the index when no dip positions are active.

Usage:
    python scripts/run_quality_dip_v2.py [config_path]
    python scripts/run_quality_dip_v2.py strategies/quality_dip_buy/config_nse_v3.yaml
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.pipeline import run_pipeline
from engine.data_provider import CRDataProvider
from lib.metrics import compute_metrics


def fetch_niftybees_prices(start_epoch, end_epoch):
    """Fetch NIFTYBEES daily closes for the simulation period."""
    provider = CRDataProvider(format="parquet")
    df = provider.fetch_ohlcv(
        exchanges=["NSE"],
        symbols=["NIFTYBEES"],
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        prefetch_days=10,
    )
    if df.is_empty():
        print("WARNING: Could not fetch NIFTYBEES data for always-invested adjustment")
        return {}

    # Build epoch -> close map
    prices = {}
    for row in df.select(["date_epoch", "close"]).to_dicts():
        prices[row["date_epoch"]] = row["close"]
    return prices


def compute_always_invested_metrics(result, niftybees_prices):
    """Adjust equity curve: idle cash earns NIFTYBEES returns.

    For each day:
        cash_flow = margin[t] - margin[t-1]  (net from trade entries/exits)
        adjusted_margin[t] = adjusted_margin[t-1] * (1 + niftybees_ret) + cash_flow

    This accurately compounds NIFTYBEES returns on idle cash while
    preserving the actual trade entries/exits from the backtest.
    """
    day_wise_log = result.get("day_wise_log")
    if not day_wise_log or len(day_wise_log) < 2:
        return None

    # Build daily NIFTYBEES returns
    sorted_epochs = sorted(niftybees_prices.keys())
    niftybees_returns = {}
    for i in range(1, len(sorted_epochs)):
        prev_close = niftybees_prices[sorted_epochs[i - 1]]
        curr_close = niftybees_prices[sorted_epochs[i]]
        if prev_close and prev_close > 0:
            niftybees_returns[sorted_epochs[i]] = curr_close / prev_close - 1.0

    # Compute adjusted equity curve
    adjusted_margin = day_wise_log[0]["margin_available"]
    adjusted_values = []

    for i, day in enumerate(day_wise_log):
        epoch = day["log_date_epoch"]
        invested = day["invested_value"]
        margin = day["margin_available"]

        if i == 0:
            adjusted_values.append(invested + adjusted_margin)
            continue

        # Cash flow: difference in original margin (captures trade entries/exits)
        prev_margin = day_wise_log[i - 1]["margin_available"]
        cash_flow = margin - prev_margin

        # Apply NIFTYBEES return to the adjusted idle cash, then add cash flow
        nifty_ret = niftybees_returns.get(epoch, 0.0)
        adjusted_margin = adjusted_margin * (1 + nifty_ret) + cash_flow

        adjusted_values.append(invested + adjusted_margin)

    # Compute metrics on adjusted curve
    daily_returns = []
    for i in range(1, len(adjusted_values)):
        if adjusted_values[i - 1] > 0:
            daily_returns.append((adjusted_values[i] - adjusted_values[i - 1]) / adjusted_values[i - 1])
        else:
            daily_returns.append(0.0)

    if not daily_returns:
        return None

    benchmark_returns = [0.0] * len(daily_returns)
    metrics = compute_metrics(daily_returns, benchmark_returns, periods_per_year=252)
    port = metrics["portfolio"]

    return {
        "cagr_adj": port.get("cagr"),
        "max_drawdown_adj": port.get("max_drawdown"),
        "calmar_ratio_adj": port.get("calmar_ratio"),
        "sharpe_ratio_adj": port.get("sharpe_ratio"),
        "start_value_adj": adjusted_values[0],
        "end_value_adj": adjusted_values[-1],
    }


def describe_config(result):
    """Extract human-readable config params from result."""
    entry = result.get("entry_config", {})
    exit_cfg = result.get("exit_config", {})
    sim = result.get("simulation_config", {})
    return {
        "yrs": entry.get("consecutive_positive_years", "?"),
        "dip": entry.get("dip_threshold_pct", "?"),
        "peak": entry.get("peak_lookback_days", "?"),
        "sector": entry.get("max_per_sector", 0),
        "tsl": exit_cfg.get("tsl_pct", "?"),
        "hold": exit_cfg.get("max_hold_days", "?"),
        "pos": sim.get("max_positions", "?"),
    }


def main():
    # Accept config path as CLI argument
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        if not os.path.isabs(config_path):
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                config_path
            )
    else:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "strategies", "quality_dip_buy", "config_nse_v2.yaml"
        )

    config_name = os.path.basename(config_path).replace(".yaml", "")
    print("=" * 100)
    print(f"QUALITY DIP-BUY: {config_name} + Always-invested post-processing")
    print("=" * 100)

    # Run pipeline
    results = run_pipeline(config_path)

    if not results:
        print("No results. Aborting.")
        return

    # Fetch NIFTYBEES for always-invested adjustment
    print("\n--- Fetching NIFTYBEES for always-invested adjustment ---")
    niftybees_prices = fetch_niftybees_prices(
        start_epoch=1262304000,  # 2010-01-01
        end_epoch=1741219200,    # 2025-03-06
    )

    # Compute adjusted metrics for each result
    for r in results:
        adj = compute_always_invested_metrics(r, niftybees_prices)
        if adj:
            r.update(adj)

    # Sort by adjusted Calmar
    results.sort(key=lambda r: r.get("calmar_ratio_adj") or r.get("calmar_ratio") or 0, reverse=True)

    # Print top results
    print("\n" + "=" * 140)
    print("TOP 20 CONFIGS (sorted by adjusted Calmar ratio)")
    print("=" * 140)
    header = (
        f"{'#':>3} {'Yrs':>3} {'Dip%':>5} {'Peak':>5} {'Sec':>4} {'TSL%':>5} {'Pos':>4} "
        f"{'CAGR':>7} {'MaxDD':>7} {'Calmar':>7} {'Sharpe':>7} "
        f"{'|':>2} {'CAGR*':>7} {'MaxDD*':>7} {'Calmar*':>8} {'Sharpe*':>8}"
    )
    print(header)
    print(f"{'':>3} {'':>3} {'':>5} {'':>5} {'':>4} {'':>5} {'':>4} "
          f"{'---Original---':>30} {'|':>2} {'---Always Invested---':>32}")
    print("-" * 140)

    for i, r in enumerate(results[:20]):
        cfg = describe_config(r)
        cagr = (r.get("cagr") or 0) * 100
        mdd = (r.get("max_drawdown") or 0) * 100
        calmar = r.get("calmar_ratio") or 0
        sharpe = r.get("sharpe_ratio") or 0
        cagr_a = (r.get("cagr_adj") or 0) * 100
        mdd_a = (r.get("max_drawdown_adj") or 0) * 100
        calmar_a = r.get("calmar_ratio_adj") or 0
        sharpe_a = r.get("sharpe_ratio_adj") or 0

        print(
            f"{i+1:>3} {cfg['yrs']:>3} {cfg['dip']:>5} {cfg['peak']:>5} {cfg['sector']:>4} "
            f"{cfg['tsl']:>5} {cfg['pos']:>4} "
            f"{cagr:>6.1f}% {mdd:>6.1f}% {calmar:>7.3f} {sharpe:>7.3f} "
            f"{'|':>2} {cagr_a:>6.1f}% {mdd_a:>6.1f}% {calmar_a:>8.3f} {sharpe_a:>8.3f}"
        )

    # Summary
    print("\n--- SUMMARY ---")
    best_orig = max(results, key=lambda r: (r.get("cagr") or 0))
    best_adj = max(results, key=lambda r: (r.get("cagr_adj") or 0))
    best_calmar = max(results, key=lambda r: (r.get("calmar_ratio_adj") or r.get("calmar_ratio") or 0))

    print(f"Best original CAGR:    {best_orig.get('cagr', 0)*100:.1f}% "
          f"(config: {best_orig['config_id']})")
    print(f"Best adjusted CAGR:    {best_adj.get('cagr_adj', 0)*100:.1f}% "
          f"(config: {best_adj['config_id']})")
    print(f"Best adjusted Calmar:  {best_calmar.get('calmar_ratio_adj', 0):.3f} "
          f"(config: {best_calmar['config_id']})")
    print(f"\nBenchmark: NIFTYBEES B&H = 12.5% CAGR, -60% MDD, Calmar 0.21")

    # Breakdown by feature
    print("\n--- FEATURE IMPACT ---")
    for label, filter_fn in [
        ("2yr quality filter", lambda r: describe_config(r)["yrs"] == 2),
        ("3yr quality filter", lambda r: describe_config(r)["yrs"] == 3),
        ("No sector limit", lambda r: describe_config(r)["sector"] == 0),
        ("Max 3/sector", lambda r: describe_config(r)["sector"] == 3),
    ]:
        subset = [r for r in results if filter_fn(r)]
        if subset:
            avg_cagr = sum((r.get("cagr") or 0) for r in subset) / len(subset) * 100
            avg_adj = sum((r.get("cagr_adj") or 0) for r in subset) / len(subset) * 100
            best = max(subset, key=lambda r: (r.get("cagr_adj") or 0))
            print(f"  {label:20s}: avg CAGR {avg_cagr:.1f}%, avg adj CAGR {avg_adj:.1f}%, "
                  f"best adj {best.get('cagr_adj', 0)*100:.1f}%")

    # Save results (without day_wise_log)
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "results", f"quality_dip_buy_{config_name}.json"
    )
    output = []
    for r in results:
        r_copy = {k: v for k, v in r.items() if k != "day_wise_log"}
        for key in ["scanner_config", "entry_config", "exit_config", "simulation_config"]:
            if key in r_copy:
                r_copy[key] = str(r_copy[key])
        output.append(r_copy)

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()

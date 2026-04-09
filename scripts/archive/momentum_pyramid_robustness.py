#!/usr/bin/env python3
"""Robustness testing for best momentum pyramid configs.

Part 1: Sub-period analysis (2010-2015, 2015-2020, 2020-2026)
Part 2: Walk-forward (IS: 2010-2017, OOS: 2018-2026)
Part 3: Parameter sensitivity (vary each param ±1 step)
Part 4: Trade-level analysis (distribution, outliers, concentration)

Always runs on bhavcopy with 5 bps slippage.
"""

import sys
import os
import time
import math
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_regime_epochs,
    CetaResearch,
    SLIPPAGE,
)
from scripts.momentum_breakout_v3 import compute_cascade_entries
from scripts.momentum_pyramid import simulate_pyramid_portfolio

STRATEGY_NAME = "robustness"

# Epoch constants
E_2010 = 1262304000
E_2015 = 1420070400
E_2016 = 1451606400
E_2018 = 1514764800
E_2020 = 1577836800
E_2026 = 1773878400

# Named configs to test
CONFIGS = {
    "A_best_cagr": {
        "cascade": (42, 126, 0.02, 0.15),
        "max_per_instrument": 3, "momentum_weight_factor": 2.0,
        "accel_norm": 0.10, "max_positions": 10, "tsl_pct": 10,
        "pyramid_decay": 0.75, "pyramid_min_gap": 21,
        "max_hold_days": 504, "regime_sma": 200,
    },
    "B_balanced": {
        "cascade": (42, 126, 0.02, 0.15),
        "max_per_instrument": 3, "momentum_weight_factor": 2.0,
        "accel_norm": 0.12, "max_positions": 10, "tsl_pct": 10,
        "pyramid_decay": 0.75, "pyramid_min_gap": 21,
        "max_hold_days": 504, "regime_sma": 200,
    },
    "C_safe": {
        "cascade": (42, 126, 0.02, 0.20),
        "max_per_instrument": 3, "momentum_weight_factor": 2.0,
        "accel_norm": 0.10, "max_positions": 10, "tsl_pct": 13,
        "pyramid_decay": 0.5, "pyramid_min_gap": 21,
        "max_hold_days": 504, "regime_sma": 50,
    },
    "D_calmar": {
        "cascade": (42, 126, 0.03, 0.20),
        "max_per_instrument": 1, "momentum_weight_factor": 1.5,
        "accel_norm": 0.10, "max_positions": 14, "tsl_pct": 13,
        "pyramid_decay": 1.0, "pyramid_min_gap": 21,
        "max_hold_days": 504, "regime_sma": 50,
    },
    "E_no_pyramid": {
        "cascade": (42, 126, 0.02, 0.15),
        "max_per_instrument": 1, "momentum_weight_factor": 2.5,
        "accel_norm": 0.10, "max_positions": 8, "tsl_pct": 11,
        "pyramid_decay": 1.0, "pyramid_min_gap": 21,
        "max_hold_days": 504, "regime_sma": 200,
    },
}


def filter_data_for_period(price_data, benchmark, end_epoch):
    """Filter price and benchmark data to end at end_epoch."""
    filtered_pd = {}
    for sym, bars in price_data.items():
        fbars = [b for b in bars if b["epoch"] <= end_epoch]
        if len(fbars) > 130:
            filtered_pd[sym] = fbars
    filtered_bm = {ep: v for ep, v in benchmark.items() if ep <= end_epoch}
    return filtered_pd, filtered_bm


def run_config(config, entries, price_data, benchmark, regime_epochs, start_epoch):
    """Run a single config and return result."""
    r, dwl = simulate_pyramid_portfolio(
        entries, price_data, benchmark,
        capital=10_000_000,
        max_positions=config["max_positions"],
        max_per_instrument=config["max_per_instrument"],
        tsl_pct=config["tsl_pct"],
        max_hold_days=config["max_hold_days"],
        exchange="NSE",
        pyramid_decay=config["pyramid_decay"],
        pyramid_min_gap=config["pyramid_min_gap"],
        momentum_weight_factor=config["momentum_weight_factor"],
        accel_norm=config["accel_norm"],
        regime_epochs=regime_epochs,
        start_epoch=start_epoch,
        strategy_name=STRATEGY_NAME,
        params=config,
    )
    return r


def extract_metrics(r):
    """Extract key metrics from BacktestResult."""
    s = r.to_dict().get("summary", {})
    return {
        "cagr": (s.get("cagr") or 0) * 100,
        "mdd": (s.get("max_drawdown") or 0) * 100,
        "calmar": s.get("calmar_ratio") or 0,
        "sharpe": s.get("sharpe_ratio") or 0,
        "trades": s.get("total_trades") or 0,
        "win_rate": (s.get("win_rate") or 0) * 100,
        "payoff": s.get("payoff_ratio") or 0,
    }


def print_metrics(label, m):
    """Print metrics in a compact row."""
    print(f"  {label:20s} | CAGR={m['cagr']:+.1f}% MDD={m['mdd']:.1f}% "
          f"Cal={m['calmar']:.2f} Sharpe={m['sharpe']:.2f} "
          f"WR={m['win_rate']:.0f}% T={m['trades']}")


def analyze_trades(r, config_name):
    """Deep trade-level analysis."""
    data = r.to_dict()
    trades = data.get("trades", [])
    if not trades:
        print(f"  No trades for {config_name}")
        return

    print(f"\n  {'='*70}")
    print(f"  TRADE ANALYSIS: {config_name} ({len(trades)} trades)")
    print(f"  {'='*70}")

    # Compute per-trade returns
    returns = []
    symbols = []
    hold_days = []
    exit_reasons = Counter()

    for t in trades:
        entry_p = t.get("entry_price", 0)
        exit_p = t.get("exit_price", 0)
        if entry_p <= 0:
            continue
        ret = (exit_p - entry_p) / entry_p
        # Subtract charges + slippage as % of trade value
        charges = t.get("charges", 0)
        slippage = t.get("slippage", 0)
        trade_val = t.get("quantity", 0) * entry_p
        if trade_val > 0:
            cost_pct = (charges + slippage) / trade_val
            ret -= cost_pct
        returns.append(ret * 100)
        symbols.append(t.get("symbol", "?"))
        hd = (t.get("exit_epoch", 0) - t.get("entry_epoch", 0)) / 86400
        hold_days.append(hd)
        exit_reasons[t.get("exit_reason", "unknown")] += 1

    if not returns:
        return

    returns.sort()
    n = len(returns)

    # Distribution
    print(f"\n  Return Distribution:")
    print(f"    Min:    {returns[0]:+.1f}%")
    print(f"    P5:     {returns[int(n*0.05)]:+.1f}%")
    print(f"    P25:    {returns[int(n*0.25)]:+.1f}%")
    print(f"    Median: {returns[int(n*0.50)]:+.1f}%")
    print(f"    P75:    {returns[int(n*0.75)]:+.1f}%")
    print(f"    P95:    {returns[int(n*0.95)]:+.1f}%")
    print(f"    Max:    {returns[-1]:+.1f}%")
    print(f"    Mean:   {sum(returns)/n:+.1f}%")
    print(f"    StdDev: {(sum((r - sum(returns)/n)**2 for r in returns) / n)**0.5:.1f}%")

    # Bucket distribution
    buckets = {"<-30%": 0, "-30 to -15%": 0, "-15 to 0%": 0,
               "0 to 15%": 0, "15 to 50%": 0, "50 to 100%": 0, ">100%": 0}
    for r in returns:
        if r < -30: buckets["<-30%"] += 1
        elif r < -15: buckets["-30 to -15%"] += 1
        elif r < 0: buckets["-15 to 0%"] += 1
        elif r < 15: buckets["0 to 15%"] += 1
        elif r < 50: buckets["15 to 50%"] += 1
        elif r < 100: buckets["50 to 100%"] += 1
        else: buckets[">100%"] += 1

    print(f"\n  Return Buckets:")
    for bucket, count in buckets.items():
        bar = "#" * int(count / n * 50)
        print(f"    {bucket:15s}: {count:4d} ({count/n*100:5.1f}%) {bar}")

    # Outlier dependency: what if we remove top 5 winners?
    sorted_rets = sorted(returns, reverse=True)
    total_return = sum(returns)
    top5_return = sum(sorted_rets[:5])
    top10_return = sum(sorted_rets[:10])
    print(f"\n  Outlier Dependency:")
    print(f"    Total return (sum): {total_return:+.0f}%")
    print(f"    Top 5 trades:       {top5_return:+.0f}% ({top5_return/total_return*100:.0f}% of total)")
    print(f"    Top 10 trades:      {top10_return:+.0f}% ({top10_return/total_return*100:.0f}% of total)")
    print(f"    Without top 5:      {total_return-top5_return:+.0f}%")

    # Top 10 winners and losers
    trade_rets = list(zip(returns, symbols, hold_days))
    trade_rets_sorted = sorted(zip(returns, symbols, hold_days), key=lambda x: -x[0])

    print(f"\n  Top 10 Winners:")
    for ret, sym, hd in trade_rets_sorted[:10]:
        print(f"    {sym:20s}: {ret:+.1f}% ({hd:.0f}d)")

    print(f"\n  Top 10 Losers:")
    for ret, sym, hd in trade_rets_sorted[-10:]:
        print(f"    {sym:20s}: {ret:+.1f}% ({hd:.0f}d)")

    # Symbol concentration
    sym_counts = Counter(symbols)
    print(f"\n  Symbol Concentration (top 10):")
    print(f"    Unique symbols: {len(sym_counts)}")
    for sym, cnt in sym_counts.most_common(10):
        sym_rets = [r for r, s, _ in zip(returns, symbols, hold_days) if s == sym]
        avg_ret = sum(sym_rets) / len(sym_rets) if sym_rets else 0
        print(f"    {sym:20s}: {cnt:3d} trades, avg return {avg_ret:+.1f}%")

    # Exit reasons
    print(f"\n  Exit Reasons:")
    for reason, count in exit_reasons.most_common():
        print(f"    {reason:20s}: {count:4d} ({count/n*100:.0f}%)")

    # Holding period stats
    print(f"\n  Holding Period:")
    print(f"    Mean: {sum(hold_days)/len(hold_days):.0f} days")
    print(f"    Median: {sorted(hold_days)[len(hold_days)//2]:.0f} days")
    winners_hd = [hd for r, _, hd in zip(returns, symbols, hold_days) if r > 0]
    losers_hd = [hd for r, _, hd in zip(returns, symbols, hold_days) if r <= 0]
    if winners_hd:
        print(f"    Winners avg: {sum(winners_hd)/len(winners_hd):.0f} days")
    if losers_hd:
        print(f"    Losers avg: {sum(losers_hd)/len(losers_hd):.0f} days")

    # Pyramid analysis: detect same-symbol entries close together
    sym_entries = defaultdict(list)
    for t in trades:
        sym_entries[t.get("symbol", "?")].append(t.get("entry_epoch", 0))

    pyramid_count = 0
    for sym, epochs in sym_entries.items():
        epochs_sorted = sorted(epochs)
        for i in range(1, len(epochs_sorted)):
            if (epochs_sorted[i] - epochs_sorted[i-1]) / 86400 < 100:
                pyramid_count += 1

    print(f"\n  Pyramid Entries (same symbol within 100d): {pyramid_count} "
          f"({pyramid_count/n*100:.0f}% of trades)")


def main():
    exchange = "NSE"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print("  ROBUSTNESS TESTING: Momentum Pyramid Top Configs")
    print("=" * 80)

    # Fetch all data (full period)
    print("\nFetching universe...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, E_2010, E_2026,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print("\nFetching benchmark...")
    benchmark = fetch_benchmark(cr, "NIFTYBEES", exchange, E_2010, E_2026,
                                warmup_days=250, source=source)

    print("\nComputing regime filters...")
    regime_200 = compute_regime_epochs(benchmark, 200)
    regime_50 = compute_regime_epochs(benchmark, 50)
    regime_map = {200: regime_200, 50: regime_50}

    # Compute cascade entries for all needed param combos
    print("\nComputing cascade entries...")
    cascade_keys = set(c["cascade"] for c in CONFIGS.values())
    cascade_cache = {}
    for ck in cascade_keys:
        cascade_cache[ck] = compute_cascade_entries(
            price_data, ck[0], ck[1], ck[2], ck[3], start_epoch=E_2010)

    # Pre-compute filtered data for sub-periods
    periods = {
        "Full (2010-2026)": (E_2010, E_2026),
        "2010-2015": (E_2010, E_2016),
        "2015-2020": (E_2015, E_2020),
        "2020-2026": (E_2020, E_2026),
        "IS (2010-2017)": (E_2010, E_2018),
        "OOS (2018-2026)": (E_2018, E_2026),
    }

    # ══════════════════════════════════════════════════════════════════════
    # PART 1 + 2: Sub-period + Walk-forward
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PART 1+2: SUB-PERIOD + WALK-FORWARD ANALYSIS")
    print(f"{'='*80}")

    for config_name, config in CONFIGS.items():
        print(f"\n  ── {config_name} ──")
        ck = config["cascade"]
        entries_full = cascade_cache[ck]
        regime = regime_map[config["regime_sma"]]

        for period_name, (start_ep, end_ep) in periods.items():
            pd_filtered, bm_filtered = filter_data_for_period(
                price_data, benchmark, end_ep)

            # Filter entries for this period
            entries_filtered = [e for e in entries_full
                                if start_ep <= e["entry_epoch"] <= end_ep]

            # Filter regime
            regime_filtered = {ep for ep in regime if ep <= end_ep}

            if len(entries_filtered) < 5:
                print(f"  {period_name:20s} | Too few entries ({len(entries_filtered)})")
                continue

            r = run_config(config, entries_filtered, pd_filtered,
                           bm_filtered, regime_filtered, start_ep)
            m = extract_metrics(r)
            print_metrics(period_name, m)

    # ══════════════════════════════════════════════════════════════════════
    # PART 3: Parameter Sensitivity (Config A)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PART 3: PARAMETER SENSITIVITY (Config A_best_cagr)")
    print("  Vary each parameter ±1 step, hold others at best")
    print(f"{'='*80}")

    base = CONFIGS["A_best_cagr"].copy()
    base_ck = base["cascade"]
    regime = regime_map[base["regime_sma"]]

    # Run baseline first
    entries = cascade_cache[base_ck]
    r_base = run_config(base, entries, price_data, benchmark, regime, E_2010)
    m_base = extract_metrics(r_base)
    print(f"\n  BASELINE:")
    print_metrics("A_best_cagr", m_base)

    # Parameters to vary (name, values, which part changes)
    sensitivity_tests = [
        # Signal params (need new cascade entries)
        ("accel_threshold", [0.01, 0.02, 0.03, 0.05], "signal"),
        ("min_momentum", [0.10, 0.15, 0.20, 0.25], "signal"),
        # Simulator params
        ("momentum_weight_factor", [1.0, 1.5, 2.0, 2.5, 3.0], "sim"),
        ("accel_norm", [0.05, 0.08, 0.10, 0.12, 0.15], "sim"),
        ("max_positions", [6, 8, 10, 12, 14], "sim"),
        ("tsl_pct", [8, 9, 10, 11, 12, 15], "sim"),
        ("pyramid_decay", [0.25, 0.50, 0.75, 1.0], "sim"),
        ("max_per_instrument", [1, 2, 3, 4], "sim"),
        ("pyramid_min_gap", [7, 14, 21, 42], "sim"),
    ]

    for param_name, values, param_type in sensitivity_tests:
        print(f"\n  ── Varying: {param_name} ──")
        for val in values:
            test_config = base.copy()

            if param_type == "signal":
                # Need different cascade entries
                if param_name == "accel_threshold":
                    test_ck = (42, 126, val, base_ck[3])
                elif param_name == "min_momentum":
                    test_ck = (42, 126, base_ck[2], val)
                else:
                    test_ck = base_ck

                if test_ck not in cascade_cache:
                    cascade_cache[test_ck] = compute_cascade_entries(
                        price_data, test_ck[0], test_ck[1],
                        test_ck[2], test_ck[3], start_epoch=E_2010)
                test_entries = cascade_cache[test_ck]
                test_config["cascade"] = test_ck
            else:
                test_config[param_name] = val
                test_entries = entries

            if len(test_entries) < 5:
                print(f"    {param_name}={val}: Too few entries")
                continue

            r = run_config(test_config, test_entries, price_data,
                           benchmark, regime, E_2010)
            m = extract_metrics(r)
            is_base = ""
            if param_type == "signal":
                if param_name == "accel_threshold" and val == base_ck[2]:
                    is_base = " <-- BASE"
                elif param_name == "min_momentum" and val == base_ck[3]:
                    is_base = " <-- BASE"
            elif val == base[param_name]:
                is_base = " <-- BASE"

            delta_cagr = m["cagr"] - m_base["cagr"]
            print(f"    {param_name}={val:6}: CAGR={m['cagr']:+.1f}% "
                  f"(Δ{delta_cagr:+.1f}) MDD={m['mdd']:.1f}% "
                  f"Cal={m['calmar']:.2f} T={m['trades']}{is_base}")

    # ══════════════════════════════════════════════════════════════════════
    # PART 4: Trade-level analysis
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PART 4: TRADE-LEVEL ANALYSIS")
    print(f"{'='*80}")

    # Analyze top 3 configs
    for config_name in ["A_best_cagr", "C_safe", "D_calmar"]:
        config = CONFIGS[config_name]
        ck = config["cascade"]
        entries = cascade_cache[ck]
        regime = regime_map[config["regime_sma"]]
        r = run_config(config, entries, price_data, benchmark, regime, E_2010)
        analyze_trades(r, config_name)

    # ══════════════════════════════════════════════════════════════════════
    # SUMMARY
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  ROBUSTNESS SUMMARY")
    print(f"{'='*80}")
    print("""
  Key questions answered:
  1. Sub-period consistency: Does the strategy work across all market regimes?
  2. Walk-forward: Does in-sample performance predict out-of-sample?
  3. Parameter sensitivity: Is 30%+ CAGR fragile or robust to param changes?
  4. Trade analysis: Is performance driven by outliers or broad-based?
  """)


if __name__ == "__main__":
    main()

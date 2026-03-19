"""Run all 4 book strategies across top 15 global exchanges.

Usage:
    python run_global.py                # all strategies, all exchanges
    python run_global.py --strategy squeeze  # one strategy, all exchanges
    python run_global.py --exchange US NSE   # all strategies, specific exchanges
"""

import argparse
import json
import os
import sys
import tempfile
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.pipeline import run_pipeline

# Top 15 exchanges by market cap, with per-exchange thresholds
# price_threshold in local currency (~$5 equivalent minimum)
# avg_txn_threshold in local currency (~$1M/day equivalent)
EXCHANGES = {
    "US":   {"price": 20,   "avg_txn": 50000000,  "label": "US (NYSE/NASDAQ/AMEX)"},
    "JPX":  {"price": 500,  "avg_txn": 500000000, "label": "Japan (Tokyo)"},
    "SHH":  {"price": 5,    "avg_txn": 50000000,  "label": "China (Shanghai)"},
    "SHZ":  {"price": 5,    "avg_txn": 50000000,  "label": "China (Shenzhen)"},
    "HKSE": {"price": 5,    "avg_txn": 10000000,  "label": "Hong Kong"},
    "NSE":  {"price": 50,   "avg_txn": 50000000,  "label": "India (NSE)"},
    "LSE":  {"price": 50,   "avg_txn": 5000000,   "label": "UK (London)"},
    "TSX":  {"price": 5,    "avg_txn": 5000000,   "label": "Canada (Toronto)"},
    "XETRA":{"price": 5,    "avg_txn": 5000000,   "label": "Germany (Frankfurt)"},
    "KSC":  {"price": 5000, "avg_txn": 10000000000, "label": "Korea (Seoul)"},
    "TAI":  {"price": 20,   "avg_txn": 100000000, "label": "Taiwan (TWSE)"},
    "ASX":  {"price": 1,    "avg_txn": 5000000,   "label": "Australia (ASX)"},
    "SAO":  {"price": 5,    "avg_txn": 10000000,  "label": "Brazil (B3)"},
    "SES":  {"price": 1,    "avg_txn": 5000000,   "label": "Singapore (SGX)"},
    "JNB":  {"price": 20,   "avg_txn": 10000000,  "label": "South Africa (JSE)"},
}

STRATEGIES = ["darvas_box", "swing_master", "squeeze", "holp_lohp"]

# Base configs per strategy (entry/exit only - scanner is per-exchange)
STRATEGY_CONFIGS = {
    "darvas_box": {
        "static": {
            "start_margin": 500000,
            "start_epoch": 1577836800,
            "end_epoch": 1741219200,
            "prefetch_days": 60,
            "data_granularity": "day",
            "strategy_type": "darvas_box",
        },
        "entry": {"box_min_days": [10], "volume_breakout_mult": [1.5]},
        "exit": {"trailing_stop_pct": [0.08], "max_hold_days": [30]},
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [1],
            "max_positions": [10],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "fixed", "value": 50000}],
        },
    },
    "swing_master": {
        "static": {
            "start_margin": 500000,
            "start_epoch": 1577836800,
            "end_epoch": 1741219200,
            "prefetch_days": 60,
            "data_granularity": "day",
            "strategy_type": "swing_master",
        },
        "entry": {"sma_short": [10], "sma_long": [20], "pullback_days": [3]},
        "exit": {"target_pct": [0.07], "stop_pct": [0.04], "max_hold_days": [20]},
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [1],
            "max_positions": [20],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "fixed", "value": 50000}],
        },
    },
    "squeeze": {
        "static": {
            "start_margin": 500000,
            "start_epoch": 1577836800,
            "end_epoch": 1741219200,
            "prefetch_days": 60,
            "data_granularity": "day",
            "strategy_type": "squeeze",
        },
        "entry": {"bb_period": [20], "bb_std": [2.0], "kc_period": [20], "kc_mult": [1.5], "mom_period": [12]},
        "exit": {"stop_pct": [0.05], "max_hold_days": [20]},
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [1],
            "max_positions": [20],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "fixed", "value": 50000}],
        },
    },
    "holp_lohp": {
        "static": {
            "start_margin": 500000,
            "start_epoch": 1577836800,
            "end_epoch": 1741219200,
            "prefetch_days": 60,
            "data_granularity": "day",
            "strategy_type": "holp_lohp",
        },
        "entry": {"lookback_period": [20]},
        "exit": {"trailing_start_day": [3], "max_hold_days": [20]},
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [1],
            "max_positions": [20],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "fixed", "value": 50000}],
        },
    },
}


def build_config(strategy: str, exchange: str) -> dict:
    """Build a full config dict for a strategy + exchange combo."""
    base = STRATEGY_CONFIGS[strategy]
    ex = EXCHANGES[exchange]
    config = {
        "static": dict(base["static"]),
        "entry": dict(base["entry"]),
        "exit": dict(base["exit"]),
        "simulation": dict(base["simulation"]),
        "scanner": {
            "instruments": [[{"exchange": exchange, "symbols": []}]],
            "price_threshold": [ex["price"]],
            "avg_day_transaction_threshold": [{"period": 20, "threshold": ex["avg_txn"]}],
            "n_day_gain_threshold": [{"n": 30, "threshold": -999}],
        },
    }
    return config


def run_single(strategy: str, exchange: str) -> dict:
    """Run a single strategy+exchange combo, return summary dict."""
    config = build_config(strategy, exchange)

    # Write temp config YAML
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        tmp_path = f.name

    try:
        results = run_pipeline(tmp_path)
    except Exception as e:
        print(f"  ERROR: {strategy} on {exchange}: {e}")
        return {
            "strategy": strategy,
            "exchange": exchange,
            "label": EXCHANGES[exchange]["label"],
            "cagr": None,
            "max_dd": None,
            "calmar": None,
            "orders": 0,
            "error": str(e),
        }
    finally:
        os.unlink(tmp_path)

    if results:
        best = results[0]
        return {
            "strategy": strategy,
            "exchange": exchange,
            "label": EXCHANGES[exchange]["label"],
            "cagr": best.get("cagr"),
            "max_dd": best.get("max_drawdown"),
            "calmar": best.get("calmar_ratio"),
            "sharpe": best.get("sharpe_ratio"),
            "sortino": best.get("sortino_ratio"),
            "orders": best.get("num_trading_days", 0),
            "total_return": best.get("total_return"),
            "error": None,
        }
    else:
        return {
            "strategy": strategy,
            "exchange": exchange,
            "label": EXCHANGES[exchange]["label"],
            "cagr": None,
            "max_dd": None,
            "calmar": None,
            "orders": 0,
            "error": "no results",
        }


def print_summary(all_results):
    """Print a formatted summary table."""
    print("\n" + "=" * 120)
    print("GLOBAL BACKTEST RESULTS (2020-01-01 to 2025-03-06)")
    print("=" * 120)

    # Group by strategy
    for strategy in STRATEGIES:
        strat_results = [r for r in all_results if r["strategy"] == strategy]
        if not strat_results:
            continue

        print(f"\n{'─' * 120}")
        print(f"  {strategy.upper()}")
        print(f"{'─' * 120}")
        print(f"  {'Exchange':<30} {'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Sortino':>8} {'TotalRet':>10}")
        print(f"  {'─' * 28} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 10}")

        # Sort by calmar descending
        strat_results.sort(key=lambda r: r.get("calmar") or -999, reverse=True)

        for r in strat_results:
            if r.get("error"):
                print(f"  {r['label']:<30} {'ERROR':>8} {r['error']}")
                continue

            cagr = f"{r['cagr'] * 100:.1f}%" if r.get("cagr") is not None else "N/A"
            dd = f"{r['max_dd'] * 100:.1f}%" if r.get("max_dd") is not None else "N/A"
            cal = f"{r['calmar']:.2f}" if r.get("calmar") is not None else "N/A"
            sh = f"{r['sharpe']:.2f}" if r.get("sharpe") is not None else "N/A"
            so = f"{r['sortino']:.2f}" if r.get("sortino") is not None else "N/A"
            tr = f"{r['total_return'] * 100:.1f}%" if r.get("total_return") is not None else "N/A"
            print(f"  {r['label']:<30} {cagr:>8} {dd:>8} {cal:>8} {sh:>8} {so:>8} {tr:>10}")

    # Cross-strategy winner per exchange
    print(f"\n{'=' * 120}")
    print("BEST STRATEGY PER EXCHANGE (by Calmar)")
    print(f"{'=' * 120}")
    print(f"  {'Exchange':<30} {'Strategy':<15} {'CAGR':>8} {'MaxDD':>8} {'Calmar':>8}")
    print(f"  {'─' * 28} {'─' * 13} {'─' * 8} {'─' * 8} {'─' * 8}")

    exchanges_seen = set()
    for r in sorted(all_results, key=lambda x: x.get("calmar") or -999, reverse=True):
        if r["exchange"] in exchanges_seen:
            continue
        exchanges_seen.add(r["exchange"])
        if r.get("error") or r.get("calmar") is None:
            continue
        cagr = f"{r['cagr'] * 100:.1f}%"
        dd = f"{r['max_dd'] * 100:.1f}%"
        cal = f"{r['calmar']:.2f}"
        print(f"  {r['label']:<30} {r['strategy']:<15} {cagr:>8} {dd:>8} {cal:>8}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Run book strategies across global exchanges")
    parser.add_argument("--strategy", nargs="*", help="Strategies to run (default: all 4)")
    parser.add_argument("--exchange", nargs="*", help="Exchanges to run (default: all 15)")
    args = parser.parse_args()

    strategies = args.strategy or STRATEGIES
    exchanges = args.exchange or list(EXCHANGES.keys())

    total = len(strategies) * len(exchanges)
    print(f"Running {len(strategies)} strategies x {len(exchanges)} exchanges = {total} backtests\n")

    all_results = []
    run_num = 0
    global_start = time.time()

    for exchange in exchanges:
        for strategy in strategies:
            run_num += 1
            print(f"\n{'#' * 80}")
            print(f"# [{run_num}/{total}] {strategy} on {EXCHANGES[exchange]['label']}")
            print(f"{'#' * 80}")

            t0 = time.time()
            result = run_single(strategy, exchange)
            elapsed = round(time.time() - t0, 1)

            cagr_str = f"{result['cagr'] * 100:.1f}%" if result.get("cagr") is not None else "N/A"
            print(f"  -> {cagr_str} CAGR in {elapsed}s")

            all_results.append(result)

    total_elapsed = round(time.time() - global_start, 1)
    print(f"\nTotal runtime: {total_elapsed}s ({total_elapsed / 60:.1f} min)")

    print_summary(all_results)

    # Save raw results to JSON
    out_path = os.path.join(os.path.dirname(__file__), "results_global.json")
    serializable = []
    for r in all_results:
        s = {k: v for k, v in r.items()}
        serializable.append(s)
    with open(out_path, "w") as f:
        json.dump(serializable, f, indent=2, default=str)
    print(f"Raw results saved to {out_path}")


if __name__ == "__main__":
    main()

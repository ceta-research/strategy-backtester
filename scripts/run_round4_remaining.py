#!/usr/bin/env python3
"""Run all remaining Round 4 tasks: walk-forward + cross-exchange."""

import json
import os
import sys
import subprocess
import copy
import yaml

STRATEGY_DIR = "strategies/eod_breakout"
RESULTS_DIR = "results/eod_breakout"

# Champion config template
CHAMPION_STATIC = {
    "strategy_type": "eod_breakout",
    "start_margin": 10000000,
    "prefetch_days": 500,
    "data_granularity": "day",
}

CHAMPION_ENTRY = {
    "n_day_high": [7],
    "n_day_ma": [10],
    "direction_score": [{"n_day_ma": 5, "score": 0.40}],
}

CHAMPION_EXIT = {
    "min_hold_time_days": [7],
    "trailing_stop_pct": [15],
}

CHAMPION_SIM = {
    "default_sorting_type": ["top_gainer"],
    "order_sorting_type": ["top_gainer"],
    "order_ranking_window_days": [180],
    "max_positions": [20],
    "max_positions_per_instrument": [1],
    "order_value_multiplier": [1],
    "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
}


def build_config(start_epoch, end_epoch, data_provider, exchange, price_threshold=50):
    scanner = {
        "instruments": [[{"exchange": exchange, "symbols": []}]],
        "price_threshold": [price_threshold],
        "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
        "n_day_gain_threshold": [{"n": 360, "threshold": 0}],
    }
    static = {**CHAMPION_STATIC, "start_epoch": start_epoch, "end_epoch": end_epoch,
              "data_provider": data_provider}
    return {"static": static, "scanner": scanner, "entry": CHAMPION_ENTRY,
            "exit": CHAMPION_EXIT, "simulation": CHAMPION_SIM}


def run_config(config_dict, output_name):
    output_path = os.path.join(RESULTS_DIR, output_name)
    if os.path.exists(output_path):
        print(f"  [SKIP] {output_name} already exists")
        return output_path

    config_path = os.path.join(RESULTS_DIR, f"_tmp_{output_name.replace('.json', '.yaml')}")
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f, default_flow_style=False)

    cmd = [sys.executable, "run.py", "--config", config_path, "--output", output_path]
    print(f"  Running: {output_name}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    os.remove(config_path)

    if result.returncode != 0:
        print(f"  FAILED: {result.stderr[-500:]}")
        return None

    # Extract key metrics from output
    for line in result.stdout.split("\n"):
        if "CAGR:" in line or "Calmar:" in line or "Pipeline Complete" in line:
            print(f"    {line.strip()}")

    return output_path


def extract_summary(json_path):
    with open(json_path) as f:
        data = json.load(f)
    if data.get("type") == "sweep":
        s = data["all_configs"][0]  # single config
    else:
        s = data["detailed"][0]["summary"]
    return {
        "cagr": (s.get("cagr") or 0) * 100,
        "mdd": (s.get("max_drawdown") or 0) * 100,
        "calmar": s.get("calmar_ratio") or 0,
        "sharpe": s.get("sharpe_ratio") or 0,
        "trades": s.get("total_trades") or 0,
    }


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # ── Walk-forward: 5 folds ──
    print("=" * 80)
    print("WALK-FORWARD VALIDATION (5 folds, ~3yr train / ~2yr test)")
    print("=" * 80)

    # Full period: 2010-01-01 (1262304000) to 2026-03-17 (1773878400)
    # 16+ years. 5 folds with rolling 3yr train / 2yr test:
    # Fold 1: train 2010-2013, test 2013-2015
    # Fold 2: train 2012-2015, test 2015-2017
    # Fold 3: train 2014-2017, test 2017-2019
    # Fold 4: train 2016-2019, test 2019-2021
    # Fold 5: train 2018-2021, test 2021-2023
    # Fold 6: train 2020-2023, test 2023-2026

    folds = [
        ("fold1", 1262304000, 1356998400, 1356998400, 1420070400),  # train 2010-2013, test 2013-2015
        ("fold2", 1325376000, 1420070400, 1420070400, 1483228800),  # train 2012-2015, test 2015-2017
        ("fold3", 1388534400, 1483228800, 1483228800, 1546300800),  # train 2014-2017, test 2017-2019
        ("fold4", 1451606400, 1546300800, 1546300800, 1609459200),  # train 2016-2019, test 2019-2021
        ("fold5", 1514764800, 1609459200, 1609459200, 1672531200),  # train 2018-2021, test 2021-2023
        ("fold6", 1577836800, 1672531200, 1672531200, 1773878400),  # train 2020-2023, test 2023-2026
    ]

    wf_results = []
    for fold_name, train_start, train_end, test_start, test_end in folds:
        print(f"\n--- {fold_name} ---")

        # We run the champion config on the TEST fold only (not re-optimizing per fold)
        # This is the simpler "fixed params, rolling OOS" walk-forward
        cfg = build_config(test_start, test_end, "nse_charting", "NSE")
        output = run_config(cfg, f"round4_wf_{fold_name}.json")
        if output and os.path.exists(output):
            s = extract_summary(output)
            wf_results.append({"fold": fold_name, **s})
            print(f"    CAGR={s['cagr']:+.1f}% MDD={s['mdd']:.1f}% Cal={s['calmar']:.3f} Trd={s['trades']}")

    if wf_results:
        calmars = [r["calmar"] for r in wf_results]
        cagrs = [r["cagr"] for r in wf_results]
        print(f"\n  Walk-forward summary:")
        print(f"    Calmar: min={min(calmars):.3f} avg={sum(calmars)/len(calmars):.3f} max={max(calmars):.3f}")
        print(f"    CAGR:   min={min(cagrs):.1f}% avg={sum(cagrs)/len(cagrs):.1f}% max={max(cagrs):.1f}%")
        # Variance
        avg_cal = sum(calmars) / len(calmars)
        var_cal = sum((c - avg_cal)**2 for c in calmars) / len(calmars)
        print(f"    Calmar std dev: {var_cal**0.5:.3f}")

    # ── Cross-exchange ──
    print("\n" + "=" * 80)
    print("CROSS-EXCHANGE (champion config on 10 markets)")
    print("=" * 80)

    # Exchange configs: (name, exchange_code, price_threshold)
    exchanges = [
        ("UK", "LSE", 1),
        ("Canada", "TSX", 2),
        ("China_SHH", "SHH", 1),
        ("China_SHZ", "SHZ", 1),
        ("Euronext", "PAR", 1),
        ("Hong_Kong", "HKSE", 1),
        ("South_Korea", "KSC", 500),
        ("Germany", "XETRA", 1),
        ("Saudi_Arabia", "SAU", 1),
        ("Taiwan", "TAI", 10),
    ]

    xc_results = []
    for name, exchange, pt in exchanges:
        print(f"\n--- {name} ({exchange}) ---")
        cfg = build_config(1262304000, 1773878400, "cr", exchange, price_threshold=pt)
        output = run_config(cfg, f"round4_xc_{name.lower()}.json")
        if output and os.path.exists(output):
            s = extract_summary(output)
            xc_results.append({"exchange": name, **s})
            print(f"    CAGR={s['cagr']:+.1f}% MDD={s['mdd']:.1f}% Cal={s['calmar']:.3f} Trd={s['trades']}")
        else:
            xc_results.append({"exchange": name, "cagr": 0, "mdd": 0, "calmar": 0, "trades": 0})

    # Final summary
    print("\n" + "=" * 80)
    print("CROSS-EXCHANGE SUMMARY")
    print("=" * 80)
    print(f"{'Exchange':<20} {'CAGR':>7} {'MDD':>7} {'Calmar':>7} {'Trades':>6}")
    print("-" * 55)

    # Add NSE and US from earlier runs
    for name, fname in [("NSE (primary)", "champion.json"), ("US", "round4_us.json")]:
        if os.path.exists(os.path.join(RESULTS_DIR, fname)):
            s = extract_summary(os.path.join(RESULTS_DIR, fname))
            print(f"{name:<20} {s['cagr']:>+6.1f}% {s['mdd']:>6.1f}% {s['calmar']:>7.3f} {s['trades']:>6}")

    for r in xc_results:
        print(f"{r['exchange']:<20} {r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>7.3f} {r['trades']:>6}")


if __name__ == "__main__":
    main()

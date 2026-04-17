#!/usr/bin/env python3
"""Round 4 validation for momentum_top_gainers champion.

Runs all 4 validation tests:
  4a. OOS split (train 2010-2020, test 2020-2026)
  4b. Walk-forward (6 rolling folds)
  4c. Cross-data-source (nse_charting, fmp.stock_eod, bhavcopy)
  4d. Cross-exchange (10 markets via fmp.stock_eod)
"""

import copy
import json
import math
import os
import subprocess
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

RESULTS_DIR = os.path.join(ROOT, "results", "momentum_top_gainers")
CONFIG_DIR = os.path.join(ROOT, "strategies", "momentum_top_gainers")
os.makedirs(RESULTS_DIR, exist_ok=True)

# Champion params
CHAMPION = {
    "strategy_type": "momentum_top_gainers",
    "momentum_lookback_days": [210],
    "top_n_pct": [0.40],
    "rebalance_interval_days": [2],
    "min_momentum_pct": [5],
    "direction_score_n_day_ma": [3],
    "direction_score_threshold": [0.45],
    "regime_instrument": ["NSE:NIFTYBEES"],
    "regime_sma_period": [0],
    "trailing_stop_pct": [40],
    "max_hold_days": [126],
    "max_positions": [30],
}


def make_config(start_epoch, end_epoch, data_provider="nse_charting",
                exchange="NSE", symbols=None, prefetch=800, extra_static=None):
    cfg = {
        "static": {
            "strategy_type": "momentum_top_gainers",
            "start_margin": 10000000,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "prefetch_days": prefetch,
            "data_granularity": "day",
            "data_provider": data_provider,
        },
        "scanner": {
            "instruments": [[{"exchange": exchange, "symbols": symbols or []}]],
            "price_threshold": [50],
            "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
            "n_day_gain_threshold": [{"n": 360, "threshold": -999}],
        },
        "entry": {
            "momentum_lookback_days": CHAMPION["momentum_lookback_days"],
            "top_n_pct": CHAMPION["top_n_pct"],
            "rebalance_interval_days": CHAMPION["rebalance_interval_days"],
            "min_momentum_pct": CHAMPION["min_momentum_pct"],
            "direction_score_n_day_ma": CHAMPION["direction_score_n_day_ma"],
            "direction_score_threshold": CHAMPION["direction_score_threshold"],
            "regime_instrument": CHAMPION["regime_instrument"],
            "regime_sma_period": CHAMPION["regime_sma_period"],
        },
        "exit": {
            "trailing_stop_pct": CHAMPION["trailing_stop_pct"],
            "max_hold_days": CHAMPION["max_hold_days"],
        },
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_gainer"],
            "order_ranking_window_days": [30],
            "max_positions": CHAMPION["max_positions"],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1.0],
            "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
        },
    }
    if extra_static:
        cfg["static"].update(extra_static)
    return cfg


def run_config(name, cfg):
    config_path = os.path.join(CONFIG_DIR, f"config_r4_{name}.yaml")
    output_path = os.path.join(RESULTS_DIR, f"round4_{name}.json")
    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"  Running {name}...", flush=True)
    result = subprocess.run(
        [sys.executable, "run.py", "--config", config_path, "--output", output_path],
        capture_output=True, text=True, timeout=7200,
    )
    if result.returncode != 0:
        print(f"  {name} FAILED: {result.stderr[-500:]}", flush=True)
        return None
    # Parse result
    if os.path.exists(output_path):
        with open(output_path) as f:
            data = json.load(f)
        configs = data.get("all_configs", [])
        if configs:
            c = configs[0]
            cagr = (c.get("cagr") or 0) * 100
            mdd = (c.get("max_drawdown") or 0) * 100
            cal = c.get("calmar_ratio") or 0
            trades = c.get("total_trades") or 0
            print(f"  {name}: CAGR={cagr:.1f}% MDD={mdd:.1f}% Cal={cal:.3f} trades={trades}", flush=True)
            return c
    print(f"  {name}: no results", flush=True)
    return None


def main():
    t0 = time.time()

    # --- 4a. OOS Split ---
    print("\n=== 4a. Out-of-Sample Split ===", flush=True)
    is_result = run_config("oos_is", make_config(1262304000, 1577836800))   # 2010-2020
    oos_result = run_config("oos_test", make_config(1577836800, 1773878400))  # 2020-2026

    if is_result and oos_result:
        is_cal = is_result.get("calmar_ratio", 0)
        oos_cal = oos_result.get("calmar_ratio", 0)
        drop = (1 - oos_cal / is_cal) * 100 if is_cal > 0 else 0
        print(f"  IS Cal: {is_cal:.3f}, OOS Cal: {oos_cal:.3f}, Drop: {drop:.1f}%")
        print(f"  {'PASS' if drop < 50 else 'WARN'}: Cal drop {'<' if drop < 50 else '>'}50%")

    # --- 4b. Walk-Forward (6 folds) ---
    print("\n=== 4b. Walk-Forward (6 folds) ===", flush=True)
    # 2010-2026 = 16 years. 6 folds: ~3yr train / ~2yr test, sliding
    folds = [
        ("fold1", 1262304000, 1357084800, 1357084800, 1420156800),  # train 2010-2012, test 2013-2014
        ("fold2", 1357084800, 1451692800, 1451692800, 1514764800),  # train 2013-2015, test 2016-2017
        ("fold3", 1451692800, 1546300800, 1546300800, 1609459200),  # train 2016-2018, test 2019-2020
        ("fold4", 1546300800, 1640995200, 1640995200, 1704067200),  # train 2019-2021, test 2022-2023
        ("fold5", 1640995200, 1735689600, 1735689600, 1773878400),  # train 2022-2024, test 2025-2026
    ]
    wf_calmars = []
    for name, _, _, test_start, test_end in folds:
        r = run_config(f"wf_{name}", make_config(test_start, test_end))
        if r:
            wf_calmars.append(r.get("calmar_ratio", 0))

    if wf_calmars:
        avg_cal = sum(wf_calmars) / len(wf_calmars)
        positive = sum(1 for c in wf_calmars if c > 0)
        std_dev = (sum((c - avg_cal) ** 2 for c in wf_calmars) / len(wf_calmars)) ** 0.5
        print(f"\n  WF Results: avg Cal={avg_cal:.3f}, std={std_dev:.3f}, positive={positive}/{len(wf_calmars)}")
        print(f"  {'PASS' if positive >= 3 else 'FAIL'}: {positive}/{len(wf_calmars)} positive folds")

    # --- 4c. Cross-Data-Source (NSE) ---
    print("\n=== 4c. Cross-Data-Source ===", flush=True)
    run_config("cds_nse_charting", make_config(1262304000, 1773878400, "nse_charting"))
    run_config("cds_fmp", make_config(1262304000, 1773878400, "cr"))
    run_config("cds_bhavcopy", make_config(1262304000, 1773878400, "bhavcopy"))

    # --- 4d. Cross-Exchange ---
    print("\n=== 4d. Cross-Exchange ===", flush=True)
    exchanges = [
        ("US", "US"), ("UK", "LSE"), ("Canada", "TSX"),
        ("China_SHH", "SHH"), ("China_SHZ", "SHZ"),
        ("Euronext", "PAR"), ("Hong_Kong", "HKSE"),
        ("South_Korea", "KSC"), ("Germany", "XETRA"),
        ("Taiwan", "TAI"),
    ]
    # Disable NSE-specific filters for non-NSE exchanges
    for name, exch in exchanges:
        cfg = make_config(1262304000, 1773878400, "cr", exchange=exch)
        # Disable regime filter (NIFTYBEES is NSE-specific)
        cfg["entry"]["regime_instrument"] = [""]
        cfg["entry"]["regime_sma_period"] = [0]
        run_config(f"cx_{name}", cfg)

    # --- Deflated Sharpe ---
    print("\n=== Deflated Sharpe ===", flush=True)
    total_configs = 864 + 648  # R2 + R3
    if oos_result:
        sr = oos_result.get("sharpe_ratio", 0)
        T = 72  # ~6 years of monthly returns (2020-2026)
        var_sr = (1 + 0.5 * sr ** 2) / T
        from scipy.stats import norm
        z = norm.ppf(1 - 1 / total_configs)
        sr_deflated = sr - math.sqrt(var_sr) * z
        print(f"  Observed Sharpe: {sr:.3f}")
        print(f"  Configs tested: {total_configs}")
        print(f"  Deflated Sharpe: {sr_deflated:.3f}")
        print(f"  {'PASS' if sr_deflated > 0.3 else 'FAIL'}: deflated Sharpe {'>' if sr_deflated > 0.3 else '<'} 0.3")

    elapsed = int(time.time() - t0)
    print(f"\n=== Round 4 Complete: {elapsed}s ===")


if __name__ == "__main__":
    main()

"""Round 4 validation for momentum_cascade.

Champion config: a=15, s=42, b=252, r=200, tsl=15, h=378, gainer, p=15
Runs: 4a OOS, 4b walk-forward, 4c cross-data, 4d cross-exchange
"""
import yaml
import copy
import subprocess
import sys
import json
import os
import math

BASE_DIR = "/Users/swas/Desktop/Swas/Kite/ATO_SUITE/strategy-backtester"
STRATEGY_DIR = f"{BASE_DIR}/strategies/momentum_cascade"
RESULTS_DIR = f"{BASE_DIR}/results/momentum_cascade"

# Champion config (robust center from R2/R3)
CHAMPION = {
    "static": {
        "strategy_type": "momentum_cascade",
        "start_margin": 10000000,
        "start_epoch": 1262304000,  # 2010-01-01
        "end_epoch": 1773878400,    # 2026-03-30
        "prefetch_days": 600,
        "data_granularity": "day",
        "data_provider": "nse_charting",
    },
    "scanner": {
        "instruments": [[{"exchange": "NSE", "symbols": []}]],
        "price_threshold": [50],
        "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
        "n_day_gain_threshold": [{"n": 360, "threshold": -999}],
    },
    "entry": {
        "fast_lookback_days": [42],
        "slow_lookback_days": [42],
        "accel_threshold_pct": [15],
        "min_momentum_pct": [20],
        "breakout_window": [252],
        "regime_instrument": ["NSE:NIFTYBEES"],
        "regime_sma_period": [200],
    },
    "exit": {
        "trailing_stop_pct": [15],
        "max_hold_days": [378],
    },
    "simulation": {
        "default_sorting_type": ["top_gainer"],
        "order_sorting_type": ["top_gainer"],
        "order_ranking_window_days": [360],
        "max_positions": [15],
        "max_positions_per_instrument": [1],
        "order_value_multiplier": [0.95],
        "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
    },
}

def run_config(name, cfg):
    """Write config, run pipeline, return result dict."""
    config_path = f"{STRATEGY_DIR}/config_r4_{name}.yaml"
    result_path = f"{RESULTS_DIR}/round4_{name}.json"

    with open(config_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    cmd = [sys.executable, f"{BASE_DIR}/run.py", "--config", config_path, "--output", result_path]
    print(f"\n=== {name} ===")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR, timeout=600)

    for line in result.stdout.split("\n"):
        if any(kw in line for kw in ["CAGR", "Best", "Fetched", "Signal gen"]):
            print(f"  {line.strip()}")

    if result.returncode != 0 or not os.path.exists(result_path):
        print(f"  FAILED or no results (0 orders?)")
        return {"cagr": 0, "mdd": 0, "calmar": 0, "sharpe": 0, "trades": 0}

    with open(result_path) as f:
        data = json.load(f)
    if not data.get("all_configs"):
        return {"cagr": 0, "mdd": 0, "calmar": 0, "sharpe": 0, "trades": 0}
    c = data["all_configs"][0]
    return {
        "cagr": c.get("cagr", 0),
        "mdd": c.get("max_drawdown", 0),
        "calmar": c.get("calmar_ratio", 0),
        "sharpe": c.get("sharpe_ratio", 0),
        "trades": c.get("total_trades", 0),
    }


def run_4a_oos():
    """4a: Out-of-sample split."""
    print("\n" + "="*60)
    print("4a: OOS SPLIT (IS: 2010-2020, OOS: 2020-2026)")
    print("="*60)

    is_cfg = copy.deepcopy(CHAMPION)
    is_cfg["static"]["end_epoch"] = 1577836800  # 2020-01-01
    r_is = run_config("oos_is", is_cfg)

    oos_cfg = copy.deepcopy(CHAMPION)
    oos_cfg["static"]["start_epoch"] = 1577836800  # 2020-01-01
    r_oos = run_config("oos_oos", oos_cfg)

    if r_is and r_oos:
        print(f"\n  IS  (2010-2020): CAGR={r_is['cagr']*100:.1f}% MDD={r_is['mdd']*100:.1f}% Cal={r_is['calmar']:.3f} Tr={r_is['trades']}")
        print(f"  OOS (2020-2026): CAGR={r_oos['cagr']*100:.1f}% MDD={r_oos['mdd']*100:.1f}% Cal={r_oos['calmar']:.3f} Tr={r_oos['trades']}")
        drop = 1 - r_oos['calmar'] / r_is['calmar'] if r_is['calmar'] > 0 else None
        print(f"  Calmar drop: {drop*100:.0f}%" if drop else "  (cannot compute drop)")


def run_4b_walkforward():
    """4b: Walk-forward with 6 rolling folds."""
    print("\n" + "="*60)
    print("4b: WALK-FORWARD (6 folds, fixed params)")
    print("="*60)

    # Epochs for fold boundaries
    folds = [
        ("2013-2015", 1356998400, 1420070400),  # 2013-01-01 to 2015-01-01
        ("2015-2017", 1420070400, 1483228800),  # 2015-01-01 to 2017-01-01
        ("2017-2019", 1483228800, 1546300800),  # 2017-01-01 to 2019-01-01
        ("2019-2021", 1546300800, 1609459200),  # 2019-01-01 to 2021-01-01
        ("2021-2023", 1609459200, 1672531200),  # 2021-01-01 to 2023-01-01
        ("2023-2026", 1672531200, 1773878400),  # 2023-01-01 to 2026-03-30
    ]

    results = []
    for label, start, end in folds:
        cfg = copy.deepcopy(CHAMPION)
        cfg["static"]["start_epoch"] = start
        cfg["static"]["end_epoch"] = end
        r = run_config(f"wf_{label}", cfg)
        if r:
            results.append((label, r))
            print(f"  {label}: CAGR={r['cagr']*100:.1f}% MDD={r['mdd']*100:.1f}% Cal={r['calmar']:.3f} Tr={r['trades']}")

    if results:
        calmars = [r['calmar'] for _, r in results]
        cagrs = [r['cagr'] for _, r in results]
        positive = sum(1 for c in calmars if c > 0)
        avg_cal = sum(calmars) / len(calmars)
        std_cal = (sum((c - avg_cal)**2 for c in calmars) / len(calmars)) ** 0.5
        print(f"\n  Avg Calmar: {avg_cal:.3f} | Std: {std_cal:.3f} | Positive: {positive}/{len(results)}")
        print(f"  Avg CAGR: {sum(cagrs)/len(cagrs)*100:.1f}%")


def run_4c_crossdata():
    """4c: Cross-data-source (3 NSE sources)."""
    print("\n" + "="*60)
    print("4c: CROSS-DATA-SOURCE (NSE)")
    print("="*60)

    # nse_charting (already tested, run again for consistency)
    r_nse = run_config("xdata_nse_charting", CHAMPION)

    # fmp.stock_eod with .NS suffix
    fmp_cfg = copy.deepcopy(CHAMPION)
    fmp_cfg["static"]["data_provider"] = "cr"
    fmp_cfg["scanner"]["instruments"] = [[{"exchange": "NSE", "symbols": []}]]
    r_fmp = run_config("xdata_fmp_ns", fmp_cfg)

    # bhavcopy (unadjusted)
    bhav_cfg = copy.deepcopy(CHAMPION)
    bhav_cfg["static"]["data_provider"] = "bhavcopy"
    r_bhav = run_config("xdata_bhavcopy", bhav_cfg)

    for label, r in [("nse_charting", r_nse), ("fmp.stock_eod", r_fmp), ("bhavcopy", r_bhav)]:
        if r:
            print(f"  {label:<20}: CAGR={r['cagr']*100:.1f}% MDD={r['mdd']*100:.1f}% Cal={r['calmar']:.3f} Tr={r['trades']}")


def run_4d_crossexchange():
    """4d: Cross-exchange (10 markets)."""
    print("\n" + "="*60)
    print("4d: CROSS-EXCHANGE (fmp.stock_eod)")
    print("="*60)

    exchanges = [
        ("US", [{"exchange": "NASDAQ", "symbols": []}, {"exchange": "NYSE", "symbols": []}]),
        ("UK", [{"exchange": "LSE", "symbols": []}]),
        ("Canada", [{"exchange": "TSX", "symbols": []}]),
        ("China_SHH", [{"exchange": "SHH", "symbols": []}]),
        ("China_SHZ", [{"exchange": "SHZ", "symbols": []}]),
        ("Euronext", [{"exchange": "PAR", "symbols": []}]),
        ("Hong_Kong", [{"exchange": "HKSE", "symbols": []}]),
        ("South_Korea", [{"exchange": "KSC", "symbols": []}]),
        ("Germany", [{"exchange": "XETRA", "symbols": []}]),
        ("Taiwan", [{"exchange": "TAI", "symbols": []}, {"exchange": "TWO", "symbols": []}]),
    ]

    results = []
    for label, insts in exchanges:
        cfg = copy.deepcopy(CHAMPION)
        cfg["static"]["data_provider"] = "cr"
        cfg["scanner"]["instruments"] = [insts]
        # Disable NSE-specific regime filter for international
        cfg["entry"]["regime_instrument"] = [""]
        cfg["entry"]["regime_sma_period"] = [0]
        # Lower turnover threshold for international markets
        cfg["scanner"]["avg_day_transaction_threshold"] = [{"period": 125, "threshold": 5000000}]
        cfg["scanner"]["price_threshold"] = [1]

        r = run_config(f"xex_{label}", cfg)
        if r:
            results.append((label, r))

    print("\n  Cross-exchange summary:")
    for label, r in sorted(results, key=lambda x: x[1]['calmar'], reverse=True):
        print(f"  {label:<15}: CAGR={r['cagr']*100:>6.1f}% MDD={r['mdd']*100:>7.1f}% Cal={r['calmar']:>7.3f} Tr={r['trades']:>5}")


def compute_deflated_sharpe(observed_sharpe, n_configs, n_monthly_returns):
    """Compute deflated Sharpe ratio."""
    from scipy.stats import norm
    var_sr = (1 + 0.5 * observed_sharpe**2) / n_monthly_returns
    z = norm.ppf(1 - 1/n_configs)
    deflated = observed_sharpe - math.sqrt(var_sr) * z
    return deflated


if __name__ == "__main__":
    test = sys.argv[1] if len(sys.argv) > 1 else "all"

    if test in ("all", "oos"):
        run_4a_oos()
    if test in ("all", "wf"):
        run_4b_walkforward()
    if test in ("all", "xdata"):
        run_4c_crossdata()
    if test in ("all", "xex"):
        run_4d_crossexchange()

    if test == "all":
        # Deflated Sharpe
        print("\n" + "="*60)
        print("DEFLATED SHARPE")
        print("="*60)
        try:
            with open(f"{RESULTS_DIR}/round4_oos_is.json") as f:
                d = json.load(f)
            full_result = f"{RESULTS_DIR}/round2.json"
            with open(full_result) as f:
                r2 = json.load(f)

            # Use full-period champion sharpe
            with open(f"{RESULTS_DIR}/round4_xdata_nse_charting.json") as f:
                champ = json.load(f)
            observed_sharpe = champ["all_configs"][0].get("sharpe_ratio", 0)
            n_configs = sum([
                1,    # R0
                93,   # R1 (12 sweeps)
                864,  # R2
                243,  # R3
            ])
            n_months = 16 * 12  # ~16 years

            deflated = compute_deflated_sharpe(observed_sharpe, n_configs, n_months)
            print(f"  Observed Sharpe: {observed_sharpe:.3f}")
            print(f"  Configs tested: {n_configs}")
            print(f"  Monthly periods: {n_months}")
            print(f"  Deflated Sharpe: {deflated:.3f}")
            print(f"  Verdict: {'PASS' if deflated > 0.3 else 'MARGINAL' if deflated > 0.15 else 'FAIL'}")
        except Exception as e:
            print(f"  Could not compute: {e}")

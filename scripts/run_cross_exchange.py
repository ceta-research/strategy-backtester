#!/usr/bin/env python3
"""Run cross-exchange tests for eod_breakout champion config."""

import json
import os
import sys
import subprocess
import tempfile

RESULTS_DIR = "results/eod_breakout"

EXCHANGES = [
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

CONFIG_TEMPLATE = """# Cross-exchange: {name} ({exchange})
static:
  strategy_type: eod_breakout
  start_margin: 10000000
  start_epoch: 1262304000
  end_epoch: 1773878400
  prefetch_days: 500
  data_granularity: day
  data_provider: cr

scanner:
  instruments:
    - [{{exchange: {exchange}, symbols: []}}]
  price_threshold: [{pt}]
  avg_day_transaction_threshold:
    - {{period: 125, threshold: 70000000}}
  n_day_gain_threshold:
    - {{n: 360, threshold: 0}}

entry:
  n_day_high: [7]
  n_day_ma: [10]
  direction_score:
    - {{n_day_ma: 5, score: 0.40}}

exit:
  min_hold_time_days: [7]
  trailing_stop_pct: [15]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [180]
  max_positions: [20]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {{type: percentage_of_instrument_avg_txn, value: 4.5}}
"""


def run_exchange(name, exchange, pt):
    output_path = os.path.join(RESULTS_DIR, f"round4_xc_{name.lower()}.json")
    if os.path.exists(output_path):
        print(f"  [SKIP] {name} already exists")
        return output_path

    config_content = CONFIG_TEMPLATE.format(name=name, exchange=exchange, pt=pt)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    try:
        cmd = [sys.executable, "run.py", "--config", config_path, "--output", output_path]
        print(f"  Running {name} ({exchange})...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"  FAILED: {result.stderr[-300:]}")
            return None
        # Print key lines
        for line in result.stdout.split("\n"):
            if any(k in line for k in ["CAGR=", "Calmar:", "Pipeline Complete", "Fetched"]):
                print(f"    {line.strip()}")
        return output_path
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT (600s)")
        return None
    finally:
        os.unlink(config_path)


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)

    results = []
    for name, exchange, pt in EXCHANGES:
        print(f"\n--- {name} ({exchange}) ---")
        path = run_exchange(name, exchange, pt)
        if path and os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            s = data.get("detailed", [{}])[0].get("summary", data.get("all_configs", [{}])[0])
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            trd = s.get("total_trades") or 0
            results.append({"exchange": name, "cagr": cagr, "mdd": mdd, "calmar": cal, "trades": trd})
        else:
            results.append({"exchange": name, "cagr": 0, "mdd": 0, "calmar": 0, "trades": 0, "error": True})

    # Summary
    print("\n" + "=" * 70)
    print("CROSS-EXCHANGE SUMMARY")
    print("=" * 70)
    print(f"{'Exchange':<20} {'CAGR':>7} {'MDD':>7} {'Calmar':>7} {'Trades':>6}")
    print("-" * 55)

    # Add NSE and US from earlier
    for name, fname in [("NSE (primary)", "champion.json"), ("US", "round4_us.json")]:
        path = os.path.join(RESULTS_DIR, fname)
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            s = data["detailed"][0]["summary"]
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            trd = s.get("total_trades") or 0
            print(f"{name:<20} {cagr:>+6.1f}% {mdd:>6.1f}% {cal:>7.3f} {trd:>6}")

    for r in results:
        flag = " *" if r.get("error") else ""
        print(f"{r['exchange']:<20} {r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>7.3f} {r['trades']:>6}{flag}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run full R4 validation for the confirmed champion: ndh=7, ndm=5, ds={3,0.54}, tsl=8, pos=15."""

import json
import os
import sys
import subprocess
import tempfile

RESULTS_DIR = "results/eod_breakout"

CHAMPION_YAML = """
static:
  strategy_type: eod_breakout
  start_margin: 10000000
  start_epoch: {start}
  end_epoch: {end}
  prefetch_days: 500
  data_granularity: day
  data_provider: {provider}

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
  n_day_ma: [5]
  direction_score:
    - {{n_day_ma: 3, score: 0.54}}

exit:
  min_hold_time_days: [0]
  trailing_stop_pct: [8]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [180]
  max_positions: [15]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {{type: percentage_of_instrument_avg_txn, value: 4.5}}
"""


def run(name, output_name, start, end, provider="nse_charting", exchange="NSE", pt=50):
    output_path = os.path.join(RESULTS_DIR, output_name)
    if os.path.exists(output_path):
        s = extract(output_path)
        print(f"  [SKIP] {name}: CAGR={s['cagr']:+.1f}% MDD={s['mdd']:.1f}% Cal={s['cal']:.3f} Trd={s['trd']}")
        return s

    yaml_content = CHAMPION_YAML.format(start=start, end=end, provider=provider, exchange=exchange, pt=pt)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        config_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, "run.py", "--config", config_path, "--output", output_path],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            print(f"  FAILED {name}: {result.stderr[-200:]}")
            return None
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT {name}")
        return None
    finally:
        os.unlink(config_path)

    s = extract(output_path)
    print(f"  {name}: CAGR={s['cagr']:+.1f}% MDD={s['mdd']:.1f}% Cal={s['cal']:.3f} Trd={s['trd']}")
    return s


def extract(path):
    with open(path) as f:
        data = json.load(f)
    if data.get("type") == "sweep":
        s = data["all_configs"][0]
    else:
        s = data["detailed"][0]["summary"]
    return {
        "cagr": (s.get("cagr") or 0) * 100,
        "mdd": (s.get("max_drawdown") or 0) * 100,
        "cal": s.get("calmar_ratio") or 0,
        "sharpe": s.get("sharpe_ratio") or 0,
        "trd": s.get("total_trades") or 0,
    }


def main():
    print("=" * 70)
    print("R4 VALIDATION: ndh=7, ndm=5, ds={3,0.54}, tsl=8, pos=15")
    print("=" * 70)

    # 4a. OOS split
    print("\n--- 4a. OOS Split ---")
    run("IS 2010-2020", "r4v2_is.json", 1262304000, 1577836800)
    run("OOS 2020-2026", "r4v2_oos.json", 1577836800, 1773878400)

    # 4b. Walk-forward
    print("\n--- 4b. Walk-Forward ---")
    folds = [
        ("WF 2013-2015", "r4v2_wf1.json", 1356998400, 1420070400),
        ("WF 2015-2017", "r4v2_wf2.json", 1420070400, 1483228800),
        ("WF 2017-2019", "r4v2_wf3.json", 1483228800, 1546300800),
        ("WF 2019-2021", "r4v2_wf4.json", 1546300800, 1609459200),
        ("WF 2021-2023", "r4v2_wf5.json", 1609459200, 1672531200),
        ("WF 2023-2026", "r4v2_wf6.json", 1672531200, 1773878400),
    ]
    wf_cals = []
    for name, fname, start, end in folds:
        s = run(name, fname, start, end)
        if s:
            wf_cals.append(s["cal"])
    if wf_cals:
        avg = sum(wf_cals) / len(wf_cals)
        pos = sum(1 for c in wf_cals if c > 0)
        print(f"  Summary: avg Cal={avg:.3f}, positive={pos}/{len(wf_cals)}")

    # 4c. Cross-data-source
    print("\n--- 4c. Cross-Data-Source ---")
    run("FMP NSE", "r4v2_fmp_nse.json", 1262304000, 1773878400, provider="cr")
    run("Bhavcopy", "r4v2_bhavcopy.json", 1262304000, 1773878400, provider="bhavcopy")

    # 4d. Cross-exchange
    print("\n--- 4d. Cross-Exchange ---")
    exchanges = [
        ("US", "US", 5), ("UK", "LSE", 1), ("Canada", "TSX", 2),
        ("China SHH", "SHH", 1), ("China SHZ", "SHZ", 1),
        ("Euronext", "PAR", 1), ("Hong Kong", "HKSE", 1),
        ("South Korea", "KSC", 500), ("Germany", "XETRA", 1),
        ("Saudi Arabia", "SAU", 1), ("Taiwan", "TAI", 10),
    ]
    for name, exc, pt in exchanges:
        run(name, f"r4v2_xc_{exc.lower()}.json", 1262304000, 1773878400, provider="cr", exchange=exc, pt=pt)

    print("\n" + "=" * 70)
    print("R4 VALIDATION COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""low_pe walk-forward harness — modern window (FMP data-window caveat)."""

import copy, json, os, sys, statistics
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from engine.data_provider import NseChartingDataProvider
from engine.pipeline import run_pipeline

# 5 rolling 2-yr folds across modern window 2018-2026 (data-window caveat).
FOLDS = [
    ("2018-01-01 → 2020-01-01", 1514764800, 1577836800),
    ("2019-01-01 → 2021-01-01", 1546300800, 1609459200),
    ("2020-01-01 → 2022-01-01", 1577836800, 1640995200),
    ("2021-01-01 → 2023-01-01", 1609459200, 1672531200),
    ("2022-01-01 → 2024-01-01", 1640995200, 1704067200),
    ("2023-01-01 → 2026-03-19", 1672531200, 1773878400),
]
CHAMPION = "strategies/low_pe/config_champion.yaml"

def run_fold(label, start_epoch, end_epoch, provider):
    with open(CHAMPION) as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    raw["static"]["start_epoch"] = start_epoch
    raw["static"]["end_epoch"] = end_epoch
    tmp = Path("/tmp/lp_wf_configs"); tmp.mkdir(exist_ok=True)
    tmp_path = tmp / f"fold_{start_epoch}.yaml"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f)
    print(f"\n=== FOLD: {label} ===")
    sweep = run_pipeline(str(tmp_path), data_provider=provider)
    tuples = getattr(sweep, "configs", [])
    if not tuples:
        return None
    _, br = tuples[0]
    d = br.to_dict(); s = d.get("summary", {})
    return {"fold": label, "cagr": s.get("cagr"), "max_drawdown": s.get("max_drawdown"),
            "calmar_ratio": s.get("calmar_ratio"), "sharpe_ratio": s.get("sharpe_ratio"),
            "total_trades": s.get("total_trades")}

def main():
    provider = NseChartingDataProvider()
    results = []
    for label, s, e in FOLDS:
        r = run_fold(label, s, e, provider)
        if r: results.append(r)
    print("\n\n=== WALK-FORWARD SUMMARY ===")
    print(f"{'Fold':<30} {'CAGR':>7} {'MDD':>7} {'Cal':>7} {'Shp':>7} {'trd':>5}")
    for r in results:
        cagr_s = f"{r['cagr']*100:>6.2f}%" if r['cagr'] is not None else "   N/A"
        mdd_s = f"{r['max_drawdown']*100:>6.1f}%" if r['max_drawdown'] is not None else "   N/A"
        cal_s = f"{r['calmar_ratio']:>7.3f}" if r['calmar_ratio'] is not None else "    N/A"
        shp_s = f"{r['sharpe_ratio']:>7.3f}" if r['sharpe_ratio'] is not None else "    N/A"
        print(f"{r['fold']:<30} {cagr_s} {mdd_s} {cal_s} {shp_s} {r['total_trades']:>5}")
    cals = [r['calmar_ratio'] for r in results if r['calmar_ratio'] is not None]
    pos = sum(1 for c in cals if c > 0)
    print(f"\nPositive folds: {pos}/{len(cals)}")
    if cals:
        print(f"Mean Calmar: {sum(cals)/len(cals):.3f}")
        if len(cals) > 1: print(f"Std Calmar:  {statistics.stdev(cals):.3f}")
    out = "results/low_pe/round4_walkforward.json"
    with open(out, "w") as f: json.dump(results, f, indent=2)
    print(f"\nSaved: {out}")

if __name__ == "__main__":
    main()

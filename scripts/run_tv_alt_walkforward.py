#!/usr/bin/env python3
"""Walk-forward for alt-champion (roe=0.08, top_n=100, tsl=15, pos=20)."""

import copy, json, os, sys, statistics
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from engine.data_provider import NseChartingDataProvider
from engine.pipeline import run_pipeline

FOLDS = [
    ("2010-01-01 → 2013-01-01", 1262304000, 1356998400),
    ("2013-01-01 → 2016-01-01", 1356998400, 1451606400),
    ("2016-01-01 → 2019-01-01", 1451606400, 1546300800),
    ("2019-01-01 → 2022-01-01", 1546300800, 1640995200),
    ("2022-01-01 → 2025-01-01", 1640995200, 1735689600),
]
CHAMPION = "strategies/trending_value/config_alt_champion.yaml"

def run_fold(label, start_epoch, end_epoch, provider):
    with open(CHAMPION) as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    raw["static"]["start_epoch"] = start_epoch
    raw["static"]["end_epoch"] = end_epoch
    tmp = Path("/tmp/tv_alt_wf"); tmp.mkdir(exist_ok=True)
    tmp_path = tmp / f"fold_{start_epoch}.yaml"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f)
    print(f"\n=== FOLD: {label} ===")
    sweep = run_pipeline(str(tmp_path), data_provider=provider)
    tuples = getattr(sweep, "configs", [])
    if not tuples: return None
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
    print("\n\n=== ALT-CHAMPION WALK-FORWARD ===")
    print(f"{'Fold':<30} {'CAGR':>7} {'MDD':>7} {'Cal':>7}")
    for r in results:
        print(f"{r['fold']:<30} {r['cagr']*100:>6.2f}% {r['max_drawdown']*100:>6.1f}% {r['calmar_ratio']:>7.3f}")
    cals = [r['calmar_ratio'] for r in results if r['calmar_ratio'] is not None]
    pos = sum(1 for c in cals if c > 0)
    print(f"\nPositive: {pos}/{len(cals)}  Mean Cal: {sum(cals)/len(cals):.3f}  Std: {statistics.stdev(cals):.3f}")

if __name__ == "__main__":
    main()

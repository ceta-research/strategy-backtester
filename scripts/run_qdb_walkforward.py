#!/usr/bin/env python3
"""quality_dip_buy walk-forward harness.

Runs the champion config against 5 rolling 3-year windows on
nse.nse_charting_day. Reports CAGR, MDD, Calmar per fold.

Usage:
    python scripts/run_qdb_walkforward.py
"""

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from engine.data_provider import NseChartingDataProvider
from engine.pipeline import run_pipeline


FOLDS = [
    ("2010-01-01 → 2013-01-01", 1262304000, 1356998400),  # ~3yr
    ("2013-01-01 → 2016-01-01", 1356998400, 1451606400),
    ("2016-01-01 → 2019-01-01", 1451606400, 1546300800),
    ("2019-01-01 → 2022-01-01", 1546300800, 1640995200),
    ("2022-01-01 → 2025-01-01", 1640995200, 1735689600),
]

CHAMPION_CONFIG = "strategies/quality_dip_buy/config_champion.yaml"


def run_fold(label, start_epoch, end_epoch, provider):
    with open(CHAMPION_CONFIG) as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    raw["static"]["start_epoch"] = start_epoch
    raw["static"]["end_epoch"] = end_epoch

    tmp_dir = Path("/tmp/qdb_wf_configs")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"fold_{start_epoch}.yaml"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f)

    print(f"\n=== FOLD: {label} ===")
    sweep = run_pipeline(str(tmp_path), data_provider=provider)
    # SweepResult.configs is list of (cfg_dict, BacktestResult)
    tuples = getattr(sweep, "configs", [])
    if not tuples:
        return None
    _, br = tuples[0]
    d = br.to_dict()
    summary = d.get("summary", {})
    return {
        "fold": label,
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "cagr": summary.get("cagr"),
        "max_drawdown": summary.get("max_drawdown"),
        "calmar_ratio": summary.get("calmar_ratio"),
        "sharpe_ratio": summary.get("sharpe_ratio"),
        "total_trades": summary.get("total_trades"),
        "win_rate": summary.get("win_rate"),
    }


def main():
    provider = NseChartingDataProvider()
    results = []
    for label, s, e in FOLDS:
        r = run_fold(label, s, e, provider)
        if r:
            results.append(r)

    print("\n\n=== WALK-FORWARD SUMMARY ===")
    print(f"{'Fold':<30} {'CAGR':>7} {'MDD':>7} {'Cal':>7} {'Shp':>7} {'trd':>6}")
    for r in results:
        cagr = r["cagr"]
        mdd = r["max_drawdown"]
        cal = r["calmar_ratio"]
        shp = r["sharpe_ratio"]
        trd = r["total_trades"]
        cagr_s = f"{cagr*100:>6.2f}%" if cagr is not None else "   N/A"
        mdd_s = f"{mdd*100:>6.1f}%" if mdd is not None else "   N/A"
        cal_s = f"{cal:>7.3f}" if cal is not None else "    N/A"
        shp_s = f"{shp:>7.3f}" if shp is not None else "    N/A"
        print(f"{r['fold']:<30} {cagr_s} {mdd_s} {cal_s} {shp_s} {trd:>6}")

    cals = [r["calmar_ratio"] for r in results if r["calmar_ratio"] is not None]
    pos = sum(1 for c in cals if c > 0)
    print()
    print(f"Positive folds: {pos}/{len(cals)}")
    if cals:
        print(f"Mean Calmar: {sum(cals)/len(cals):.3f}")
        import statistics
        if len(cals) > 1:
            print(f"Std Calmar:  {statistics.stdev(cals):.3f}")

    out = "results/quality_dip_buy/round4_walkforward.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generic R4c runner: re-run a strategy's champion config under alternative
NSE data sources.

Usage:
    python scripts/run_r4c_generic.py --strategy <name> --champion-config <path>

Outputs per-strategy R4c results to results/<strategy>/round4c_<source>.json
"""
import argparse
import os
import sys
import time
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.pipeline import run_pipeline


# data_provider value → result-file suffix
ALT_SOURCES = [
    ("cr", "fmp"),         # CRDataProvider against fmp.stock_eod (NSE: pass .NS suffix)
    ("bhavcopy", "bhavcopy"),
]


def run_one(strategy_name, champion_cfg, provider, suffix):
    out = f"results/{strategy_name}/round4c_{suffix}.json"
    if os.path.exists(out):
        print(f"  [SKIP] {out} exists")
        return out

    # Load champion config, override data_provider, write to temp
    cfg = yaml.safe_load(open(champion_cfg))
    cfg["static"]["data_provider"] = provider

    tmp_cfg = f"/tmp/r4c_{strategy_name}_{suffix}.yaml"
    with open(tmp_cfg, "w") as f:
        yaml.safe_dump(cfg, f)

    print(f"  Running {strategy_name} on {provider} ({suffix}) ...")
    t0 = time.time()
    try:
        result = run_pipeline(tmp_cfg)
        elapsed = round(time.time() - t0, 1)
        result.save(out)
        if hasattr(result, "all_configs") and result.all_configs:
            c = result.all_configs[0]
            print(f"    [{elapsed}s] CAGR {c['cagr']*100:.2f}% MDD {c['max_drawdown']*100:.2f}% Cal {c['calmar_ratio']:.3f}")
        return out
    except Exception as e:
        print(f"    FAILED: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--champion-config", required=True)
    args = ap.parse_args()

    os.makedirs(f"results/{args.strategy}", exist_ok=True)
    print(f"=== R4c for {args.strategy} ===")

    for provider, suffix in ALT_SOURCES:
        run_one(args.strategy, args.champion_config, provider, suffix)


if __name__ == "__main__":
    main()

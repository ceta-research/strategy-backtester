#!/usr/bin/env python3
"""Generic R4d cross-exchange runner.

Runs a strategy's champion config across the 11 listed exchanges via
fmp.stock_eod (cr provider). Skips strategies that have fundamental
dependencies (those won't work cleanly cross-exchange).

Usage:
    python scripts/run_r4d_generic.py --strategy <name> --champion-config <path>
"""
import argparse
import os
import sys
import time
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.pipeline import run_pipeline


# (label, exchange code, price_threshold)
EXCHANGES = [
    ("US",         "NASDAQ", 1),
    ("UK",         "LSE", 1),
    ("Canada",     "TSX", 2),
    ("China_SHH",  "SHH", 1),
    ("China_SHZ",  "SHZ", 1),
    ("Euronext",   "PAR", 1),
    ("Hong_Kong",  "HKSE", 1),
    ("South_Korea","KSC", 500),
    ("Germany",    "XETRA", 1),
    ("Saudi_Arabia","SAU", 1),
    ("Taiwan",     "TAI", 10),
]


def run_one(strategy, champion_cfg, label, exchange, pt):
    out = f"results/{strategy}/round4d_xc_{label.lower()}.json"
    if os.path.exists(out):
        print(f"  [SKIP] {out} exists")
        return out

    cfg = yaml.safe_load(open(champion_cfg))
    cfg["static"]["data_provider"] = "cr"
    # Override scanner instruments to target exchange
    cfg["scanner"]["instruments"] = [[{"exchange": exchange, "symbols": []}]]
    # Override price threshold for the exchange
    cfg["scanner"]["price_threshold"] = [pt]

    tmp_cfg = f"/tmp/r4d_{strategy}_{label}.yaml"
    with open(tmp_cfg, "w") as f:
        yaml.safe_dump(cfg, f)

    print(f"  {label:14} ({exchange}) ...", end=" ", flush=True)
    t0 = time.time()
    try:
        result = run_pipeline(tmp_cfg)
        elapsed = round(time.time() - t0, 1)
        result.save(out)
        if hasattr(result, "all_configs") and result.all_configs:
            c = result.all_configs[0]
            print(f"[{elapsed}s] CAGR {c['cagr']*100:>+6.2f}% MDD {c['max_drawdown']*100:>+6.2f}% Cal {c['calmar_ratio']:.3f} ({c['total_trades']} trades)")
        return out
    except Exception as e:
        print(f"FAILED: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--champion-config", required=True)
    args = ap.parse_args()

    os.makedirs(f"results/{args.strategy}", exist_ok=True)
    print(f"=== R4d cross-exchange for {args.strategy} ===")

    for label, exchange, pt in EXCHANGES:
        run_one(args.strategy, args.champion_config, label, exchange, pt)


if __name__ == "__main__":
    main()

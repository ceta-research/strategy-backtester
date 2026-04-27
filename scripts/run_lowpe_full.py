#!/usr/bin/env python3
"""One-off: run low_pe champion config on full 2010-2026 window for N-leg ensemble."""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.pipeline import run_pipeline


def main():
    cfg = "strategies/low_pe/config_champion_full.yaml"
    out = "results/low_pe/champion_full.json"
    print(f"Running {cfg} ...")
    t0 = time.time()
    result = run_pipeline(cfg)
    elapsed = round(time.time() - t0, 1)
    result.save(out)
    print(f"Done in {elapsed}s -> {out}")
    if hasattr(result, "all_configs") and result.all_configs:
        c = result.all_configs[0]
        print(f"  CAGR: {c['cagr']:.4f} | vol: {c['annualized_volatility']:.4f} | "
              f"Sharpe: {c['sharpe_ratio']:.4f} | Cal: {c['calmar_ratio']:.4f} | "
              f"trades: {c['total_trades']}")


if __name__ == "__main__":
    main()

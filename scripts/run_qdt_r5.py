#!/usr/bin/env python3
"""QDT R5 overlay refit: max_hold × ppi × tsl sweep on champion baseline."""
import os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.pipeline import run_pipeline


def main():
    cfg = "strategies/quality_dip_tiered/config_round5_overlay.yaml"
    out = "results/quality_dip_tiered/round5_overlay.json"
    print(f"Running {cfg} ...")
    t0 = time.time()
    result = run_pipeline(cfg)
    elapsed = round(time.time() - t0, 1)
    result.save(out)
    print(f"Done in {elapsed}s -> {out}")
    if hasattr(result, "all_configs") and result.all_configs:
        # Sort by Calmar desc
        configs = sorted(result.all_configs, key=lambda c: c["calmar_ratio"] or 0, reverse=True)
        print(f"\nTop 10 by Calmar:")
        print(f"  {'config_id':10} {'CAGR':>7} {'MDD':>8} {'Cal':>6} {'Sharpe':>7} {'Trades':>7}")
        for c in configs[:10]:
            params = c["params"]
            print(f"  {params.get('config_id','?'):10} {c['cagr']*100:>6.2f}% {c['max_drawdown']*100:>7.2f}% {c['calmar_ratio']:>5.3f} {c['sharpe_ratio']:>6.3f} {c['total_trades']:>7}")


if __name__ == "__main__":
    main()

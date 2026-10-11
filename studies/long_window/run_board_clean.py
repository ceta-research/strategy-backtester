#!/usr/bin/env python3
"""Run every distinct board config on the repaired snapshot, its own window, engine defaults.

Usage: run_board_clean.py --shard i/n | <key> ...   -> out/board/<key>.json + out/board_summary.jsonl
"""
import json
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, HERE]
from engine.pipeline import run_pipeline  # noqa: E402
from providers import SnapshotChartingProvider  # noqa: E402

CONFIGS = {  # key: (label, config) in LEADERBOARD.md row order; row 8 is a duplicate of row 1
    "eod_breakout_irhyst": ("eod_breakout IR-hyst", "strategies/eod_breakout/config_ir_hyst_best.yaml"),
    "eod_breakout_irnomh": ("eod_breakout IR-no-min-hold", "strategies/eod_breakout/config_ir_no_min_hold.yaml"),
    "low_pe": ("low_pe", "strategies/low_pe/config_champion.yaml"),
    "eod_breakout_irsma50": ("eod_breakout IR-SMA50", "strategies/eod_breakout/config_ir_best.yaml"),
    "eod_technical_atsl": ("eod_technical adaptive-TSL", "strategies/eod_technical/config_adaptive_tsl_30_3.yaml"),
    "eod_technical": ("eod_technical", "strategies/eod_technical/config_champion.yaml"),
    "eod_breakout_nods": ("eod_breakout noDS-regime", "strategies/eod_breakout/config_no_ds_regime.yaml"),
    "enhanced_breakout": ("enhanced_breakout", "strategies/enhanced_breakout/config_champion.yaml"),
    "trending_value": ("trending_value", "strategies/trending_value/config_champion.yaml"),
    "quality_dip_tiered": ("quality_dip_tiered", "strategies/quality_dip_tiered/config_champion.yaml"),
    "forced_selling_dip": ("forced_selling_dip", "strategies/forced_selling_dip/config_champion.yaml"),
    "ml_supertrend": ("ml_supertrend", "strategies/ml_supertrend/config_champion.yaml"),
    "momentum_top_gainers": ("momentum_top_gainers", "strategies/momentum_top_gainers/config_champion.yaml"),
    "factor_composite": ("factor_composite", "strategies/factor_composite/config_champion.yaml"),
    "quality_dip_buy": ("quality_dip_buy", "strategies/quality_dip_buy/config_champion.yaml"),
    "momentum_cascade": ("momentum_cascade", "strategies/momentum_cascade/config_champion.yaml"),
}
OUT = os.path.join(HERE, "out", "board")


def run(key):
    label, cfg = CONFIGS[key]
    os.makedirs(OUT, exist_ok=True)
    st = yaml.safe_load(open(os.path.join(ROOT, cfg)))["static"]
    t0 = time.time()
    sweep = run_pipeline(os.path.join(ROOT, cfg), data_provider=SnapshotChartingProvider(ca=True, clean=True))
    p = os.path.join(OUT, f"{key}.json")
    sweep.save(p)
    det = json.load(open(p))["detailed"][0]
    s = det["summary"]
    row = {"key": key, "label": label, "config": cfg, "start": st["start_epoch"], "end": st["end_epoch"],
           "provider": st.get("data_provider"), "cagr": s["cagr"], "mdd": s["max_drawdown"],
           "calmar": s["calmar_ratio"], "sharpe": s["sharpe_ratio"], "trades": s["total_trades"],
           "yearly": {str(y["year"]): y["return"] for y in det["yearly_returns"]}, "secs": round(time.time() - t0)}
    with open(os.path.join(HERE, "out", "board_summary.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"### {key}: CAGR {s['cagr']*100:.2f}% MDD {s['max_drawdown']*100:.2f}% Calmar {s['calmar_ratio']:.2f} "
          f"trades {s['total_trades']} ({row['secs']}s)", flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--shard":
        i, n = map(int, a[1].split("/"))
        keys = [k for k in CONFIGS if k not in a[2:]][i::n]
    else:
        keys = a
    for k in keys:
        run(k)

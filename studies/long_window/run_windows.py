#!/usr/bin/env python3
"""Re-run the board's top momentum/breakout configs over 10/15/20-year windows to the latest bar.

Usage: run_windows.py <strategy_key> [window_label ...]
Writes out/<key>__<window>.json (full sweep) and appends one row per run to out/summary.jsonl.
"""
import copy
import json
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from engine.pipeline import run_pipeline  # noqa: E402
from providers import SnapshotChartingProvider  # noqa: E402

OUT = os.path.join(HERE, "out")

STRATS = {
    "eod_technical_atsl": "strategies/eod_technical/config_adaptive_tsl_30_3.yaml",
    "momentum_cascade": "strategies/momentum_cascade/config_champion.yaml",
    "eod_breakout_irhyst": "strategies/eod_breakout/config_ir_hyst_best.yaml",
    "enhanced_breakout": "strategies/enhanced_breakout/config_champion.yaml",
}

Y2006, Y2010, Y2011, Y2016 = 1136073600, 1262304000, 1293840000, 1451606400
END_2010 = 1293839999            # 2010-12-31 23:59:59
LATEST = 1791504000              # 2026-10-09, last bar in the snapshot

# label: (start, end, ca). start/end None = keep the config's own value.
WINDOWS = {
    "repro": (None, None, False),
    "2010_now_raw": (Y2010, LATEST, False),
    "2010_now_ca": (Y2010, LATEST, True),
    "15y_raw": (Y2011, LATEST, False),
    "15y_ca": (Y2011, LATEST, True),
    "10y_raw": (Y2016, LATEST, False),
    "10y_ca": (Y2016, LATEST, True),
    "20y_raw": (Y2006, LATEST, False),
    "20y_ca": (Y2006, LATEST, True),
    "oos_2006_2010_raw": (Y2006, END_2010, False),
    "oos_2006_2010_ca": (Y2006, END_2010, True),
    # *_clean = ca plus drop_interleaved (TATASTEEL/BFUTILITIE stray series).
    "repro_clean": (None, None, True),
    "2010_now_clean": (Y2010, LATEST, True),
    "15y_clean": (Y2011, LATEST, True),
    "10y_clean": (Y2016, LATEST, True),
    "20y_clean": (Y2006, LATEST, True),
    "oos_2006_2010_clean": (Y2006, END_2010, True),
}


def run_one(key, label):
    start, end, ca = WINDOWS[label]
    with open(os.path.join(ROOT, STRATS[key])) as f:
        cfg = yaml.safe_load(f)
    cfg = copy.deepcopy(cfg)
    if start is not None:
        cfg["static"]["start_epoch"] = start
        cfg["static"]["end_epoch"] = end
    os.makedirs(os.path.join(OUT, "configs"), exist_ok=True)
    cfg_path = os.path.join(OUT, "configs", f"{key}__{label}.yaml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    t0 = time.time()
    provider = SnapshotChartingProvider(ca=ca, clean=label.endswith("_clean"))
    sweep = run_pipeline(cfg_path, data_provider=provider)
    res_path = os.path.join(OUT, f"{key}__{label}.json")
    sweep.save(res_path)
    with open(res_path) as f:
        det = json.load(f)["detailed"][0]
    s = det["summary"]
    row = {
        "strategy": key, "window": label, "ca": ca,
        "start": cfg["static"]["start_epoch"], "end": cfg["static"]["end_epoch"],
        "cagr": s["cagr"], "mdd": s["max_drawdown"], "calmar": s["calmar_ratio"],
        "sharpe": s["sharpe_ratio"], "trades": s["total_trades"],
        "ca_events": None if provider.events is None else provider.events.height,
        "yearly": {str(y["year"]): y["return"] for y in det["yearly_returns"]},
        "secs": round(time.time() - t0, 1),
    }
    with open(os.path.join(OUT, "summary.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"### {key} {label}: CAGR {s['cagr']*100:.2f}% MDD {s['max_drawdown']*100:.2f}% "
          f"Calmar {s['calmar_ratio']:.2f} trades {s['total_trades']} ({row['secs']}s)", flush=True)


if __name__ == "__main__":
    key = sys.argv[1]
    labels = sys.argv[2:] or list(WINDOWS)
    for label in labels:
        run_one(key, label)

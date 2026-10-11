#!/usr/bin/env python3
"""Run named momentum_cascade audit variants on the repaired snapshot.

Usage: run_audit.py <variant> [<variant> ...] | --shard i/n | --list
Writes out/<variant>.json and appends to out/summary.jsonl.
"""
import copy
import json
import os
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LW = os.path.join(os.path.dirname(HERE), "long_window")
sys.path[:0] = [ROOT, HERE, LW]

import mc_variant  # noqa: E402  (registers momentum_cascade_audit, patches the stats builder)
from engine.pipeline import run_pipeline  # noqa: E402
from providers import SnapshotChartingProvider  # noqa: E402

OUT = os.path.join(HERE, "out")
BASE = os.path.join(ROOT, "strategies/momentum_cascade/config_champion.yaml")
Y = {y: int(__import__("datetime").datetime(y, 1, 1, tzinfo=__import__("datetime").timezone.utc).timestamp())
     for y in range(2005, 2020)}
LATEST = 1791504000  # 2026-10-09
NOW10 = (Y[2010], LATEST)
NOW20 = (Y[2006], LATEST)


def v(window=NOW10, engine=False, knobs=None, static=None, entry=None, exit=None, sim=None, log=False):
    return dict(window=window, engine=engine, knobs=knobs or {}, static=static or {}, entry=entry or {},
                exit=exit or {}, sim=sim or {}, log=log)


LIVE = dict(knobs={"exit_fill": "next_open", "lag_liquidity": True},
            static={"start_margin": 5000000, "slippage_rate": 0.0015})

VARIANTS = {
    # Gate: real engine generator vs the audit copy at defaults.
    "gate_engine": v(engine=True),
    "gate_variant": v(log=True),
    # L1 exit fill timing.
    "L1_next_open": v(knobs={"exit_fill": "next_open"}, log=True),
    "L1_next_open_20y": v(window=NOW20, knobs={"exit_fill": "next_open"}),
    # L3a liquidity cap from the previous bar.
    "L3_lag_liq": v(knobs={"lag_liquidity": True}),
    # L4 ranking tie-order.
    **{f"L4_shuffle_{s}": v(knobs={"shuffle_seed": s}) for s in (1, 2, 3)},
    # L5 costs and capacity.
    "L5_slip15": v(static={"slippage_rate": 0.0015}),
    "L5_slip30": v(static={"slippage_rate": 0.0030}),
    "L5_cap50L": v(static={"start_margin": 5000000}),
    "L5_cap5cr": v(static={"start_margin": 50000000}),
    "L5_cap25cr": v(static={"start_margin": 250000000}),
    # L6 one-at-a-time neighbours (champion value in the comment).
    "L6_fast30": v(entry={"fast_lookback_days": [30]}),            # 42
    "L6_fast63": v(entry={"fast_lookback_days": [63]}),
    "L6_slow30": v(entry={"slow_lookback_days": [30]}),            # 42
    "L6_slow63": v(entry={"slow_lookback_days": [63]}),
    "L6_accel10": v(entry={"accel_threshold_pct": [10]}),          # 15
    "L6_accel20": v(entry={"accel_threshold_pct": [20]}),
    "L6_mom15": v(entry={"min_momentum_pct": [15]}),               # 20
    "L6_mom25": v(entry={"min_momentum_pct": [25]}),
    "L6_brk189": v(entry={"breakout_window": [189]}),              # 252
    "L6_brk315": v(entry={"breakout_window": [315]}),
    "L6_sma150": v(entry={"regime_sma_period": [150]}),            # 200
    "L6_sma250": v(entry={"regime_sma_period": [250]}),
    "L6_noregime": v(entry={"regime_sma_period": [0]}),
    "L6_tsl12": v(exit={"trailing_stop_pct": [12]}),               # 15
    "L6_tsl18": v(exit={"trailing_stop_pct": [18]}),
    "L6_hold252": v(exit={"max_hold_days": [252]}),                # 378
    "L6_hold504": v(exit={"max_hold_days": [504]}),
    "L6_rank180": v(sim={"order_ranking_window_days": [180]}),     # 360
    "L6_rank540": v(sim={"order_ranking_window_days": [540]}),
    "L6_pos10": v(sim={"max_positions": [10]}),                    # 15
    "L6_pos20": v(sim={"max_positions": [20]}),
    # L7 start-year sweep, all to the latest bar.
    **{f"L7_start{y}": v(window=(Y[y], LATEST)) for y in range(2006, 2019)},
    # L9 live-realistic combined run (the planning number).
    "L9_live": v(log=True, **LIVE),
    "L9_live_20y": v(window=NOW20, log=True, **LIVE),
    # L1b: entries-first ordering means a next-open exit frees its slot a day late (250/554
    # trades reroute). Live, you sell at the open and buy at the open, so settle exits first.
    "L1b_close_ebe": v(sim={"exit_before_entry": [True]}),
    "L1b_next_open_ebe": v(knobs={"exit_fill": "next_open"}, sim={"exit_before_entry": [True]}, log=True),
    "L1b_next_open_ebe_20y": v(window=NOW20, knobs={"exit_fill": "next_open"},
                               sim={"exit_before_entry": [True]}),
    "L9b_live_ebe": v(log=True, sim={"exit_before_entry": [True]}, **LIVE),
    "L9b_live_ebe_20y": v(window=NOW20, log=True, sim={"exit_before_entry": [True]}, **LIVE),
    # L10 path dependence: same window, start shifted 0..27 days. Signals are identical; only
    # which trades win the 15 slots changes (exits-first alone moved CAGR 18.3 -> 15.2).
    **{f"L10_base_d{d:02d}": v(window=(Y[2010] + d * 86400, LATEST)) for d in range(0, 30, 3)},
    **{f"L10_live_d{d:02d}": v(window=(Y[2010] + d * 86400, LATEST), sim={"exit_before_entry": [True]}, **LIVE)
       for d in range(0, 30, 3)},
}


def run(name):
    spec = VARIANTS[name]
    with open(BASE) as f:
        cfg = copy.deepcopy(yaml.safe_load(f))
    cfg["static"]["start_epoch"], cfg["static"]["end_epoch"] = spec["window"]
    if not spec["engine"]:
        cfg["static"]["strategy_type"] = "momentum_cascade_audit"
    for sec, key in (("static", "static"), ("entry", "entry"), ("exit", "exit"), ("simulation", "sim")):
        cfg[sec].update(spec[key])
    os.makedirs(os.path.join(OUT, "configs"), exist_ok=True)
    cfg_path = os.path.join(OUT, "configs", f"{name}.yaml")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    mc_variant.KNOBS.update({"exit_fill": "close", "shuffle_seed": None, "order_log": None,
                             "lag_liquidity": False})
    mc_variant.KNOBS.update(spec["knobs"])
    if spec["log"]:
        mc_variant.KNOBS["order_log"] = os.path.join(OUT, f"{name}.orders.json")

    t0 = time.time()
    sweep = run_pipeline(cfg_path, data_provider=SnapshotChartingProvider(ca=True, clean=True))
    res = os.path.join(OUT, f"{name}.json")
    sweep.save(res)
    det = json.load(open(res))["detailed"][0]
    s = det["summary"]
    row = {"variant": name, "window": spec["window"], "knobs": spec["knobs"], "static": spec["static"],
           "entry": spec["entry"], "exit": spec["exit"], "sim": spec["sim"],
           "cagr": s["cagr"], "mdd": s["max_drawdown"], "calmar": s["calmar_ratio"],
           "sharpe": s["sharpe_ratio"], "trades": s["total_trades"],
           "dd_days": s["max_dd_duration_periods"], "avg_hold": s.get("avg_hold_days"),
           "yearly": {str(y["year"]): y["return"] for y in det["yearly_returns"]},
           "secs": round(time.time() - t0, 1)}
    with open(os.path.join(OUT, "summary.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"### {name}: CAGR {s['cagr']*100:.2f}% MDD {s['max_drawdown']*100:.2f}% "
          f"Calmar {s['calmar_ratio']:.2f} trades {s['total_trades']} ({row['secs']}s)", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--list"]:
        print("\n".join(VARIANTS))
        sys.exit()
    if args and args[0] == "--shard":
        i, n = map(int, args[1].split("/"))
        skip = set(args[2:])
        names = [k for k in VARIANTS if k not in skip][i::n]
    else:
        names = args
    for name in names:
        run(name)

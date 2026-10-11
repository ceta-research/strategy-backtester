#!/usr/bin/env python3
"""Consolidated results table for every strategy on the board.

Columns: board as published (pre ranking fix, raw charting) | after the 10-09 ranking fix
(raw charting) | repaired data (same window, engine defaults) | NIFTYBEES on that window.
Sources: docs/leaderboard_rerun_2026-10-09.tsv, out/board_summary.jsonl (run_board_clean.py),
out/summary.jsonl (repro_clean rows for the four long-window strategies), the snapshot for
NIFTYBEES. Writes BOARD_2026-10-11.md next to this script.
"""
import datetime as dt
import json
import os

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
import sys  # noqa: E402
sys.path.insert(0, HERE)
import providers as P  # noqa: E402

TYPES = {"eod_breakout": "breakout", "eod_technical": "momentum/breakout", "enhanced_breakout": "breakout",
         "momentum_cascade": "momentum", "momentum_top_gainers": "momentum", "ml_supertrend": "trend",
         "low_pe": "value", "trending_value": "value+momentum", "factor_composite": "multi-factor",
         "quality_dip_tiered": "dip-buy", "quality_dip_buy": "dip-buy", "forced_selling_dip": "dip-buy",
         "earnings_dip": "dip-buy"}
TSV_KEY = {"eod_breakout IR-hyst": "eod_breakout_irhyst", "eod_breakout IR-nomh": "eod_breakout_irnomh",
           "low_pe": "low_pe", "eod_breakout IR-SMA50": "eod_breakout_irsma50",
           "eod_technical adaptiveTSL": "eod_technical_atsl", "eod_technical": "eod_technical",
           "eod_breakout noDS-regime": "eod_breakout_nods", "enhanced_breakout": "enhanced_breakout",
           "trending_value": "trending_value", "quality_dip_tiered": "quality_dip_tiered",
           "forced_selling_dip": "forced_selling_dip", "ml_supertrend": "ml_supertrend",
           "momentum_top_gainers": "momentum_top_gainers", "factor_composite": "factor_composite",
           "quality_dip_buy": "quality_dip_buy", "momentum_cascade": "momentum_cascade"}

tsv = {}
for ln in open(os.path.join(ROOT, "docs", "leaderboard_rerun_2026-10-09.tsv")):
    rank, name, oc, om, nc, nm = ln.rstrip("\n").split("\t")
    if name in TSV_KEY:
        tsv[TSV_KEY[name]] = (float(oc), float(om), float(nc), float(nm))

clean = {}
for ln in open(os.path.join(HERE, "out", "board_summary.jsonl")):
    r = json.loads(ln)
    clean[r["key"]] = r
lw = {}
for ln in open(os.path.join(HERE, "out", "summary.jsonl")):
    r = json.loads(ln)
    if r["window"] == "repro_clean":
        lw[r["strategy"]] = r
for k in ("eod_breakout_irhyst", "eod_technical_atsl", "enhanced_breakout", "momentum_cascade"):
    if k not in clean and k in lw:
        clean[k] = {"cagr": lw[k]["cagr"], "mdd": lw[k]["mdd"], "calmar": lw[k]["calmar"],
                    "trades": lw[k]["trades"], "start": lw[k]["start"], "end": lw[k]["end"]}

nb = pl.read_parquet(P.CHARTING).filter(pl.col("symbol") == "NIFTYBEES").sort("date_epoch")


def bench(s, e):
    w = nb.filter(pl.col("date_epoch").is_between(s, e))
    c = w["close"]
    yrs = (w["date_epoch"][-1] - w["date_epoch"][0]) / 86400 / 365.25
    cagr = (c[-1] / c[0]) ** (1 / yrs) - 1
    return cagr, (c / c.cum_max() - 1).min()


def d(e):
    return dt.datetime.fromtimestamp(e, dt.timezone.utc).strftime("%Y-%m")


order = ["momentum_cascade", "eod_technical_atsl", "eod_technical", "eod_breakout_irhyst", "eod_breakout_irsma50",
         "eod_breakout_irnomh", "eod_breakout_nods", "enhanced_breakout", "quality_dip_tiered", "trending_value",
         "ml_supertrend", "factor_composite", "low_pe", "quality_dip_buy", "momentum_top_gainers", "forced_selling_dip"]
rows = []
for k in order:
    c = clean.get(k)
    rows.append((k, c["cagr"] if c else -9))
rows.sort(key=lambda x: -x[1])

lines = ["| strategy | type | window | board as published | after ranking fix (raw data) | **repaired data** | NIFTYBEES same window | repaired vs index |",
         "|---|---|---|---|---|---|---|---|"]
for k, _ in rows:
    c, t = clean.get(k), tsv.get(k)
    label = next(v[0] for kk, v in __import__("run_board_clean").CONFIGS.items() if kk == k)
    typ = TYPES[k.split("_ir")[0].split("_nods")[0].split("_atsl")[0]] if k not in TYPES else TYPES[k]
    win = f"{d(c['start'])} → {d(c['end'])}" if c else "?"
    b = bench(c["start"], c["end"]) if c else None
    lines.append(
        f"| {label} | {typ} | {win} | "
        + (f"{t[0]:.1f}% / {t[1]:.0f}% | {t[2]:.1f}% / {t[3]:.0f}% | " if t else "— | — | ")
        + (f"**{c['cagr']*100:.1f}% / {c['mdd']*100:.0f}%** (Calmar {c['calmar']:.2f}, {c['trades']} trades) | "
           f"{b[0]*100:.1f}% / {b[1]*100:.0f}% | **{(c['cagr']-b[0])*100:+.1f}pp** |" if c else "(not run) | | |"))
md = "\n".join(lines)
print(md)
with open(os.path.join(HERE, "BOARD_2026-10-11.md"), "w") as f:
    f.write("# Strategy board on repaired data (2026-10-11)\n\n"
            "CAGR / max drawdown. 'Board as published' = LEADERBOARD.md (before the 2026-10-09 ranking fix, raw "
            "charting). 'After ranking fix' = docs/leaderboard_rerun_2026-10-09.tsv (raw charting). 'Repaired data' = "
            "the same config and window on the repaired snapshot (B045–B047 fixed), engine defaults, run by "
            "run_board_clean.py. NIFTYBEES is price-only here, i.e. it reinvests dividends (≈ total return), while "
            "the strategies' legs are price-only, so the index column is flattered by roughly its yield.\n\n" + md + "\n")

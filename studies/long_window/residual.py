#!/usr/bin/env python3
"""Residual corporate-action exposure: trades held across a >=30% one-bar close drop.

A drop that size inside a holding window is either a real crash or an unadjusted
split/bonus the calendar missed. Prints both raw- and CA-run counts so the
adjustment's reach is visible, and lists the CA-run survivors for eyeballing.
"""
import glob
import json
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import providers as P  # noqa: E402

df = pl.read_parquet(P.CHARTING)
ev = P.detect_raw_events(df)
adj = P.back_adjust(df, ev)
cln, _ = P.drop_interleaved(adj)


def drops(frame):
    return (frame.sort(["symbol", "date_epoch"])
                 .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1)
                               .alias("r"))
                 .filter(pl.col("r") <= -0.30)
                 .select([("NSE:" + pl.col("symbol")).alias("symbol"), "date_epoch", "r"]))


dr = {"raw": drops(df), "ca": drops(adj), "clean": drops(cln)}
for path in sorted(glob.glob(os.path.join(HERE, "out", "*__*.json"))):
    name = os.path.basename(path)[:-5]
    mode = name.rsplit("_", 1)[-1] if name.endswith(("_ca", "_clean")) else "raw"
    t = pl.DataFrame(json.load(open(path))["detailed"][0]["trades"])
    hit = (t.join(dr[mode], on="symbol")
            .filter((pl.col("date_epoch") > pl.col("entry_epoch"))
                    & (pl.col("date_epoch") <= pl.col("exit_epoch"))))
    print(f"{name:<45} trades {t.height:5d}  across >=30% drop: {hit.height:3d}  "
          f"net pnl of those {hit['net_pnl'].sum() / 1e5 if hit.height else 0:9.1f} L")
    if mode == "clean" and hit.height:
        for r in hit.select(["symbol", "entry_date", "exit_date", "pnl_pct", "r"]).rows():
            print(f"      {r[0]:<18} {r[1]} -> {r[2]}  trade {r[3]:7.1f}%  bar {r[4]*100:6.1f}%")

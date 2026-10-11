#!/usr/bin/env python3
"""NIFTYBEES per window, and how many names clear the scanner's nominal liquidity bar each year."""
import json
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import providers as P  # noqa: E402
from run_windows import WINDOWS  # noqa: E402

df = pl.read_parquet(P.CHARTING)
ev = P.detect_raw_events(df)
print("NIFTYBEES raw events:", ev.filter(pl.col("symbol") == "NIFTYBEES").height)
adj = P.back_adjust(df, ev)

nb = adj.filter(pl.col("symbol") == "NIFTYBEES").sort("date_epoch")
out = {}
for label, (start, end, _) in WINDOWS.items():
    if start is None or label.endswith("_raw"):
        continue
    w = nb.filter((pl.col("date_epoch") >= start) & (pl.col("date_epoch") <= end))
    c = w["close"]
    days = (w["date_epoch"][-1] - w["date_epoch"][0]) / 86400
    cagr = (c[-1] / c[0]) ** (365.25 / days) - 1
    mdd = (c / c.cum_max() - 1).min()
    out[label.replace("_ca", "")] = {"cagr": cagr, "mdd": mdd, "calmar": cagr / -mdd}
    print(f"NIFTYBEES {label:<20} CAGR {cagr*100:6.2f}%  MDD {mdd*100:7.2f}%  Calmar {cagr/-mdd:.2f}")

yr = (nb.with_columns(pl.from_epoch("date_epoch").dt.year().alias("y"))
        .group_by("y").agg(pl.col("close").first().alias("o"), pl.col("close").last().alias("c"))
        .sort("y"))
# Year return = last close of year / last close of previous year.
yr = yr.with_columns((pl.col("c") / pl.col("c").shift(1) - 1).alias("ret"))
out["yearly"] = {str(y): r for y, r in zip(yr["y"], yr["ret"]) if r is not None}

# Universe: names with price > 50 and 125-bar avg turnover > 7 cr on at least one day of the year.
u = (adj.sort(["symbol", "date_epoch"])
        .with_columns((pl.col("volume") * pl.col("average_price")).rolling_mean(125, min_samples=1)
                      .over("symbol").alias("tv"),
                      pl.from_epoch("date_epoch").dt.year().alias("y"))
        .filter((pl.col("close") > 50) & (pl.col("tv") > 7e7) & (pl.col("y") >= 2004))
        .group_by("y").agg(pl.col("symbol").n_unique().alias("n")).sort("y"))
out["universe"] = {str(y): n for y, n in zip(u["y"], u["n"])}
print("universe per year:", out["universe"])
json.dump(out, open(os.path.join(HERE, "out", "bench.json"), "w"), indent=1)

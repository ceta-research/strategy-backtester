#!/usr/bin/env python3
"""What bhavcopy adds: the live config on a survivorship-free universe vs repaired charting.

Reads out/{BH,CH}_S0X0{,_20y}_mar26.json. For the bhav runs, splits trades into names that
charting has (survivors) and names it doesn't (delisted or otherwise absent), and lists
trades held across a >=30% one-bar drop (a split the calendar lacks, or a real collapse).
"""
import json
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE)), os.path.join(os.path.dirname(HERE), "long_window")]
import providers as P  # noqa: E402
from bhav_provider import CACHE  # noqa: E402

ROWS = {}
for ln in open(os.path.join(HERE, "out", "summary.jsonl")):
    r = json.loads(ln)
    ROWS[r["variant"]] = r
chart_syms = set("NSE:" + s for s in pl.read_parquet(P.CHARTING)["symbol"].unique().to_list())
from engine import charting_repairs as R  # noqa: E402
bhav = pl.read_parquet(CACHE)
bhav = R.back_adjust(bhav, R.detect_raw_events(bhav, pl.read_parquet(P.SPLITS)))   # as the engine saw it
bhav_last = bhav.group_by("symbol").agg(pl.col("date_epoch").max().alias("last")).to_dict(as_series=False)
bhav_last = dict(zip(bhav_last["symbol"], bhav_last["last"]))
drops = (bhav.sort(["symbol", "date_epoch"])
             .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("r"))
             .filter(pl.col("r") <= -0.30).select([("NSE:" + pl.col("symbol")).alias("symbol"), "date_epoch", "r"]))

print("| run | data | window | CAGR | max DD | Calmar | trades |\n|---|---|---|---|---|---|---|")
for w, lab in (("_mar26", "2010-01 → 2026-03"), ("_20y_mar26", "2006-01 → 2026-03")):
    for src, name in (("CH", "charting (repaired)"), ("BHS", "bhavcopy data, charting's symbols only"),
                      ("BH", "bhavcopy (repaired, incl. delisted)")):
        r = ROWS.get(f"{src}_S0X0{w}")
        if r:
            print(f"| {src} | {name} | {lab} | {r['cagr']*100:.2f}% | {r['mdd']*100:.1f}% | {r['calmar']:.2f} | {r['trades']} |")

for w in ("_mar26", "_20y_mar26"):
    v = f"BH_S0X0{w}"
    if v not in ROWS:
        continue
    t = pl.DataFrame(json.load(open(os.path.join(HERE, "out", f"{v}.json")))["detailed"][0]["trades"])
    tot = t["net_pnl"].sum()
    absent = t.filter(~pl.col("symbol").is_in(list(chart_syms)))
    gone = t.with_columns(pl.col("symbol").str.replace("NSE:", "").alias("s")).filter(
        pl.col("s").map_elements(lambda s: bhav_last.get(s, 0) < 1774396800 - 60 * 86400, return_dtype=pl.Boolean))
    print(f"\n{v}: {t.height} trades, net {tot/1e5:.0f} L")
    print(f"  in names charting does NOT have: {absent.height} trades, net {absent['net_pnl'].sum()/1e5:.1f} L "
          f"({absent['net_pnl'].sum()/tot*100:+.1f}% of P&L), mean {absent['pnl_pct'].mean() if absent.height else 0:.1f}%, "
          f"worst {absent['pnl_pct'].min() if absent.height else 0:.1f}%")
    print(f"  in names whose bhav history ends >60d before 2026-03 (delisted/renamed): {gone.height} trades, "
          f"net {gone['net_pnl'].sum()/1e5:.1f} L, mean {gone['pnl_pct'].mean() if gone.height else 0:.1f}%")
    if absent.height:
        print("  absent names:", sorted(set(absent["symbol"].str.replace("NSE:", "").to_list()))[:40])
    hit = (t.join(drops, on="symbol").filter((pl.col("date_epoch") > pl.col("entry_epoch"))
                                             & (pl.col("date_epoch") <= pl.col("exit_epoch"))))
    print(f"  trades held across a >=30% one-bar drop: {hit.height}, net {hit['net_pnl'].sum()/1e5 if hit.height else 0:.1f} L")
    if hit.height:
        for row in hit.sort("r").select(["symbol", "entry_date", "exit_date", "pnl_pct", "r"]).head(12).rows():
            print(f"     {row[0]:<18} {row[1]} -> {row[2]}  trade {row[3]:7.1f}%  bar {row[4]*100:6.1f}%")
    y = ROWS[v]["yearly"]
    cy = ROWS.get(f"CH_S0X0{w}", {}).get("yearly", {})
    print("  year  bhav   charting")
    for yr in sorted(y):
        print(f"  {yr}  {y[yr]*100:6.1f}  {cy.get(yr, float('nan'))*100:6.1f}")

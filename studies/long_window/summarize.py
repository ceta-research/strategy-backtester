#!/usr/bin/env python3
"""Print markdown tables from out/summary.jsonl + out/bench.json."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
rows = {}
for line in open(os.path.join(HERE, "out", "summary.jsonl")):
    r = json.loads(line)
    rows[(r["strategy"], r["window"])] = r          # last run wins
bench = json.load(open(os.path.join(HERE, "out", "bench.json")))

STRATS = ["momentum_cascade", "eod_technical_atsl", "eod_breakout_irhyst", "enhanced_breakout"]


def cell(r):
    return f"{r['cagr']*100:.1f}% / {r['mdd']*100:.0f}% / {r['calmar']:.2f}" if r else "—"


print("| strategy | board (repro) | 2010→now raw | 2010→now clean | 10y clean | 15y clean | "
      "20y clean | 20y raw | 2006-10 clean |")
print("|---|---|---|---|---|---|---|---|---|")
for s in STRATS:
    g = lambda w: rows.get((s, w))
    print(f"| {s} | {cell(g('repro'))} | {cell(g('2010_now_raw'))} | {cell(g('2010_now_clean'))} | "
          f"{cell(g('10y_clean'))} | {cell(g('15y_clean'))} | {cell(g('20y_clean'))} | "
          f"{cell(g('20y_raw'))} | {cell(g('oos_2006_2010_clean'))} |")
b = lambda w: f"{bench[w]['cagr']*100:.1f}% / {bench[w]['mdd']*100:.0f}% / {bench[w]['calmar']:.2f}"
print(f"| **NIFTYBEES** | — | {b('2010_now')} | {b('2010_now')} | {b('10y')} | {b('15y')} | "
      f"{b('20y')} | {b('20y')} | {b('oos_2006_2010')} |")

print("\nraw → ca → clean (CAGR, pp moves):")
for s in STRATS:
    for w in ["2010_now", "15y", "10y", "20y", "oos_2006_2010"]:
        a, c, k = (rows.get((s, f"{w}_{x}")) for x in ("raw", "ca", "clean"))
        if a and c and k:
            print(f"  {s:<22}{w:<15} raw {a['cagr']*100:6.2f}  ca {c['cagr']*100:6.2f}  "
                  f"clean {k['cagr']*100:6.2f}   trades {k['trades']}")

print("\nYear-by-year, 20y clean (%):")
years = sorted({y for s in STRATS for y in rows.get((s, "20y_clean"), {}).get("yearly", {})})
print("| year | " + " | ".join(STRATS) + " | NIFTYBEES | universe |")
print("|---|" + "---|" * (len(STRATS) + 2))
for y in years:
    vals = [rows.get((s, "20y_clean"), {}).get("yearly", {}).get(y) for s in STRATS]
    nb = bench["yearly"].get(y)
    print(f"| {y} | " + " | ".join("—" if v is None else f"{v*100:.1f}" for v in vals)
          + f" | {'—' if nb is None else f'{nb*100:.1f}'} | {bench['universe'].get(y, '—')} |")

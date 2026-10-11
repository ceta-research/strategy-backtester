#!/usr/bin/env python3
"""Apply PREREG.md's decision rules to the PR_* runs. Prints the table and the verdict."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
ROWS = {}
for line in open(os.path.join(OUT, "summary.jsonl")):
    r = json.loads(line)
    ROWS[r["variant"]] = r


def worst_trade(v):
    t = json.load(open(os.path.join(OUT, f"{v}.json")))["detailed"][0]["trades"]
    return min(x["pnl_pct"] for x in t)


def stats(v):
    r = ROWS[v]
    return {"cagr": r["cagr"], "mdd": r["mdd"], "calmar": r["calmar"], "trades": r["trades"], "worst": worst_trade(v)}


print("| arm | window | CAGR | max DD | Calmar | trades | worst trade |\n|---|---|---|---|---|---|---|")
S = {}
for arm in ("S0X0", "S1X0", "S2X0", "S0X1", "S1X1"):
    for w in ("", "_20y"):
        v = f"PR_{arm}{w}"
        if v not in ROWS:
            print(f"| {arm} | {w or '2010→'} | (missing) |")
            continue
        s = S[(arm, w)] = stats(v)
        print(f"| {arm} | {'20y' if w else '2010→'} | {s['cagr']*100:.2f}% | {s['mdd']*100:.1f}% | "
              f"{s['calmar']:.2f} | {s['trades']} | {s['worst']:.1f}% |")


def passes_stop(arm):
    ok = True
    for w in ("", "_20y"):
        b, a = S[("S0X0", w)], S[(arm, w)]
        ok &= a["mdd"] > b["mdd"] and a["worst"] > b["worst"] and (b["cagr"] - a["cagr"]) <= 0.010
    return ok


stop = "S2" if passes_stop("S2X0") else ("S1" if passes_stop("S1X0") else "S0")
print(f"\nRule 1 (stop): S2 passes = {passes_stop('S2X0')}, S1 passes = {passes_stop('S1X0')} -> adopt **{stop}**")

mech = None
if stop in ("S0", "S1"):
    ok = all(S[(f"{stop}X0", w)]["cagr"] - S[(f"{stop}X1", w)]["cagr"] <= 0.010 for w in ("", "_20y"))
    mech = "X1" if ok else "X0"
    print(f"Rule 2 (mechanics): {stop}X1 costs "
          + ", ".join(f"{(S[(f'{stop}X0', w)]['cagr'] - S[(f'{stop}X1', w)]['cagr'])*100:+.2f}pp ({'20y' if w else '2010→'})"
                      for w in ("", "_20y"))
          + f" -> **{mech}**")
else:
    mech = "X0"
    print("Rule 2 (mechanics): S2 has no X1 arm (not pre-registered) -> **X0** (next-open sells)")
print(f"\nconfig_live.yaml: stop_mode={'gtt' if mech == 'X1' else 'close'}, "
      f"trail_from_entry={stop == 'S1'}, initial_stop_pct={15 if stop == 'S2' else 0}")

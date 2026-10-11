#!/usr/bin/env python3
"""Print every table AUDIT.md cites, from out/ (run_audit.py results) and the clean snapshot."""
import datetime as dt
import json
import math
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
LW = os.path.join(os.path.dirname(HERE), "long_window")
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE)), LW]
import providers as P  # noqa: E402
from engine.data_provider import remove_price_oscillations, cast_ohlcv_dtypes  # noqa: E402

OUT = os.path.join(HERE, "out")
ROWS = {}
for line in open(os.path.join(OUT, "summary.jsonl")):
    r = json.loads(line)
    ROWS[r["variant"]] = r
BENCH = json.load(open(os.path.join(LW, "out", "bench.json")))


def det(v):
    return json.load(open(os.path.join(OUT, f"{v}.json")))["detailed"][0]


def trades(v):
    return pl.DataFrame(det(v)["trades"])


def line(v, label=None):
    r = ROWS.get(v)
    if not r:
        return f"| {label or v} | (missing) |"
    return (f"| {label or v} | {r['cagr']*100:.2f}% | {r['mdd']*100:.1f}% | {r['calmar']:.2f} | "
            f"{r['trades']} | {r['dd_days']} |")


HDR = "| variant | CAGR | max DD | Calmar | trades | longest underwater (days) |\n|---|---|---|---|---|---|"


def section(title):
    print(f"\n### {title}\n")


# ── Gate ────────────────────────────────────────────────────────────────────
section("Gate")
k = lambda t: (t["symbol"], t["entry_epoch"], t["exit_epoch"], t["entry_price"], t["exit_price"], t["quantity"])
a, b = det("gate_engine"), det("gate_variant")
print(f"engine vs audit copy: trades identical = {sorted(map(k, a['trades'])) == sorted(map(k, b['trades']))}, "
      f"equity identical = {a['equity_curve'] == b['equity_curve']}")
print(HDR)
print(line("gate_engine", "baseline (engine)"))

# ── L1 exit timing ──────────────────────────────────────────────────────────
section("L1 exit fill timing (2010 → now)")
print(HDR)
for v, lab in [("gate_variant", "close fill, entries first (engine)"),
               ("L1_next_open", "next-open fill, entries first"),
               ("L1b_close_ebe", "close fill, exits first"),
               ("L1b_next_open_ebe", "next-open fill, exits first (fair)"),
               ("L1_next_open_20y", "next-open fill, entries first, 20y"),
               ("L1b_next_open_ebe_20y", "next-open fill, exits first, 20y")]:
    print(line(v, lab))
base, nxt = trades("gate_variant"), trades("L1b_next_open_ebe")
j = base.join(nxt, on=["symbol", "entry_epoch"], how="inner", suffix="_n")
chg = (j["exit_price_n"] / j["exit_price"] - 1)
print(f"\nshared trades {j.height} of {base.height}; exit price next open vs trigger close: "
      f"mean {chg.mean()*100:+.3f}%, median {chg.median()*100:+.3f}%, p05 {chg.quantile(0.05)*100:+.2f}%, "
      f"p95 {chg.quantile(0.95)*100:+.2f}%")

# ── Armed share ─────────────────────────────────────────────────────────────
section("Trades that never armed their stop (baseline)")
olog = pl.DataFrame(json.load(open(os.path.join(OUT, "gate_variant.orders.json"))))
tb = base.join(olog.rename({"instrument": "symbol"}).select(["symbol", "entry_epoch", "armed", "reason"]),
               on=["symbol", "entry_epoch"], how="left")
tot = tb["net_pnl"].sum()
for (armed,), g in tb.group_by(["armed"], maintain_order=True):
    print(f"armed={armed}: {g.height} trades, net {g['net_pnl'].sum()/1e5:.1f} L "
          f"({g['net_pnl'].sum()/tot*100:.1f}% of P&L), mean {g['pnl_pct'].mean():.1f}%, "
          f"worst {g['pnl_pct'].min():.1f}%")
print("exit reasons:", tb.group_by("reason").agg(pl.len(), (pl.col("net_pnl").sum() / 1e5).round(1).alias("net_L"))
      .sort("reason").rows())

# ── L2 is printed by l2_fills.py ────────────────────────────────────────────

# ── L3 look-ahead ───────────────────────────────────────────────────────────
section("L3 look-ahead")
print(HDR)
print(line("gate_variant", "baseline"))
print(line("L3_lag_liq", "liquidity cap from previous bar"))
raw = pl.read_parquet(P.CHARTING)
clean, _ = P.drop_interleaved(P.back_adjust(raw, P.detect_raw_events(raw)))
clean, _ = P.split_stitches(clean)
f = cast_ohlcv_dtypes(clean.filter(pl.col("date_epoch") >= 1262304000 - 600 * 86400)).with_columns(
    (pl.lit("NSE:") + pl.col("symbol")).alias("instrument"))
kept = remove_price_oscillations(f, verbose=False)
removed = f.join(kept.select(["instrument", "date_epoch"]), on=["instrument", "date_epoch"], how="anti")
liq = (f.sort(["instrument", "date_epoch"]).with_columns(
    (pl.col("volume") * pl.col("average_price")).rolling_mean(125, min_samples=1).over("instrument").alias("tv"))
    .select(["instrument", "date_epoch", "tv"]))
rl = removed.join(liq, on=["instrument", "date_epoch"]).filter(pl.col("tv") > 7e7)
held = removed.join(base.select([pl.col("symbol").alias("instrument"), "entry_epoch", "exit_epoch"]), on="instrument") \
              .filter(pl.col("date_epoch").is_between(pl.col("entry_epoch"), pl.col("exit_epoch")))
print(f"\noscillation filter (non-causal) removed {removed.height:,} bars 2008+; {rl.height:,} on liquid "
      f"symbol-days; {held.height} inside a held baseline trade "
      f"({held['instrument'].n_unique()} trades' symbols)")

# ── L4 determinism + short-history cohort ───────────────────────────────────
section("L4 determinism")
for s in (1, 2, 3):
    v = f"L4_shuffle_{s}"
    if v in ROWS:
        same = sorted(map(k, det(v)["trades"])) == sorted(map(k, b["trades"]))
        print(f"{v}: trades identical to baseline = {same}")
bars = (clean.select([("NSE:" + pl.col("symbol")).alias("symbol"), "date_epoch"])
        .join(base.select(["symbol", "entry_epoch"]), on="symbol")
        .filter(pl.col("date_epoch") < pl.col("entry_epoch")).group_by(["symbol", "entry_epoch"]).len())
cohort = base.join(bars, on=["symbol", "entry_epoch"]).filter(pl.col("len").is_between(262, 361))
print(f"trades in names with 262-361 bars of history (eligible, null-ranked): {cohort.height}, "
      f"net {cohort['net_pnl'].sum()/1e5:.1f} L")

# ── L5 costs and capacity ───────────────────────────────────────────────────
section("L5 costs and capacity (2010 → now)")
print(HDR)
for v, lab in [("gate_variant", "₹1 cr, 5 bps (baseline)"), ("L5_slip15", "15 bps"), ("L5_slip30", "30 bps"),
               ("L5_cap50L", "₹50 L"), ("L5_cap5cr", "₹5 cr"), ("L5_cap25cr", "₹25 cr")]:
    print(line(v, lab))

# ── L6 parameter neighbours ─────────────────────────────────────────────────
section("L6 one-at-a-time neighbours (2010 → now)")
print(HDR)
print(line("gate_variant", "champion"))
l6 = [v for v in ROWS if v.startswith("L6_")]
for v in l6:
    print(line(v))
cs = sorted(ROWS[v]["cagr"] for v in l6 if v != "L6_noregime")
print(f"\nneighbour CAGR (excl. no-regime): median {cs[len(cs)//2]*100:.2f}%, min {cs[0]*100:.2f}%, "
      f"max {cs[-1]*100:.2f}%; within 3pp of champion: "
      f"{sum(abs(c - ROWS['gate_variant']['cagr']) <= 0.03 for c in cs)}/{len(cs)}")

# ── L7 timing and concentration ─────────────────────────────────────────────
section("L7 start-year sweep (to 2026-10-09)")
print("| start | CAGR | max DD | Calmar | longest underwater (days) | NIFTYBEES CAGR |\n|---|---|---|---|---|---|")
nb = pl.read_parquet(P.CHARTING).filter(pl.col("symbol") == "NIFTYBEES").sort("date_epoch")
for y in range(2006, 2019):
    v = f"L7_start{y}"
    if v not in ROWS:
        continue
    r = ROWS[v]
    w = nb.filter(pl.col("date_epoch") >= r["window"][0])
    yrs = (w["date_epoch"][-1] - w["date_epoch"][0]) / 86400 / 365.25
    nbc = (w["close"][-1] / w["close"][0]) ** (1 / yrs) - 1
    print(f"| {y} | {r['cagr']*100:.2f}% | {r['mdd']*100:.1f}% | {r['calmar']:.2f} | {r['dd_days']} | {nbc*100:.2f}% |")

section("L7 concentration and exposure (baseline)")
srt = base.sort("net_pnl", descending=True)
for n in (5, 10, 20):
    print(f"top {n} trades = {srt.head(n)['net_pnl'].sum()/tot*100:.1f}% of net P&L")
y = ROWS["gate_variant"]["yearly"]
yrs = (ROWS["gate_variant"]["window"][1] - ROWS["gate_variant"]["window"][0]) / 86400 / 365.25
prod = math.prod(1 + x for x in y.values())
print(f"ex-2021 CAGR: {((prod / (1 + y['2021'])) ** (1 / (yrs - 1)) - 1)*100:.2f}% (2021 = {y['2021']*100:.1f}%)")
days = pl.DataFrame({"d": pl.date_range(dt.date(2010, 1, 1), dt.date(2026, 10, 9), "1d", eager=True)})
iv = base.select(pl.from_epoch("entry_epoch").dt.date().alias("a"), pl.from_epoch("exit_epoch").dt.date().alias("b"))
occ = days.join(iv, how="cross").filter(pl.col("d").is_between(pl.col("a"), pl.col("b"))).group_by("d").len()
print(f"days with >=1 position: {occ.height/days.height*100:.1f}%; mean positions held: "
      f"{occ['len'].sum()/days.height:.1f} of 15")

# ── L8 tax ──────────────────────────────────────────────────────────────────
def after_tax(v):
    """Indian equity tax at today's rates, by financial year, with set-off; returns (pre, post) CAGR."""
    d = det(v)
    t = pl.DataFrame(d["trades"]).with_columns(
        pl.from_epoch("exit_epoch").dt.date().alias("xd"),
        ((pl.col("exit_epoch") - pl.col("entry_epoch")) / 86400).alias("hd"))
    t = t.with_columns(pl.when(pl.col("xd").dt.month() >= 4).then(pl.col("xd").dt.year())
                       .otherwise(pl.col("xd").dt.year() - 1).alias("fy"),
                       (pl.col("hd") > 365).alias("lt"))
    eq = pl.DataFrame(d["equity_curve"]).with_columns(pl.col("date").str.to_date().alias("d"))
    factor, carry_st, carry_lt, paid = 1.0, 0.0, 0.0, []
    for fy in sorted(t["fy"].unique()):
        g = t.filter(pl.col("fy") == fy)
        st = g.filter(~pl.col("lt"))["net_pnl"].sum() + carry_st
        lt = g.filter(pl.col("lt"))["net_pnl"].sum() + carry_lt
        if st < 0:                       # short-term loss may offset long-term gain
            lt, st = lt + st, 0.0
        carry_st = 0.0
        carry_lt = min(lt, 0.0)
        tax = 0.20 * max(st, 0) + 0.125 * max(lt - 125000, 0)
        end = dt.date(fy + 1, 3, 31)
        e = eq.filter(pl.col("d") <= end)["value"].tail(1)
        if tax > 0 and e.len():
            factor *= 1 - tax / e[0]
            paid.append(tax)
    e0, e1 = eq["value"][0], eq["value"][-1]
    yrs = (eq["d"][-1] - eq["d"][0]).days / 365.25
    return (e1 / e0) ** (1 / yrs) - 1, (e1 * factor / e0) ** (1 / yrs) - 1, \
        t.filter(pl.col("lt")).height / t.height


section("L8 tax estimate (today's rates: STCG 20%, LTCG 12.5% over ₹1.25L; FY set-off)")
for v in ("gate_variant", "L9b_live_ebe", "L9b_live_ebe_20y"):
    if v in ROWS:
        pre, post, ltshare = after_tax(v)
        print(f"{v}: pre-tax {pre*100:.2f}% → after tax {post*100:.2f}% ({ltshare*100:.0f}% of trades held >1y)")
for w, (s, e) in {"2010_now": (1262304000, 1791504000), "20y": (1136073600, 1791504000)}.items():
    x = nb.filter(pl.col("date_epoch").is_between(s, e))
    yrs = (x["date_epoch"][-1] - x["date_epoch"][0]) / 86400 / 365.25
    g = x["close"][-1] / x["close"][0]
    print(f"NIFTYBEES {w}: pre-tax {(g ** (1/yrs) - 1)*100:.2f}% → after LTCG on exit "
          f"{((1 + (g - 1) * 0.875) ** (1/yrs) - 1)*100:.2f}% (price-only; NIFTYBEES NAV reinvests dividends)")

# ── L9 combined ─────────────────────────────────────────────────────────────
section("L9 live-realistic: next-open exits (exits first), lagged liquidity cap, ₹50 L, 15 bps")
print(HDR)
for v in ("gate_variant", "L9b_live_ebe", "L9b_live_ebe_20y", "L9_live", "L9_live_20y"):
    print(line(v))
if "L9b_live_ebe" in ROWS:
    print("\nyear by year, L9b_live_ebe vs NIFTYBEES (%):")
    for yr, ret in ROWS["L9b_live_ebe"]["yearly"].items():
        print(f"  {yr}: {ret*100:6.1f}   NIFTYBEES {BENCH['yearly'].get(yr, float('nan'))*100:6.1f}")

# ── L10 path dependence ─────────────────────────────────────────────────────
section("L10 path dependence: start shifted 0-27 days into Jan 2010, same end")
for pre, lab in (("L10_base_d", "engine as-is"), ("L10_live_d", "live-realistic (L9b settings)")):
    vs = sorted(v for v in ROWS if v.startswith(pre))
    if not vs:
        continue
    c = sorted(ROWS[v]["cagr"] for v in vs)
    m = sorted(ROWS[v]["mdd"] for v in vs)
    print(f"{lab}: {len(vs)} runs | CAGR min {c[0]*100:.2f}% median {(c[len(c)//2-1]+c[len(c)//2])/2*100:.2f}% "
          f"max {c[-1]*100:.2f}% | max DD worst {m[0]*100:.1f}% best {m[-1]*100:.1f}%")
    print("   " + ", ".join(f"d{v[-2:]} {ROWS[v]['cagr']*100:.1f}%" for v in vs))

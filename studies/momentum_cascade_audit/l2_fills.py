#!/usr/bin/env python3
"""L2: re-price every trade from nse_bhavcopy_historical (independent of charting).

Usage: l2_fills.py <variant> [...]   (reads out/<variant>.json)

bhav epoch = charting epoch - 5.5h (verified on RELIANCE 2015: 245/247 daily returns match).
Per trade, over the bars from entry to exit:
  * entry open: charting open vs bhav OPEN, scaled by the same day's close ratio;
  * each later bar: charting close/prev close vs bhav CLOSE/prev CLOSE. A day whose two
    returns differ by a clean split ratio is a corporate action (charting adjusted, bhav
    raw) and uses charting's return; every other day must agree;
  * exit: close fills compare the exit bar's close; next-open fills its open.
bhav-implied trade return is built from bhav prices on every non-CA day. Bhavcopy ends
2026-03-24; later trades are reported as unchecked. Also: stub opens (open == prev close).
"""
import io
import json
import math
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(os.path.dirname(HERE)), os.path.join(os.path.dirname(HERE), "long_window")]
from lib.cr_client import CetaResearch  # noqa: E402
import providers as P  # noqa: E402

IST = 19800
CACHE = os.path.join(HERE, "out", "bhav_traded.parquet")
NICE = [1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4, 1 / 5, 2 / 5, 3 / 5, 4 / 5, 1 / 6, 5 / 6, 1 / 7, 1 / 8, 1 / 10,
        10 / 11, 1 / 20, 4 / 7, 5 / 7, 1 / 1.1]


def is_ca(ratio):
    """True if a bhav/charting return ratio is a clean split/bonus factor (either direction)."""
    lr = abs(math.log(ratio))
    return lr > 0.08 and any(abs(lr - abs(math.log(n))) < 0.03 for n in NICE)


def bhav_for(symbols):
    have = pl.read_parquet(CACHE) if os.path.exists(CACHE) else None
    need = sorted(set(symbols) - (set(have["SYMBOL"].unique()) if have is not None else set()))
    if need:
        cr = CetaResearch()
        parts = []
        for k in range(0, len(need), 150):
            syms = ",".join(f"'{s}'" for s in need[k:k + 150])
            raw = cr.query(f"""SELECT SYMBOL, date_epoch, OPEN, CLOSE, PREV_CLOSE FROM nse.nse_bhavcopy_historical
                               WHERE SERIES = 'EQ' AND SYMBOL IN ({syms}) AND date_epoch >= 1230768000""",
                           format="parquet", verbose=False, timeout=600, limit=20000000)
            parts.append(pl.read_parquet(io.BytesIO(raw)))
        new = pl.concat(parts).with_columns(pl.col("date_epoch").cast(pl.Int64))
        have = new if have is None else pl.concat([have, new])
        have.write_parquet(CACHE)
    return have.filter(pl.col("SYMBOL").is_in(list(symbols))).unique(["SYMBOL", "date_epoch"])


_CHART = None


def chart():
    global _CHART
    if _CHART is None:
        df = pl.read_parquet(P.CHARTING)
        df, _ = P.drop_interleaved(P.back_adjust(df, P.detect_raw_events(df)))
        df, _ = P.split_stitches(df)
        _CHART = df.select(["symbol", pl.col("date_epoch").cast(pl.Int64), "open", "close"])
    return _CHART


def check(variant, next_open_exit):
    det = json.load(open(os.path.join(HERE, "out", f"{variant}.json")))["detailed"][0]
    t = pl.DataFrame(det["trades"]).with_columns(pl.col("symbol").str.replace("NSE:", "").alias("sym"))
    # split instruments ("AWL~0") are keyed by their base ticker in bhavcopy
    b = bhav_for([x.split("~")[0] for x in t["sym"].unique().to_list()]).with_columns(
        (pl.col("date_epoch") + IST).alias("date_epoch"))
    last_bhav = b["date_epoch"].max()
    j = (chart().filter(pl.col("symbol").is_in(t["sym"].unique().to_list()))
                .with_columns(pl.col("symbol").str.split("~").list.first().alias("SYMBOL"))
                .join(b, on=["SYMBOL", "date_epoch"], how="left")
                .sort(["symbol", "date_epoch"]))
    by_sym = {s[0]: g for s, g in j.group_by("symbol")}
    rows, bad_days = [], []
    for r in t.iter_rows(named=True):
        base = {k: r[k] for k in ("symbol", "entry_date", "exit_date", "exit_reason", "net_pnl")}
        if r["exit_epoch"] > last_bhav:
            rows.append({**base, "status": "after_bhav_end"})
            continue
        g = by_sym[r["sym"]].filter(pl.col("date_epoch").is_between(r["entry_epoch"], r["exit_epoch"]))
        # Entry and exit bars must exist in bhavcopy; a missing day in between (renames, gaps)
        # falls back to charting's own return and is counted as unchecked.
        if (g.height == 0 or g["date_epoch"][0] != r["entry_epoch"] or g["OPEN"][0] is None
                or g["CLOSE"][0] is None or g["CLOSE"][-1] is None or g["OPEN"][-1] is None):
            rows.append({**base, "status": "no_bhav_bar"})
            continue
        oc, cc, ob, cb = g["open"].to_list(), g["close"].to_list(), g["OPEN"].to_list(), g["CLOSE"].to_list()
        open_err = (oc[0] / cc[0]) / (ob[0] / cb[0]) - 1          # same-day open/close shape
        bh = cb[0] / ob[0]                                        # bhav: entry open -> entry close
        ca_days, mism, unchecked = 0, 0, 0
        steps = []   # per bar: (return used, bhav prev close or None) so the exit bar can be redone
        for k in range(1, len(cc)):
            rc = cc[k] / cc[k - 1]
            if cb[k] is None or cb[k - 1] is None:    # no one-day bhav comparison: use charting
                unchecked += 1
                steps.append((rc, None))
                continue
            rb = cb[k] / cb[k - 1]
            if is_ca(rb / rc):
                ca_days += 1
                steps.append((rc, None))
                continue
            if abs(rb / rc - 1) > 0.02:
                mism += 1
                bad_days.append({"symbol": r["symbol"], "date_epoch": g["date_epoch"][k], "chart_ret": rc,
                                 "bhav_ret": rb})
            steps.append((rb, cb[k - 1]))
        last_is_open = next_open_exit and r["exit_reason"] != "end_of_sim"
        if last_is_open:   # exit bar: prev close -> open instead of prev close -> close
            if not steps:
                bh = ob[0] / ob[0]
            else:
                ret, prev_b = steps[-1]
                steps[-1] = (ob[-1] / prev_b if prev_b else oc[-1] / cc[-2], prev_b)
        for ret, _ in steps:
            bh *= ret
        bt = r["exit_price"] / r["entry_price"]
        rows.append({**base, "status": "checked", "bt_ret": bt, "bh_ret": bh, "err": bt / bh - 1,
                     "open_err": open_err, "ca_days": ca_days, "mismatch_days": mism, "unchecked_days": unchecked,
                     "chart_stub": oc[0] == (by_sym[r["sym"]].filter(pl.col("date_epoch") < r["entry_epoch"])["close"].tail(1).to_list() or [None])[0]})
    out = pl.DataFrame(rows, infer_schema_length=None)
    ck = out.filter(pl.col("status") == "checked")
    bad = ck.filter(pl.col("err").abs() > 0.02)
    tot = t["net_pnl"].sum()
    print(f"\n== {variant}: {t.height} trades | checked {ck.height} | after bhav end "
          f"{out.filter(pl.col('status') == 'after_bhav_end').height} | no bhav bar "
          f"{out.filter(pl.col('status') == 'no_bhav_bar').height} (net {out.filter(pl.col('status') == 'no_bhav_bar')['net_pnl'].sum()/1e5:.1f} L)")
    print(f"   trade return vs bhavcopy: median {ck['err'].median()*100:+.3f}%  mean {ck['err'].mean()*100:+.3f}%  "
          f"p05 {ck['err'].quantile(0.05)*100:+.2f}%  p95 {ck['err'].quantile(0.95)*100:+.2f}%")
    print(f"   entry open vs bhav OPEN: median {ck['open_err'].median()*100:+.3f}%  |err|>1%: "
          f"{ck.filter(pl.col('open_err').abs() > 0.01).height}")
    print(f"   |trade err| > 2%: {bad.height} trades, net P&L {bad['net_pnl'].sum()/1e5:.1f} L "
          f"({bad['net_pnl'].sum()/tot*100:.1f}% of {tot/1e5:.1f} L) | CA days recognised {ck['ca_days'].sum()} | "
          f"unexplained >2% day mismatches {ck['mismatch_days'].sum()}")
    print(f"   entry bars with charting open == prev close: {ck['chart_stub'].sum()} of {ck.height} | "
          f"held days with no one-day bhav comparison: {ck['unchecked_days'].sum()}")
    if bad.height:
        print(bad.sort("err").select(["symbol", "entry_date", "exit_date", "bt_ret", "bh_ret", "err",
                                      "open_err", "mismatch_days", "net_pnl"]).with_columns(
            pl.col("err") * 100, pl.col("open_err") * 100))
    if bad_days:
        bd = pl.DataFrame(bad_days).with_columns(pl.from_epoch("date_epoch").dt.date().alias("d"))
        print("   mismatch days (first 15):", bd.select(["symbol", "d", "chart_ret", "bhav_ret"]).head(15).rows())
    out.write_parquet(os.path.join(HERE, "out", f"{variant}.l2.parquet"))


def stub_opens():
    """Liquid charting bars whose open equals the previous close exactly, vs bhavcopy on the same bars."""
    df = pl.read_parquet(P.CHARTING).sort(["symbol", "date_epoch"]).with_columns(
        pl.col("close").shift(1).over("symbol").alias("pc"),
        (pl.col("volume") * pl.col("average_price")).rolling_mean(125, min_samples=1).over("symbol").alias("tv"))
    liq = df.filter((pl.col("tv") > 7e7) & pl.col("pc").is_not_null() & (pl.col("date_epoch") >= 1262304000))
    stub = liq.filter(pl.col("open") == pl.col("pc"))
    print(f"liquid charting bars 2010+: {liq.height:,}; open == prev close exactly: {stub.height:,} "
          f"({stub.height/liq.height*100:.2f}%)")
    print("   by year:", stub.with_columns(pl.from_epoch("date_epoch").dt.year().alias("y")).group_by("y").len()
          .join(liq.with_columns(pl.from_epoch("date_epoch").dt.year().alias("y")).group_by("y").len(), on="y")
          .with_columns((pl.col("len") / pl.col("len_right") * 100).round(1).alias("pct")).sort("y")
          .select(["y", "pct"]).rows())


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--stubs" in args:
        stub_opens()
        args.remove("--stubs")
    for v in args:
        check(v, next_open_exit=("next_open" in v or "live" in v))

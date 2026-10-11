#!/usr/bin/env python3
"""momentum_cascade DAILY RUNNER — OBSERVE ONLY. Places NO orders.

Runs the champion through the latest repaired nse_charting close (the same engine signal
generator the backtest uses, on NseChartingRepairedProvider), then evaluates the LATEST bar
with the generator's own `entry_signal` / `walk_exit`, which the backtest itself never sees
(add_next_day_values drops each instrument's last bar). Output is the book and TOMORROW'S
ORDERS: entries signalled at today's close (buy at the open) and exits triggered at today's
close (sell at the open).

Paper ledger (results/momentum_cascade/paper_ledger.jsonl) is APPEND-ONLY: one row per entry
or exit on first sight, with the engine's own prices, never re-walked. A hash of closed trades
older than 5 days is compared with the previous run to catch history rewrites (data backfill).

Usage:
    momentum_cascade_daily.py                      # fetch, run, print, log
    momentum_cascade_daily.py --capital 1000000    # size proposals for a Rs 10L sleeve
    momentum_cascade_daily.py --no-log             # don't touch the ledger / observe log
    momentum_cascade_daily.py --snapshot DIR       # read a local snapshot instead of fetching
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import polars as pl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from engine.config_loader import load_config, get_entry_config_iterator, get_exit_config_iterator  # noqa: E402
from engine.pipeline import run_pipeline  # noqa: E402
from engine.signals.base import run_scanner, build_regime_filter  # noqa: E402
from engine.signals.momentum_cascade import build_inst_data, entry_signal, walk_exit  # noqa: E402
from engine import charting_repairs as R  # noqa: E402

DEFAULT_CFG = "strategies/momentum_cascade/config_live.yaml"
OUT_DIR = "results/momentum_cascade"
RESULT_JSON = f"{OUT_DIR}/daily_backtest.json"
LATEST_JSON = f"{OUT_DIR}/daily_latest.json"
OBSERVE_LOG = f"{OUT_DIR}/observe_log.jsonl"
LEDGER = f"{OUT_DIR}/paper_ledger.jsonl"
MAX_DATA_LAG_DAYS = 5
REWRITE_HORIZON_DAYS = 5
NON_EQUITY = ("ETF", "BEES", "SILVER", "GOLD", "LIQUID", "NIFTY", "SENSEX", "IETF", "ADD")


def india_today():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)).date()


def d_(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).date()


def is_non_equity(sym):
    s = sym.upper()
    return any(t in s for t in NON_EQUITY) and not s.startswith("GOLDIAM")


class _Fixed:
    """Provider that hands run_pipeline a frame fetched once by this script."""

    def __init__(self, df):
        self.df = df

    def fetch_ohlcv(self, *a, **k):
        return self.df


def fetch(cfg, snapshot_dir):
    st = cfg["static_config"]
    if snapshot_dir:
        sys.path.insert(0, os.path.join(REPO, "studies", "long_window"))
        os.environ["LW_SNAP_DIR"] = snapshot_dir
        import providers as P
        prov = P.SnapshotChartingProvider(ca=True, clean=True)
        df = prov.fetch_ohlcv(["NSE"], None, st["start_epoch"], st["end_epoch"], st["prefetch_days"])
        info = {"events": prov.events, "dropped": prov.dropped, "stitches": prov.splits}
    else:
        from engine.data_provider import NseChartingRepairedProvider
        prov = NseChartingRepairedProvider()
        df = prov.fetch_ohlcv(["NSE"], None, st["start_epoch"], st["end_epoch"], st["prefetch_days"])
        info = prov.info
    return df, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CFG)
    ap.add_argument("--capital", type=float, default=1_000_000, help="paper sleeve size for proposal qty")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--snapshot", default=None, help="local snapshot dir (studies/long_window/snapshot)")
    args = ap.parse_args()
    os.makedirs(os.path.join(REPO, OUT_DIR), exist_ok=True)

    cfg_path = os.path.join(REPO, args.config)
    cfg = load_config(cfg_path)
    entry_cfg = next(iter(get_entry_config_iterator(cfg)))
    exit_cfg = next(iter(get_exit_config_iterator(cfg)))
    sim_cfg = cfg["simulation_config_input"]
    max_pos = sim_cfg["max_positions"][0]
    mult = sim_cfg.get("order_value_multiplier", [1.0])[0]
    cap_pct = sim_cfg["max_order_value"][0]["value"] if sim_cfg.get("max_order_value") else None

    # ---- data ----
    df, info = fetch(cfg, args.snapshot)
    if df.is_empty():
        raise SystemExit("no data")
    last_epoch = int(df["date_epoch"].max())
    last_date = d_(last_epoch)
    stale = (india_today() - last_date).days
    if stale > MAX_DATA_LAG_DAYS:
        raise SystemExit(f"!! STALE DATA: last bar {last_date} is {stale} days behind India today "
                         f"({india_today()}); refusing to report.")
    last_rows = df.filter(pl.col("date_epoch") == last_epoch)
    dup_bars = last_rows.height - last_rows["instrument"].n_unique()
    recent_events = (info["events"].filter(pl.col("ex_epoch") >= last_epoch - 30 * 86400)
                     .select(["symbol", "ex_date", "num", "den", "kind"]).rows()) if info and info["events"] is not None else []

    # ---- backtest through the last bar (book + ledger source) ----
    sweep = run_pipeline(cfg_path, data_provider=_Fixed(df))
    res_path = os.path.join(REPO, RESULT_JSON)
    sweep.save(res_path)
    det = json.load(open(res_path))["detailed"][0]
    trades = det["trades"]
    # Open = still held at the last bar. A recycled-ticker segment (ROML~0) also ends in
    # "end_of_data", years ago, so require the exit to sit at the data's edge.
    edge = last_epoch - 10 * 86400
    book = [t for t in trades if t.get("exit_reason") == "end_of_data" and t["exit_epoch"] >= edge]
    closed = [t for t in trades if not (t.get("exit_reason") == "end_of_data" and t["exit_epoch"] >= edge)
              and t.get("exit_reason") != "end_of_sim"]

    # ---- latest-bar evaluation with the generator's own functions ----
    df_full = df.sort(["instrument", "date_epoch"]).with_columns([
        pl.col("date_epoch").shift(-1).over("instrument").alias("next_epoch"),
        pl.col("open").shift(-1).over("instrument").alias("next_open"),
        pl.col("volume").shift(-1).over("instrument").alias("next_volume"),
    ])
    inst = build_inst_data(df_full)
    ctx = {**cfg, "start_epoch": cfg["static_config"]["start_epoch"]}
    _, df_trim = run_scanner(ctx, df)
    scan_pass = {u for u, s in zip(df_trim["uid"].to_list(), df_trim["scanner_config_ids"].to_list()) if s}
    bull = build_regime_filter(df, entry_cfg.get("regime_instrument", ""), entry_cfg.get("regime_sma_period", 0))
    regime_on = (not entry_cfg.get("regime_instrument")) or (last_epoch in bull)
    tsl = exit_cfg["trailing_stop_pct"] / 100.0
    knobs = dict(exit_fill=exit_cfg.get("exit_fill", "close"), stop_mode=exit_cfg.get("stop_mode", "close"),
                 trail_from_entry=bool(exit_cfg.get("trail_from_entry", False)),
                 initial_stop_pct=float(exit_cfg.get("initial_stop_pct", 0) or 0) / 100.0)

    book_rows, pending_exits = [], []
    for t in book:
        d = inst.get(t["symbol"])
        if d is None or t["entry_epoch"] not in d["epochs"]:
            continue
        i0 = d["epochs"].index(t["entry_epoch"])
        sig_close = d["closes"][i0 - 1] if i0 > 0 else None
        w = walk_exit(d, i0, t["entry_price"], sig_close if sig_close else t["entry_price"], tsl,
                      exit_cfg["max_hold_days"], **knobs)
        n = len(d["closes"])
        last_c, prev_c = d["closes"][-1], d["closes"][-2] if n > 1 else None
        split_like = prev_c and last_c and R.looks_like_split(last_c / prev_c)
        pending = w["reason"] if (w["exit_epoch"] is None and w["reason"]) else None
        if pending and split_like:
            pending = None
        # The level the next close is tested against (close mode), or the GTT level (gtt mode).
        if knobs["stop_mode"] == "gtt":
            level = w["level"]
        elif w["armed"]:
            level = w["trail_high"] * (1 - tsl)
        elif knobs["initial_stop_pct"] > 0:
            level = t["entry_price"] * (1 - knobs["initial_stop_pct"])
        else:
            level = None
        row = {"symbol": t["symbol"].replace("NSE:", ""), "entry_date": t["entry_date"],
               "entry_px": round(t["entry_price"], 2), "last_px": round(last_c, 2),
               "unreal_pct": round((last_c / t["entry_price"] - 1) * 100, 1),
               "hold_days": int((last_epoch - t["entry_epoch"]) / 86400),
               "trail_high": round(w["trail_high"], 2), "armed": w["armed"],
               "stop_level": round(level, 2) if level else None,
               "pending_exit": pending, "flags": []}
        if split_like:
            row["flags"].append(f"SPLIT-LIKE BAR {last_c/prev_c:.3f}x: exit suppressed, verify")
        if is_non_equity(row["symbol"]):
            row["flags"].append("NON-EQUITY")
        if pending:
            pending_exits.append(row)
        book_rows.append(row)
    book_rows.sort(key=lambda r: r["entry_date"])

    # Fresh signals on the last bar (only meaningful if the regime is on).
    fast_lb, slow_lb = entry_cfg["fast_lookback_days"], entry_cfg["slow_lookback_days"]
    accel, mom, brk = entry_cfg["accel_threshold_pct"] / 100.0, entry_cfg["min_momentum_pct"] / 100.0, entry_cfg.get("breakout_window", 63)
    lookback = max(slow_lb, fast_lb * 2, brk) + 10
    rank_win = sim_cfg["order_ranking_window_days"][0]
    held = {r["symbol"] for r in book_rows if not r["pending_exit"]}
    cands = []
    for name, d in inst.items():
        n = len(d["closes"])
        if n < lookback + 2 or f"{name}:{last_epoch}" not in scan_pass or d["epochs"][-1] != last_epoch:
            continue
        if name.replace("NSE:", "") in held:
            continue
        a = entry_signal(d["closes"], n - 1, slow_lb, fast_lb, accel, mom, brk)
        if a is None:
            continue
        # top_gainer ranking: close_T vs close rank_win bars back (ranking.py uses prev_close at
        # the entry bar, which for tomorrow's entry is today's close). Null gain ranks last.
        ref = d["closes"][n - 1 - rank_win] if n - 1 - rank_win >= 0 else None
        gain = (d["closes"][-1] / ref - 1) if ref else None
        cands.append({"symbol": name.replace("NSE:", ""), "close": d["closes"][-1], "accel": a, "gain": gain})
    cands.sort(key=lambda c: (c["gain"] is None, -(c["gain"] or 0)))
    free = max(0, max_pos - len(book_rows) + len(pending_exits))

    # Sizing for the proposals: sleeve/15 x multiplier, capped at cap_pct of 30-bar mean turnover.
    tv = (df.sort(["instrument", "date_epoch"])
            .with_columns((pl.col("volume") * pl.col("average_price")).rolling_mean(30, min_samples=1)
                          .over("instrument").alias("tv"))
            .filter(pl.col("date_epoch") == last_epoch).select(["instrument", "tv"]))
    tv_map = dict(zip(tv["instrument"].to_list(), tv["tv"].to_list()))
    order_value = args.capital / max_pos * mult
    proposals = []
    for c in cands[:free] if regime_on else []:
        ov = order_value
        t30 = tv_map.get("NSE:" + c["symbol"])
        if cap_pct and t30:
            ov = min(ov, t30 * cap_pct / 100)
        qty = int(ov / c["close"])
        proposals.append({**c, "qty_at_close": qty, "value": round(qty * c["close"]),
                          "flags": ["NON-EQUITY"] if is_non_equity(c["symbol"]) else []})

    # ---- rewrite detector + ledger ----
    horizon = last_epoch - REWRITE_HORIZON_DAYS * 86400
    old_closed = sorted((t["symbol"], t["entry_epoch"], t["exit_epoch"], round(t["entry_price"], 4),
                         round(t["exit_price"], 4)) for t in closed if t["exit_epoch"] <= horizon)
    hist_hash = hashlib.md5(json.dumps(old_closed).encode()).hexdigest()
    # Compare only like with like: same config (any knob change re-derives history) and the
    # same horizon date. A different config resets the detector rather than crying wolf.
    cfg_hash = hashlib.md5(open(cfg_path, "rb").read()).hexdigest()
    prev = json.load(open(os.path.join(REPO, LATEST_JSON))) if os.path.isfile(os.path.join(REPO, LATEST_JSON)) else None
    rewritten = bool(prev and prev.get("history_hash") and prev["history_hash"] != hist_hash
                     and prev.get("history_through") == str(d_(horizon))
                     and prev.get("config_hash") == cfg_hash)
    # First run seeds the ledger with the book as found (seeded=True) and a seed marker; from
    # then on only entries and exits that appear after the seed date are observed rows.
    ledger_path = os.path.join(REPO, LEDGER)
    seen_entries, seen_exits, seed_epoch = set(), set(), None
    if os.path.isfile(ledger_path):
        for ln in open(ledger_path):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("type") == "seed":
                seed_epoch = r["data_last_epoch"]
            elif r.get("type") == "entry":
                seen_entries.add((r["symbol"], r["entry_epoch"]))
            elif r.get("type") == "exit":
                seen_exits.add((r["symbol"], r["entry_epoch"], r["exit_epoch"]))
    new_rows = []
    seeding = seed_epoch is None
    if seeding:
        seed_epoch = last_epoch
        new_rows.append({"type": "seed", "run": str(india_today()), "data_last": str(last_date),
                         "data_last_epoch": last_epoch, "config": args.config, "capital": args.capital})
    for t in book + ([] if seeding else [c for c in closed if c["exit_epoch"] > seed_epoch]):
        key = (t["symbol"], t["entry_epoch"])
        if key not in seen_entries:
            d = inst.get(t["symbol"])
            i0 = d["epochs"].index(t["entry_epoch"]) if d and t["entry_epoch"] in d["epochs"] else None
            new_rows.append({"type": "entry", "first_seen_run": str(india_today()), "data_last": str(last_date),
                             "symbol": t["symbol"], "entry_epoch": t["entry_epoch"], "entry_date": t["entry_date"],
                             "entry_price": t["entry_price"], "seeded": seeding,
                             "prior_close": d["closes"][i0 - 1] if i0 else None,
                             "entry_close": d["closes"][i0] if i0 is not None else None})
            seen_entries.add(key)
    for t in ([] if seeding else [c for c in closed if c["exit_epoch"] > seed_epoch]):
        key = (t["symbol"], t["entry_epoch"], t["exit_epoch"])
        if key not in seen_exits:
            new_rows.append({"type": "exit", "first_seen_run": str(india_today()), "data_last": str(last_date),
                             "symbol": t["symbol"], "entry_epoch": t["entry_epoch"], "exit_epoch": t["exit_epoch"],
                             "exit_date": t["exit_date"], "exit_price": t["exit_price"],
                             "exit_reason": t.get("exit_reason"), "pnl_pct": t.get("pnl_pct")})
            seen_exits.add(key)

    # ---- report ----
    W = 86
    print("\n" + "=" * W)
    print(f"  momentum_cascade DAILY  |  OBSERVE ONLY — NO ORDERS PLACED")
    print(f"  run {india_today()} IST  |  data through {last_date} ({stale}d lag)  |  regime "
          f"{'BULL' if regime_on else 'BEAR (no new entries)'}  |  book {len(book_rows)}/{max_pos}")
    print(f"  data health: dup bars on last date {dup_bars}; repairs: {info['events'].height if info else '?'} split events, "
          f"{len(recent_events)} in last 30d; history rewrite vs prior run: {'!! YES' if rewritten else 'no'}")
    if recent_events:
        print("  recent split events (verify any held name):", ", ".join(f"{s} {e} {int(n)}:{int(dn)} {k}" for s, e, n, dn, k in recent_events))
    print("=" * W)
    print(f"  {'BOOK':<12}{'ENTRY':>11}{'ENTRY_PX':>10}{'LAST':>9}{'UNRL%':>7}{'HELD':>6}{'HIGH':>9}{'ARMED':>7}{'LEVEL':>9}  NOTE")
    for r in book_rows:
        note = ("SELL AT OPEN (%s)" % r["pending_exit"]) if r["pending_exit"] else ""
        note += " ".join(" !! " + f for f in r["flags"])
        print(f"  {r['symbol']:<12}{r['entry_date']:>11}{r['entry_px']:>10}{r['last_px']:>9}{r['unreal_pct']:>7}"
              f"{r['hold_days']:>6}{r['trail_high']:>9}{str(r['armed']):>7}{str(r['stop_level'] or '-'):>9}  {note}")
    print("-" * W)
    print(f"  TOMORROW: sell at open {len(pending_exits)}; free slots {free}; signals on last bar {len(cands)}"
          + ("" if regime_on else " (regime BEAR: none taken)"))
    for p in proposals:
        print(f"    BUY  {p['symbol']:<12} ~{p['qty_at_close']} @ ~{p['close']:.2f} (₹{p['value']:,})  "
              f"gain360 {'-' if p['gain'] is None else f'{p['gain']*100:.0f}%'}  accel {p['accel']*100:.0f}%"
              + ("  !! NON-EQUITY" if p["flags"] else ""))
    if len(cands) > free:
        print("    (not taken, beyond free slots): " + ", ".join(c["symbol"] for c in cands[free:free + 10]))
    print(f"  Proposal sizing: ₹{args.capital:,.0f} sleeve / {max_pos} x {mult} = ₹{order_value:,.0f} per name, "
          f"capped at {cap_pct}% of 30-day turnover. Quantities are at today's close; recompute at the open.")
    print(f"  ledger: {sum(1 for r in new_rows if r['type']=='entry')} new entries, "
          f"{sum(1 for r in new_rows if r['type']=='exit')} new exits" + (" (--no-log: NOT written)" if args.no_log else ""))
    print("=" * W)

    payload = {"run_date": str(india_today()), "data_last": str(last_date), "stale_days": stale,
               "regime_on": regime_on, "dup_bars": dup_bars, "recent_split_events": recent_events,
               "history_hash": hist_hash, "history_through": str(d_(horizon)), "history_rewritten": rewritten,
               "config_hash": cfg_hash,
               "book": book_rows, "pending_exits": [r["symbol"] for r in pending_exits],
               "signals_last_bar": [c["symbol"] for c in cands], "proposals": proposals,
               "capital": args.capital, "config": args.config}
    with open(os.path.join(REPO, LATEST_JSON), "w") as f:
        json.dump(payload, f, indent=1, default=str)
    if not args.no_log:
        with open(os.path.join(REPO, OBSERVE_LOG), "a") as f:
            f.write(json.dumps(payload, default=str) + "\n")
        with open(ledger_path, "a") as f:
            for r in new_rows:
                f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()

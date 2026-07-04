#!/usr/bin/env python3
"""eod_breakout LIVE SHORTLIST — OBSERVE ONLY. Places NO orders.

Runs the validated IR-hyst champion (engine signal gen, the SAME one the
backtest uses) through the latest available nse_charting close, then reports
the strategy's CURRENT BOOK and TODAY'S FRESH ENTRIES — the names eod_breakout
would be holding / buying. This is the daily shortlist to MANUALLY VET + OBSERVE.

Open positions are identified as trades whose exit_reason == 'end_of_data'
(force-closed at sim end == still open). Fresh entries = entered on/near the
last data date. Each run is appended to an observation log so we can later
measure whether the signal (and our manual vetoes) add or subtract value.

NOTHING here authenticates to a broker or places an order. Pure read.

Usage:
    python scripts/eod_breakout_shortlist.py            # run + print + log
    python scripts/eod_breakout_shortlist.py --no-run   # reuse last result JSON
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CFG = "strategies/eod_breakout/config_ir_hyst_live.yaml"
RESULT_JSON = "results/eod_breakout/live_shortlist.json"
OBSERVE_LOG = "results/eod_breakout/observe_log.jsonl"
FRESH_WINDOW_DAYS = 7  # entry within this many days of last close = "fresh buy"


def india_today():
    # Machine clock is Irish, not India. India = UTC+5:30, no DST.
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)).date()


def run_backtest(cfg):
    out = os.path.join(REPO, RESULT_JSON)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(f">>> Running engine signal gen (champion) through latest close ...", flush=True)
    r = subprocess.run(
        [sys.executable, "run.py", "--config", cfg, "--output", out],
        cwd=REPO, capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-2000:] + "\n" + r.stderr[-2000:] + "\n")
        raise SystemExit(f"backtest failed (exit {r.returncode})")
    # surface the one useful line (data range) from engine stdout
    for ln in r.stdout.splitlines():
        if "Fetching nse_charting" in ln or "range=" in ln:
            print("   " + ln.strip())
    return out


def parse_date(s):
    return dt.date.fromisoformat(s) if s else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CFG)
    ap.add_argument("--no-run", action="store_true", help="reuse last result JSON instead of re-running")
    args = ap.parse_args()

    out = os.path.join(REPO, RESULT_JSON)
    if not args.no_run:
        out = run_backtest(args.config)
    elif not os.path.isfile(out):
        raise SystemExit("--no-run but no prior result JSON; run once without it first")

    d = json.load(open(out))
    det = d["detailed"][0]
    trades = det["trades"]
    if not trades:
        raise SystemExit("no trades in result")

    last_date = max(parse_date(t["exit_date"]) for t in trades if t.get("exit_date"))
    opens = [t for t in trades if t.get("exit_reason") == "end_of_data"]

    # build shortlist rows
    rows = []
    for t in opens:
        ed = parse_date(t["entry_date"])
        fresh = ed is not None and (last_date - ed).days <= FRESH_WINDOW_DAYS
        rows.append({
            "symbol": t["symbol"].replace("NSE:", ""),
            "entry_date": t["entry_date"],
            "entry_px": round(t["entry_price"], 2),
            "last_px": round(t["exit_price"], 2),      # latest close (mark)
            "unreal_pct": round(t["pnl_pct"], 1),
            "hold_days": int(t.get("hold_days") or 0),
            "fresh": fresh,
        })
    rows.sort(key=lambda r: (not r["fresh"], r["entry_date"]), reverse=False)
    fresh_rows = [r for r in rows if r["fresh"]]
    last_entry = max((r["entry_date"] for r in rows), default=None)
    # entries only fire in a bull internal-regime; recent entry => bull
    regime = "BULL" if fresh_rows else ("BEAR? (no entries within %dd)" % FRESH_WINDOW_DAYS)

    # ---- print ----
    print()
    print("=" * 78)
    print(f"  eod_breakout LIVE SHORTLIST  |  OBSERVE ONLY — NO ORDERS PLACED")
    print(f"  run {india_today()} IST  |  data through {last_date}  |  internal-regime: {regime}")
    print(f"  current book: {len(rows)} open positions  |  fresh (<= {FRESH_WINDOW_DAYS}d): {len(fresh_rows)}  |  last entry: {last_entry}")
    print("=" * 78)
    print(f"  {'SYMBOL':<13}{'ENTRY':>11}{'ENTRY_PX':>10}{'LAST_PX':>10}{'UNREAL%':>9}{'HELD_d':>7}  TAG")
    print("  " + "-" * 74)
    for r in rows:
        tag = "<< FRESH — VET" if r["fresh"] else ""
        print(f"  {r['symbol']:<13}{r['entry_date']:>11}{r['entry_px']:>10}{r['last_px']:>10}"
              f"{r['unreal_pct']:>9}{r['hold_days']:>7}  {tag}")
    print("  " + "-" * 74)
    print(f"  VET the FRESH names (governance/ASM-GSM/halt hard-disqualifiers only). OBSERVE the rest.")
    print(f"  CAVEAT: this is the current BOOK ({len(rows)}/15 slots), NOT a complete 'buy-today' list.")
    print(f"          Breakouts on the LATEST bar (they fill next open) and any beyond the 15 slots")
    print(f"          are NOT shown. A full daily buy-scan needs an uncapped signal-only run (TODO).")
    print("=" * 78)

    # ---- persist shortlist + append observe log ----
    payload = {
        "run_date": str(india_today()),
        "data_last": str(last_date),
        "regime": regime,
        "n_open": len(rows),
        "book_full": len(rows) >= 15,
        "fresh": [r["symbol"] for r in fresh_rows],
        "book": rows,
    }
    with open(os.path.join(REPO, "results/eod_breakout/shortlist_latest.json"), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(REPO, OBSERVE_LOG), "a") as f:
        f.write(json.dumps(payload) + "\n")
    print(f"  saved shortlist_latest.json + appended {OBSERVE_LOG}")


if __name__ == "__main__":
    main()

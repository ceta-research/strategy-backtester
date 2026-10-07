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

Screens (added 2026-08-16, see results/eod_breakout/GRADUATION_CRITERIA.md):
FLAG-ONLY in this observe flow — the book always shows the engine's TRUE state
(curating the observe output would contaminate the confidence data). The
automated version must instead EXCLUDE these classes UPSTREAM in the universe
and re-validate the backtest:
  - NON-EQUITY: ETF/fund symbols the universe lets through (SBILIQETF class)
  - RED_FLAG: names whose latest vetting_log.jsonl verdict is RED_FLAG
    (63MOONS class: live promoter litigation/scandal)

Usage:
    python scripts/eod_breakout_shortlist.py            # run + print + log
    python scripts/eod_breakout_shortlist.py --no-run   # reuse last result JSON
    python scripts/eod_breakout_shortlist.py --no-log   # don't append observe_log (testing)
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
VETTING_LOG = "results/eod_breakout/vetting_log.jsonl"
FRESH_WINDOW_DAYS = 7  # entry within this many days of last close = "fresh buy"

# Non-equity heuristic. Sensitivity-biased on purpose: a false positive costs one
# glance at a flag; a false negative is a 72-day ETF squatting a strategy slot.
NON_EQUITY_EXPLICIT = {"LIQUIDBEES", "GOLDBEES", "NIFTYBEES", "JUNIORBEES",
                       "SILVERBEES", "BANKBEES", "LIQUIDCASE"}


def is_non_equity(sym):
    s = sym.upper()
    return "ETF" in s or s.endswith("BEES") or s in NON_EQUITY_EXPLICIT


def load_vet_cache():
    """{symbol: (run_date, verdict)} from each symbol's LATEST vetting-ledger
    verdict. Skips run-summary rows (symbol/verdict null). Fail-open: a missing
    or corrupt ledger returns an empty cache — flags simply won't show."""
    cache = {}
    try:
        with open(os.path.join(REPO, VETTING_LOG)) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sym, verdict = r.get("symbol"), r.get("verdict")
                if not sym or not verdict:
                    continue
                rd = r.get("run_date", "")
                if sym not in cache or rd > cache[sym][0]:
                    cache[sym] = (rd, verdict)
    except OSError:
        pass
    return cache


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
    ap.add_argument("--no-log", action="store_true", help="don't append observe_log (testing)")
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

    # Last PROCESSED day comes from the equity curve. max(exit_date) is only the last
    # trade exit, which stays frozen while the book is empty (read as "stale data" for
    # three runs in Sep/Oct 2026 when the sim was in fact current).
    last_date = parse_date(det["equity_curve"][-1]["date"])
    last_exit = max((parse_date(t["exit_date"]) for t in trades if t.get("exit_date")), default=None)
    opens = [t for t in trades if t.get("exit_reason") == "end_of_data"]
    recent_exits = [t for t in trades if t.get("exit_date") and t.get("exit_reason") != "end_of_data"
                    and (last_date - parse_date(t["exit_date"])).days <= FRESH_WINDOW_DAYS]

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

    # ---- data-integrity guard (added 2026-08-16 after the duplicate-bar incident) ----
    # The sim can NEVER legitimately hold more than 15 open positions. If it does, the
    # input frame had duplicate (symbol,date) bars, which garbles the whole sim path
    # (2026-08-16: 24/15 "open" positions, 12 phantom same-day entries, rewritten
    # entry history). ROOT CAUSE: the NSE ingest cycle APPENDS a re-fetch of the last
    # stored bar (intentionally — the next cycle's final bar corrects a provisional
    # post-close bar) and the CHAINED repack task dedups afterwards. Between fetch
    # start and repack completion (daily ~00:00-03:30+ UTC = 05:30-09:00+ IST) the
    # warehouse TRANSIENTLY holds the last day twice. Runs in that window see a
    # corrupt frame. Diagnose: rows vs count(DISTINCT symbol) per date_epoch;
    # confirm repack_nse_charting_day completed in ts_task_queue after the fetch.
    corrupt = len(rows) > 15
    in_ingest_window = 0 <= dt.datetime.now(dt.timezone.utc).hour < 4
    if corrupt:
        print("\n" + "!" * 78)
        print("  !! DATA CORRUPTION SUSPECTED: %d open positions > 15-slot cap." % len(rows))
        print("  !! The book below is NOT TRUSTWORTHY — do NOT vet or act on it.")
        if in_ingest_window:
            print("  !! It is currently the daily NSE ingest window (~00:00-03:30+ UTC): the last")
            print("  !! day's bars are transiently DUPLICATED until repack_nse_charting_day runs.")
            print("  !! RE-RUN after the repack completes (check ts_task_queue).")
        else:
            print("  !! Outside the ingest window — check nse_charting_day for duplicate bars:")
            print("  !!   SELECT date_epoch, count(*), count(DISTINCT symbol) FROM nse.nse_charting_day")
            print("  !!   GROUP BY 1 HAVING count(*) > count(DISTINCT symbol) ORDER BY 1 DESC;")
        print("!" * 78)
    elif in_ingest_window:
        print("\n  NOTE: run started inside the daily NSE ingest window (~00:00-03:30+ UTC);")
        print("        if results look odd, re-run after repack_nse_charting_day completes.")

    # ---- screens (flag-only; see module docstring) ----
    vet_cache = load_vet_cache()
    for r in rows:
        flags = []
        if is_non_equity(r["symbol"]):
            flags.append("NON-EQUITY")
        vd = vet_cache.get(r["symbol"])
        if vd and vd[1] == "RED_FLAG":
            flags.append("RED_FLAG(%s)" % vd[0][5:])
        r["flags"] = flags
        r["vet"] = {"date": vd[0], "verdict": vd[1]} if vd else None
    screen_hits = {
        "non_equity": [r["symbol"] for r in rows if "NON-EQUITY" in r["flags"]],
        "red_flag": [r["symbol"] for r in rows if any(f.startswith("RED_FLAG") for f in r["flags"])],
    }

    # ---- print ----
    print()
    print("=" * 78)
    print(f"  eod_breakout LIVE SHORTLIST  |  OBSERVE ONLY — NO ORDERS PLACED")
    print(f"  run {india_today()} IST  |  data through {last_date}  |  internal-regime: {regime}")
    print(f"  current book: {len(rows)} open positions  |  fresh (<= {FRESH_WINDOW_DAYS}d): {len(fresh_rows)}  |  last entry: {last_entry}  |  last exit: {last_exit}")
    if recent_exits:
        from collections import Counter
        by = Counter((t["exit_date"], t.get("exit_reason")) for t in recent_exits)
        print(f"  exits in last {FRESH_WINDOW_DAYS}d: " + "; ".join(f"{d_} {n}x {r}" for (d_, r), n in sorted(by.items())))
    print("=" * 78)
    print(f"  {'SYMBOL':<13}{'ENTRY':>11}{'ENTRY_PX':>10}{'LAST_PX':>10}{'UNREAL%':>9}{'HELD_d':>7}  TAG")
    print("  " + "-" * 74)
    for r in rows:
        parts = []
        if r["fresh"]:
            parts.append("<< FRESH — VET")
        parts += ["!! " + f for f in r["flags"]]
        if not parts:
            v = r.get("vet")
            parts.append("vet:%s(%s)" % (v["verdict"], v["date"][5:]) if v else "vet:PENDING")
        print(f"  {r['symbol']:<13}{r['entry_date']:>11}{r['entry_px']:>10}{r['last_px']:>10}"
              f"{r['unreal_pct']:>9}{r['hold_days']:>7}  {'  '.join(parts)}")
    print("  " + "-" * 74)
    if screen_hits["non_equity"] or screen_hits["red_flag"]:
        print(f"  !! SCREEN HITS — non-equity: {', '.join(screen_hits['non_equity']) or '-'}"
              f" | red-flag ledger: {', '.join(screen_hits['red_flag']) or '-'}")
        print(f"     Flag-only in OBSERVE mode. Automation spec: exclude these classes UPSTREAM")
        print(f"     in the universe + re-validate the backtest (GRADUATION_CRITERIA.md).")
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
        "suspect_corrupt": corrupt,
        "fresh": [r["symbol"] for r in fresh_rows],
        "screen_hits": screen_hits,
        "book": rows,
    }
    with open(os.path.join(REPO, "results/eod_breakout/shortlist_latest.json"), "w") as f:
        json.dump(payload, f, indent=2)
    if args.no_log:
        print(f"  saved shortlist_latest.json (--no-log: observe_log NOT appended)")
    else:
        with open(os.path.join(REPO, OBSERVE_LOG), "a") as f:
            f.write(json.dumps(payload) + "\n")
        print(f"  saved shortlist_latest.json + appended {OBSERVE_LOG}")


if __name__ == "__main__":
    main()

"""Pure intraday Opening-Range Breakout — Phase A baseline.

Each day stands alone:
  - Universe = top N symbols by trailing 30-day minute turnover (monthly rebalance)
  - Setup = first OR_MINUTES minutes' high (per stock)
  - Entry = first bar after OR window with high > OR-high
           Fill at max(OR-high, bar_open) * (1 + slip/10000)  [bug-fix carried]
  - Exit = target / stop / EOD 15:25
           Loop starts at range(entry_idx + 1, ...)            [bug-fix carried]
  - No regime filter, no daily data, single-day positions only

Decision gate: CAGR <= 0% at 0bps slippage → ORB has no basic edge.
"""
import sys, json, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_minute_data, nse_intraday_charges, SECONDS_IN_ONE_DAY,
)
import polars as pl
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import statistics


# ── Config ───────────────────────────────────────────────────────────────

CONFIG = {
    "start_date": "2022-01-01",
    "end_date": "2025-12-31",
    "initial_capital": 1_000_000,
    "top_n": 50,
    "min_avg_turnover": 50_000_000,  # Rs 5 Cr/day in trailing window (relaxed)
    "universe_lookback_days": 30,
    "opening_range_minutes": 15,     # 09:15-09:30
    "target_pct": 1.0,
    "stop_pct": 0.5,
    "max_positions": 5,
    "eod_exit_minute": 925,          # 15:25
    "slippage_bps_list": [0, 3],     # both runs in one pass
}

OR_END_MINUTE_OFFSET = 555           # 09:15 IST stored as bar_minute=555


# ── Helpers ──────────────────────────────────────────────────────────────

def month_starts(start_date_str, end_date_str):
    """Return list of (month_start_date_str, month_start_epoch) covering range."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append((cur.strftime("%Y-%m-%d"), int(cur.timestamp())))
        # Next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


def compute_universe(df_minute, ref_epoch, lookback_days, top_n, min_turnover):
    """Top-N symbols by total turnover in [ref_epoch - lookback_days, ref_epoch)."""
    start_ep = ref_epoch - lookback_days * SECONDS_IN_ONE_DAY
    win = df_minute.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") < ref_epoch)
    )
    if win.is_empty():
        return []
    agg = win.group_by("symbol").agg(
        (pl.col("close") * pl.col("volume")).sum().alias("tv")
    ).filter(pl.col("tv") >= min_turnover).sort("tv", descending=True).head(top_n)
    return agg["symbol"].to_list()


def simulate_orb_day(day_df, universe, config, margin):
    """Simulate ORB for one day; returns (trades, day_pnl)."""
    or_min = config["opening_range_minutes"]
    target = config["target_pct"] / 100
    stop = config["stop_pct"] / 100
    eod = config["eod_exit_minute"]
    max_pos = config["max_positions"]
    slip = config["slippage_bps"]
    or_end = OR_END_MINUTE_OFFSET + or_min

    candidates = []
    sym_to_bars = {}
    for sym in universe:
        bars = day_df.filter(pl.col("symbol") == sym).sort("bar_minute").to_dicts()
        if len(bars) < or_min + 5:
            continue
        sym_to_bars[sym] = bars
        or_bars = [b for b in bars if OR_END_MINUTE_OFFSET <= b["bar_minute"] < or_end]
        post = [b for b in bars if b["bar_minute"] >= or_end]
        if not or_bars or not post:
            continue
        valid_highs = [b["high"] for b in or_bars if b["high"]]
        if not valid_highs:
            continue
        or_high = max(valid_highs)
        if or_high <= 0:
            continue
        # First post-OR breakout
        for i, bar in enumerate(post):
            if bar["high"] and bar["high"] > or_high:
                candidates.append({
                    "sym": sym, "or_high": or_high,
                    "minute": bar["bar_minute"], "idx": i, "post": post,
                })
                break

    if not candidates:
        return [], 0.0

    candidates.sort(key=lambda c: (c["minute"], c["sym"]))
    selected = candidates[:max_pos]
    order_value = margin / max_pos

    trades = []
    pnl_sum = 0.0
    for c in selected:
        bars = c["post"]
        idx = c["idx"]
        bar_open = bars[idx].get("open") or c["or_high"]
        fill = max(c["or_high"], bar_open)
        entry_price = fill * (1 + slip / 10000)

        exit_price = exit_type = None
        for j in range(idx + 1, len(bars)):
            b = bars[j]
            bh, bl, bc = (b["high"] or entry_price), (b["low"] or entry_price), (b["close"] or entry_price)
            if target > 0 and bh >= entry_price * (1 + target):
                exit_price, exit_type = entry_price * (1 + target), "target"; break
            if stop > 0 and bl <= entry_price * (1 - stop):
                exit_price, exit_type = entry_price * (1 - stop), "stop"; break
            if b["bar_minute"] >= eod:
                exit_price, exit_type = bc, "eod_close"; break
        if exit_price is None and bars:
            exit_price = bars[-1]["close"] or entry_price
            exit_type = "eod_close_fallback"
        if not exit_price or exit_price <= 0:
            continue

        exit_price = exit_price * (1 - slip / 10000)
        charges = nse_intraday_charges(order_value)
        pnl = (exit_price - entry_price) / entry_price * order_value - charges
        pnl_sum += pnl
        trades.append({
            "symbol": c["sym"],
            "or_high": round(c["or_high"], 2),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "entry_minute": c["minute"],
            "exit_type": exit_type,
            "pnl": round(pnl, 2),
            "year": datetime.fromtimestamp(bars[idx]["dateEpoch"], tz=timezone.utc).year,
        })
    return trades, pnl_sum


def metrics(equity, trades):
    if not equity or len(equity) < 2:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, trades=0, win_rate=0, final=0)
    initial = equity[0][1]
    final = equity[-1][1]
    days = (equity[-1][0] - equity[0][0]) / SECONDS_IN_ONE_DAY
    years = max(days / 365.25, 1e-9)
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 else -100
    peak, max_dd = initial, 0.0
    daily_rets = []
    prev = initial
    for _, eq in equity:
        peak = max(peak, eq)
        max_dd = min(max_dd, (eq - peak) / peak)
        if prev > 0:
            daily_rets.append((eq - prev) / prev)
        prev = eq
    if len(daily_rets) > 1:
        sd = statistics.stdev(daily_rets)
        sharpe = (statistics.mean(daily_rets) / sd) * (252 ** 0.5) if sd > 0 else 0
    else:
        sharpe = 0
    calmar = (cagr / abs(max_dd * 100)) if max_dd < 0 else 0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return dict(
        cagr=round(cagr, 2), mdd=round(max_dd * 100, 2),
        sharpe=round(sharpe, 3), calmar=round(calmar, 3),
        trades=len(trades),
        win_rate=round(wins / len(trades) * 100, 1) if trades else 0,
        final=round(final, 2),
    )


def run_one(df_minute, config):
    print(f"\n--- Running ORB(slip={config['slippage_bps']}bps) ---")
    t0 = time.time()
    months = month_starts(config["start_date"], config["end_date"])
    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    # Pre-compute monthly universes
    print("  Building monthly universes...")
    month_universes = {}
    for date_str, ep in months:
        u = compute_universe(
            df_minute, ep, config["universe_lookback_days"],
            config["top_n"], config["min_avg_turnover"],
        )
        month_universes[date_str] = u
    nonempty = sum(1 for u in month_universes.values() if u)
    print(f"  {nonempty}/{len(months)} months have non-empty universe")

    # Simulate per day
    print("  Running per-day simulation...")
    margin = float(config["initial_capital"])
    all_trades = []
    equity = [(start_ep, margin)]

    # Group minute data by date_key for fast per-day access
    unique_dates = sorted(df_minute.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") < end_ep)
    )["date_key"].unique().to_list())

    sim_start = time.time()
    for i, date_key in enumerate(unique_dates):
        day_df = df_minute.filter(pl.col("date_key") == date_key)
        if day_df.is_empty():
            continue
        # Pick the active monthly universe (most recent month_start <= this date)
        day_epoch = date_key * SECONDS_IN_ONE_DAY
        day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        active_universe = []
        for date_str, ep in months:
            if ep <= day_epoch:
                active_universe = month_universes[date_str]
            else:
                break
        if not active_universe:
            continue
        cfg = {**config}
        trades, pnl = simulate_orb_day(day_df, active_universe, cfg, margin)
        if trades:
            margin += pnl
            equity.append((day_epoch, margin))
            all_trades.extend(trades)
        if (i + 1) % 100 == 0:
            print(f"    Day {i+1}/{len(unique_dates)} ({day_str}): margin={margin:,.0f}, trades_so_far={len(all_trades)}")

    print(f"  Simulation done in {time.time()-sim_start:.0f}s, {time.time()-t0:.0f}s total")
    return all_trades, equity


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    print("="*70)
    print("PURE INTRADAY OPENING-RANGE BREAKOUT — Phase A baseline")
    print("="*70)
    print(json.dumps({k: v for k, v in CONFIG.items() if k != "slippage_bps_list"}, indent=2))

    # Load all NSE minute data
    df_minute = load_minute_data(symbols=None)
    df_minute = df_minute.with_columns([
        pl.col("dateEpoch").cast(pl.Int64).alias("dateEpoch"),
    ])

    all_results = {}
    for slip in CONFIG["slippage_bps_list"]:
        cfg = {**CONFIG, "slippage_bps": slip}
        trades, equity = run_one(df_minute, cfg)
        m = metrics(equity, trades)
        # Per-year breakdown
        per_year = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
        for t in trades:
            y = t["year"]
            per_year[y]["pnl"] += t["pnl"]
            per_year[y]["trades"] += 1
            if t["pnl"] > 0:
                per_year[y]["wins"] += 1
        per_year_summary = {y: {
            "trades": v["trades"],
            "pnl_pct_of_initial": round(v["pnl"] / CONFIG["initial_capital"] * 100, 2),
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
        } for y, v in sorted(per_year.items())}
        # Exit-type distribution
        exit_dist = defaultdict(int)
        for t in trades:
            exit_dist[t["exit_type"]] += 1
        all_results[slip] = {"metrics": m, "per_year": per_year_summary, "exit_types": dict(exit_dist)}
        print(f"\n  RESULT slip={slip}bps: CAGR={m['cagr']}% MDD={m['mdd']}% "
              f"Sharpe={m['sharpe']} Calmar={m['calmar']} Trades={m['trades']} WR={m['win_rate']}% Final=Rs {m['final']:,.0f}")

    # Persist
    with open("/home/swas/backtester/pure_orb_phase_a.json", "w") as f:
        json.dump(all_results, f, indent=2)

    # Summary table
    print(f"\n{'='*70}")
    print("PHASE A — SUMMARY")
    print(f"{'='*70}")
    for slip in CONFIG["slippage_bps_list"]:
        r = all_results[slip]
        m = r["metrics"]
        print(f"\nSlippage = {slip} bps")
        print(f"  Overall: CAGR={m['cagr']}%  MDD={m['mdd']}%  Calmar={m['calmar']}  "
              f"Sharpe={m['sharpe']}  Trades={m['trades']}  WR={m['win_rate']}%")
        print(f"  Per-year:")
        for y, v in r["per_year"].items():
            print(f"    {y}: {v['trades']:>5d} trades, PnL={v['pnl_pct_of_initial']:>+6.2f}% of initial, WR={v['win_rate']}%")
        print(f"  Exit types: {r['exit_types']}")

    # Decision gate
    cagr_0 = all_results[0]["metrics"]["cagr"]
    cagr_3 = all_results[3]["metrics"]["cagr"]
    print(f"\n{'='*70}")
    print("DECISION GATE")
    print(f"{'='*70}")
    if cagr_0 <= 0:
        print(f"  RESULT: NO EDGE (CAGR={cagr_0}% <= 0% at 0 slip)")
        print("  ACTION: Pure ORB has no basic edge. Pivot to a different setup.")
    elif cagr_3 <= 0:
        print(f"  RESULT: EDGE EXISTS BUT FRAGILE ({cagr_0}% raw → {cagr_3}% at 3bps)")
        print("  ACTION: Slippage destroys edge. Marginal. Decide whether to continue.")
    elif cagr_0 > 5:
        print(f"  RESULT: PROMISING ({cagr_0}% at 0 slip, {cagr_3}% at 3bps)")
        print("  ACTION: Proceed to Phase B — opening-range size sweep + per-year stability.")
    else:
        print(f"  RESULT: SMALL POSITIVE EDGE ({cagr_0}% at 0 slip, {cagr_3}% at 3bps)")
        print("  ACTION: Marginal. Consider Phase B but expect tight margins.")


if __name__ == "__main__":
    main()

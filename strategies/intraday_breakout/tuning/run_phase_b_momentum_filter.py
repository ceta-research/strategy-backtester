"""Phase B — Momentum-filtered intraday breakout.

Tests whether universe selection (filtering to stocks in a confirmed
daily uptrend) rescues the intraday breakout mechanism that Phase A
showed has no edge on a top-50-by-turnover universe.

Universe (weekly rebalance, Mon–Fri stable):
  - Min liquidity: avg 30-day turnover > Rs 5 Cr/day
  - Trend: close > 10-day SMA AND close > 50-day SMA
  - Momentum present: 20-day return > 0%
  - NOT slowing: 5-day return > 0%      ← the user's anti-filter
  - Rank candidates by 20-day return, take top 15

Intraday execution (carried from Phase A, both bug fixes preserved):
  - 15-min opening range
  - Entry: first post-OR bar with high > OR-high
  - Fill: max(OR-high, bar_open) * (1 + slip/10000)
  - Exit: target 1.5% / stop 0.7% / EOD 15:25
  - Exit loop from range(entry_idx + 1, ...)
  - Max 5 concurrent positions/day, equal weight
"""
import sys, json, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_daily_data, load_minute_data,
    nse_intraday_charges, SECONDS_IN_ONE_DAY,
)
import polars as pl
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import statistics


CONFIG = {
    "start_date": "2022-01-01",
    "end_date": "2025-12-31",
    "initial_capital": 1_000_000,
    "universe_size": 15,
    "min_avg_turnover": 50_000_000,        # Rs 5 Cr/day, trailing 30 days
    "rebalance_weekday": 4,                # 0=Mon, 4=Fri
    "lookback_20d": 20,
    "lookback_5d": 5,
    "sma_short": 10,
    "sma_long": 50,
    "opening_range_minutes": 15,
    "target_pct": 1.5,
    "stop_pct": 0.7,
    "max_positions": 5,
    "eod_exit_minute": 925,
    "slippage_bps_list": [0, 3],
}

OR_OFFSET = 555  # 09:15 IST as bar_minute (timestamps are local-as-UTC)


# ── Universe selection (uses DAILY data; relative comparisons are split-safe) ─

def compute_weekly_universes(df_daily, config):
    """For each Monday in range, compute the universe based on prior Friday close.

    Returns: dict[monday_date_str -> list of instrument names]
    """
    start = datetime.strptime(config["start_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(config["end_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    rebalance_wd = config["rebalance_weekday"]

    # Generate all rebalance Fridays
    fridays = []
    cur = start
    while cur <= end:
        if cur.weekday() == rebalance_wd:
            fridays.append(cur)
        cur += timedelta(days=1)

    universes = {}
    sma_long = config["sma_long"]
    sma_short = config["sma_short"]
    lookback_20 = config["lookback_20d"]
    lookback_5 = config["lookback_5d"]
    min_turnover = config["min_avg_turnover"]
    universe_size = config["universe_size"]

    print(f"  Computing universes for {len(fridays)} weekly rebalance dates...")
    for fri in fridays:
        fri_epoch = int(fri.timestamp())
        # Window: need at least sma_long days of history before this date
        window_start = fri_epoch - (sma_long + 10) * SECONDS_IN_ONE_DAY * 2  # buffer for holidays
        window_end = fri_epoch + SECONDS_IN_ONE_DAY  # inclusive of Friday

        win = df_daily.filter(
            (pl.col("date_epoch") >= window_start) &
            (pl.col("date_epoch") <= window_end)
        )
        if win.is_empty():
            continue

        # Per-instrument metrics
        # Get last sma_long+1 bars per instrument
        per_inst = (win.sort(["instrument", "date_epoch"])
                       .group_by("instrument")
                       .tail(sma_long + 5))

        # For each instrument, compute metrics
        candidates = []
        for inst_name, grp in per_inst.group_by("instrument"):
            inst_name = inst_name[0] if isinstance(inst_name, tuple) else inst_name
            rows = grp.sort("date_epoch").to_dicts()
            if len(rows) < sma_long + 1:
                continue
            closes = [r["close"] for r in rows if r["close"]]
            volumes = [r["volume"] for r in rows if r["volume"]]
            if len(closes) < sma_long + 1:
                continue
            today_close = closes[-1]
            sma10 = sum(closes[-sma_short:]) / sma_short
            sma50 = sum(closes[-sma_long:]) / sma_long
            if today_close <= sma10 or today_close <= sma50:
                continue  # not in uptrend
            if len(closes) < lookback_20 + 1:
                continue
            ret_20 = (today_close - closes[-lookback_20-1]) / closes[-lookback_20-1]
            if ret_20 <= 0:
                continue  # no positive momentum
            if len(closes) < lookback_5 + 1:
                continue
            ret_5 = (today_close - closes[-lookback_5-1]) / closes[-lookback_5-1]
            if ret_5 <= 0:
                continue  # slowing down — skip
            # Liquidity check (avg turnover trailing 30 days)
            recent = rows[-30:] if len(rows) >= 30 else rows
            avg_turnover = sum((r.get("close") or 0) * (r.get("volume") or 0) for r in recent) / len(recent)
            if avg_turnover < min_turnover:
                continue
            candidates.append((inst_name, ret_20, avg_turnover))

        # Rank by 20-day return, take top N
        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = [c[0] for c in candidates[:universe_size]]

        # Apply this universe to the Mon-Fri AFTER this Friday
        for offset_day in range(1, 8):  # Mon-Sun next week
            apply_date = fri + timedelta(days=offset_day)
            universes[apply_date.strftime("%Y-%m-%d")] = selected

    return universes


# ── Intraday simulation (mirrors Phase A) ────────────────────────────────

def simulate_orb_day(day_minute_df, universe_fmp_syms, config, margin):
    """Simulate ORB on one day for a given universe (FMP symbol format)."""
    or_min = config["opening_range_minutes"]
    target = config["target_pct"] / 100
    stop = config["stop_pct"] / 100
    eod = config["eod_exit_minute"]
    max_pos = config["max_positions"]
    slip = config["slippage_bps"]
    or_end = OR_OFFSET + or_min

    candidates = []
    for sym in universe_fmp_syms:
        bars = day_minute_df.filter(pl.col("symbol") == sym).sort("bar_minute").to_dicts()
        if len(bars) < or_min + 5:
            continue
        or_bars = [b for b in bars if OR_OFFSET <= b["bar_minute"] < or_end]
        post = [b for b in bars if b["bar_minute"] >= or_end]
        if not or_bars or not post:
            continue
        valid_highs = [b["high"] for b in or_bars if b["high"]]
        if not valid_highs:
            continue
        or_high = max(valid_highs)
        if or_high <= 0:
            continue
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


def metrics(equity, trades, initial_capital):
    if not equity or len(equity) < 2:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, trades=0, win_rate=0, final=initial_capital)
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


def run_one(df_daily, df_minute, weekly_universes, config):
    print(f"\n--- Running Phase B (slip={config['slippage_bps']}bps) ---")
    t0 = time.time()
    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    margin = float(config["initial_capital"])
    all_trades = []
    equity = [(start_ep, margin)]
    universe_size_log = []

    unique_dates = sorted(df_minute.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") < end_ep)
    )["date_key"].unique().to_list())

    sim_start = time.time()
    for i, date_key in enumerate(unique_dates):
        day_df = df_minute.filter(pl.col("date_key") == date_key)
        if day_df.is_empty():
            continue
        day_epoch = date_key * SECONDS_IN_ONE_DAY
        day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        active_universe = weekly_universes.get(day_str, [])
        if not active_universe:
            continue
        # Convert NSE bare-symbol format (e.g., RELIANCE) to FMP format (RELIANCE.NS)
        fmp_syms = [u.replace("NSE:", "") + ".NS" for u in active_universe]
        universe_size_log.append(len(fmp_syms))
        cfg = {**config}
        trades, pnl = simulate_orb_day(day_df, fmp_syms, cfg, margin)
        if trades:
            margin += pnl
            equity.append((day_epoch, margin))
            all_trades.extend(trades)
        if (i + 1) % 100 == 0:
            print(f"    Day {i+1}/{len(unique_dates)} ({day_str}): margin={margin:,.0f}, trades={len(all_trades)}")

    print(f"  Simulation done in {time.time()-sim_start:.0f}s, {time.time()-t0:.0f}s total")
    if universe_size_log:
        print(f"  Universe size avg: {sum(universe_size_log)/len(universe_size_log):.1f}, "
              f"min: {min(universe_size_log)}, max: {max(universe_size_log)}")
    return all_trades, equity


def main():
    print("="*70)
    print("PHASE B — Momentum-filtered intraday breakout")
    print("="*70)
    print(json.dumps({k: v for k, v in CONFIG.items() if k != "slippage_bps_list"}, indent=2))

    # Load daily data for universe selection
    end_ep = int(datetime.strptime(CONFIG["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    start_ep = int(datetime.strptime(CONFIG["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    prefetch_ep = start_ep - 200 * SECONDS_IN_ONE_DAY  # 200 days for SMA50 + buffer

    print("\n--- Loading daily data ---")
    df_daily = load_daily_data(prefetch_ep, end_ep)

    print("\n--- Loading minute data ---")
    df_minute = load_minute_data(symbols=None)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64).alias("dateEpoch")])

    print("\n--- Building weekly universes from daily data ---")
    weekly_universes = compute_weekly_universes(df_daily, CONFIG)
    nonempty = sum(1 for v in weekly_universes.values() if v)
    print(f"  {nonempty}/{len(weekly_universes)} day-keys mapped to a universe")
    # Sample a few
    for d in sorted(weekly_universes.keys())[:5]:
        u = weekly_universes[d]
        if u:
            print(f"  {d}: {u[:5]}{' ...' if len(u) > 5 else ''} ({len(u)} total)")
            break
    sample_dates = ["2023-01-09", "2024-01-08", "2025-01-06"]
    for d in sample_dates:
        if d in weekly_universes:
            print(f"  {d}: {weekly_universes[d][:8]}")

    all_results = {}
    for slip in CONFIG["slippage_bps_list"]:
        cfg = {**CONFIG, "slippage_bps": slip}
        trades, equity = run_one(df_daily, df_minute, weekly_universes, cfg)
        m = metrics(equity, trades, CONFIG["initial_capital"])
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
        exit_dist = defaultdict(int)
        for t in trades:
            exit_dist[t["exit_type"]] += 1
        all_results[slip] = {"metrics": m, "per_year": per_year_summary, "exit_types": dict(exit_dist)}
        print(f"\n  RESULT slip={slip}bps: CAGR={m['cagr']}% MDD={m['mdd']}% "
              f"Sharpe={m['sharpe']} Calmar={m['calmar']} Trades={m['trades']} "
              f"WR={m['win_rate']}% Final=Rs {m['final']:,.0f}")

    with open("/home/swas/backtester/phase_b_momentum.json", "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'='*70}")
    print("PHASE B — SUMMARY")
    print(f"{'='*70}")
    for slip in CONFIG["slippage_bps_list"]:
        r = all_results[slip]
        m = r["metrics"]
        print(f"\nSlippage = {slip} bps")
        print(f"  Overall: CAGR={m['cagr']}%  MDD={m['mdd']}%  Calmar={m['calmar']}  "
              f"Sharpe={m['sharpe']}  Trades={m['trades']}  WR={m['win_rate']}%")
        print(f"  Per-year:")
        for y, v in r["per_year"].items():
            print(f"    {y}: {v['trades']:>4d} trades, PnL={v['pnl_pct_of_initial']:>+6.2f}%, WR={v['win_rate']}%")
        print(f"  Exit types: {r['exit_types']}")

    cagr_0 = all_results[0]["metrics"]["cagr"]
    cagr_3 = all_results[3]["metrics"]["cagr"]
    print(f"\n{'='*70}")
    print("DECISION GATE")
    print(f"{'='*70}")
    if cagr_0 <= 0:
        print(f"  RESULT: NO EDGE FROM MOMENTUM FILTER (CAGR={cagr_0}% at 0 slip)")
        print("  ACTION: Universe selection doesn't rescue intraday breakout.")
        print("  Pivot to mean-reversion / pair trading.")
    elif cagr_0 < 5:
        print(f"  RESULT: WEAK EDGE ({cagr_0}% raw, {cagr_3}% at 3bps)")
        print("  ACTION: Filter helps but not enough. Tune or pivot.")
    elif cagr_3 <= 0:
        print(f"  RESULT: GROSS POSITIVE BUT NET NEGATIVE ({cagr_0}% raw, {cagr_3}% at 3bps)")
        print("  ACTION: Real edge but slippage destroys it. Limit orders may save.")
    else:
        print(f"  RESULT: PROMISING ({cagr_0}% at 0 slip, {cagr_3}% at 3bps)")
        print("  ACTION: Proceed to parameter sweep + per-year stability checks.")


if __name__ == "__main__":
    main()

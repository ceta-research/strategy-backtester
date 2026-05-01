"""Phase B variant — Nifty 50 / Nifty 100 universe constraint.

Survivorship-bias mitigation: restrict candidate pool to current
large-cap index members. Large-caps almost never delist (low survivor
selection bias). Momentum filter then operates within this restricted
pool.

Note: index constituents drift slightly over 2022-2025 (e.g., LICI added
2022, JIOFIN added 2023, SHRIRAMFIN added 2023). Stocks removed from
the index didn't blow up — they were demoted while still trading. So
even with current-membership lists, survivorship is materially clean.
"""
import sys, json, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import load_daily_data, load_minute_data, SECONDS_IN_ONE_DAY
from run_phase_b_momentum_filter import (
    compute_weekly_universes, simulate_orb_day, metrics, CONFIG, OR_OFFSET,
)
import polars as pl
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import statistics


# ── Index constituent lists (May 2026) ──────────────────────────────────

NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "ULTRACEMCO", "WIPRO",
]

NIFTY_NEXT_50 = [
    "ABB", "ACC", "ADANIPOWER", "ADANIGREEN", "AMBUJACEM",
    "AUROPHARMA", "BANKBARODA", "BHEL", "BOSCHLTD", "CANBK",
    "CHOLAFIN", "COLPAL", "DABUR", "DIVISLAB", "DLF",
    "DMART", "GAIL", "GODREJCP", "HAVELLS", "HDFCAMC",
    "ICICIGI", "ICICIPRULI", "INDHOTEL", "INDIGO", "IOC",
    "IRCTC", "JINDALSTEL", "LICI", "LODHA", "LTIM",
    "LTTS", "MARICO", "MOTHERSON", "MUTHOOTFIN", "NAUKRI",
    "NMDC", "OFSS", "PIDILITIND", "PIIND", "PNB",
    "POLYCAB", "RECLTD", "SAIL", "SHREECEM", "SIEMENS",
    "SRF", "TATAPOWER", "TORNTPHARM", "TVSMOTOR", "VEDL",
    "VOLTAS", "ZOMATO",
]

NIFTY_100 = NIFTY_50 + NIFTY_NEXT_50


# ── Universe builder with index constraint ───────────────────────────────

def compute_weekly_universes_constrained(df_daily, config, allowed_set):
    """Same as compute_weekly_universes but pre-filters to allowed_set."""
    df_filtered = df_daily.filter(pl.col("instrument").is_in(list(allowed_set)))
    return compute_weekly_universes(df_filtered, config)


def run_one(df_daily, df_minute, weekly_universes, config, label):
    print(f"\n--- Running {label} (slip={config['slippage_bps']}bps) ---")
    t0 = time.time()
    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    margin = float(config["initial_capital"])
    all_trades = []
    equity = [(start_ep, margin)]
    universe_sizes = []

    unique_dates = sorted(df_minute.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") < end_ep)
    )["date_key"].unique().to_list())

    for i, date_key in enumerate(unique_dates):
        day_df = df_minute.filter(pl.col("date_key") == date_key)
        if day_df.is_empty():
            continue
        day_epoch = date_key * SECONDS_IN_ONE_DAY
        day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        active_universe = weekly_universes.get(day_str, [])
        if not active_universe:
            continue
        fmp_syms = [u.replace("NSE:", "") + ".NS" for u in active_universe]
        universe_sizes.append(len(fmp_syms))
        cfg = {**config}
        trades, pnl = simulate_orb_day(day_df, fmp_syms, cfg, margin)
        if trades:
            margin += pnl
            equity.append((day_epoch, margin))
            all_trades.extend(trades)
        if (i + 1) % 200 == 0:
            print(f"    Day {i+1}/{len(unique_dates)} ({day_str}): margin={margin:,.0f}, trades={len(all_trades)}")

    if universe_sizes:
        print(f"  Universe size after filter — avg: {sum(universe_sizes)/len(universe_sizes):.1f}, "
              f"min: {min(universe_sizes)}, max: {max(universe_sizes)}")
    print(f"  Done in {time.time()-t0:.0f}s")
    return all_trades, equity


def summarize(label, slip, trades, equity, initial_capital):
    m = metrics(equity, trades, initial_capital)
    per_year = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in trades:
        y = t["year"]
        per_year[y]["pnl"] += t["pnl"]
        per_year[y]["trades"] += 1
        if t["pnl"] > 0:
            per_year[y]["wins"] += 1
    py = {y: {
        "trades": v["trades"],
        "pnl_pct_of_initial": round(v["pnl"] / initial_capital * 100, 2),
        "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
    } for y, v in sorted(per_year.items())}
    exit_dist = defaultdict(int)
    for t in trades:
        exit_dist[t["exit_type"]] += 1

    print(f"\n  RESULT [{label}, slip={slip}bps]: CAGR={m['cagr']}% MDD={m['mdd']}% "
          f"Sharpe={m['sharpe']} Calmar={m['calmar']} Trades={m['trades']} "
          f"WR={m['win_rate']}%")
    return {"metrics": m, "per_year": py, "exit_types": dict(exit_dist)}


def main():
    print("="*70)
    print("PHASE B (Nifty 50/100 universe) — survivorship-bias mitigation")
    print("="*70)

    end_ep = int(datetime.strptime(CONFIG["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    start_ep = int(datetime.strptime(CONFIG["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    prefetch_ep = start_ep - 200 * SECONDS_IN_ONE_DAY

    print("\nLoading daily and minute data...")
    df_daily = load_daily_data(prefetch_ep, end_ep)
    df_minute = load_minute_data(symbols=None)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64).alias("dateEpoch")])

    # Coverage check
    daily_syms = set(df_daily["instrument"].unique().to_list())
    minute_syms_fmp = set(df_minute["symbol"].unique().to_list())
    minute_syms_bare = {s.replace(".NS", "") for s in minute_syms_fmp}

    n50_in_daily = [s for s in NIFTY_50 if s in daily_syms]
    n50_in_minute = [s for s in NIFTY_50 if s in minute_syms_bare]
    n100_in_daily = [s for s in NIFTY_100 if s in daily_syms]
    n100_in_minute = [s for s in NIFTY_100 if s in minute_syms_bare]

    print(f"\nNifty 50  coverage: daily={len(n50_in_daily)}/50, minute={len(n50_in_minute)}/50")
    print(f"Nifty 100 coverage: daily={len(n100_in_daily)}/100, minute={len(n100_in_minute)}/100")

    missing_n50 = set(NIFTY_50) - daily_syms
    if missing_n50:
        print(f"  Nifty 50 missing from daily: {sorted(missing_n50)}")
    missing_n50_min = set(NIFTY_50) - minute_syms_bare
    if missing_n50_min:
        print(f"  Nifty 50 missing from minute: {sorted(missing_n50_min)}")

    all_results = {}

    for label, allowed in [("Nifty50", set(NIFTY_50)), ("Nifty100", set(NIFTY_100))]:
        print(f"\n{'='*70}")
        print(f"VARIANT: {label}")
        print(f"{'='*70}")
        weekly = compute_weekly_universes_constrained(df_daily, CONFIG, allowed)
        nonempty = sum(1 for v in weekly.values() if v)
        print(f"  {nonempty}/{len(weekly)} day-keys mapped to a universe")

        for slip in CONFIG["slippage_bps_list"]:
            cfg = {**CONFIG, "slippage_bps": slip}
            trades, equity = run_one(df_daily, df_minute, weekly, cfg, label)
            res = summarize(label, slip, trades, equity, CONFIG["initial_capital"])
            all_results[f"{label}_slip{slip}"] = res

    with open("/home/swas/backtester/phase_b_nifty.json", "w") as f:
        json.dump(all_results, f, indent=2)

    # Comparison summary
    print(f"\n{'='*70}")
    print("PHASE B (Nifty universes) — SUMMARY")
    print(f"{'='*70}")
    print(f"\n{'Variant':<20} {'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 80)
    # Reference: previous Phase B (unconstrained)
    print(f"{'Phase B (top 1500+)':<20} {0:>5d} {7.6:>7.2f}% {-13.5:>7.2f}% {0.564:>8.3f} {1.329:>8.3f} {3470:>7d} {37.4:>5.1f}%  (reference, biased)")
    print(f"{'Phase B (top 1500+)':<20} {3:>5d} {-2.77:>7.2f}% {-25.85:>7.2f}% {-0.107:>8.3f} {-0.465:>8.3f} {3470:>7d} {36.0:>5.1f}%  (reference, biased)")
    for key, r in all_results.items():
        m = r["metrics"]
        label, _, slip = key.partition("_slip")
        print(f"{label:<20} {int(slip):>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% {m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    # Per-year comparison
    print(f"\nPer-year breakdown:")
    for key, r in all_results.items():
        print(f"\n{key}:")
        for y, v in r["per_year"].items():
            print(f"  {y}: {v['trades']:>4d} trades, PnL={v['pnl_pct_of_initial']:>+6.2f}%, WR={v['win_rate']}%")
        print(f"  Exit types: {r['exit_types']}")

    # Decision summary
    print(f"\n{'='*70}")
    print("INTERPRETATION")
    print(f"{'='*70}")
    n50_0 = all_results["Nifty50_slip0"]["metrics"]["cagr"]
    n100_0 = all_results["Nifty100_slip0"]["metrics"]["cagr"]
    n50_3 = all_results["Nifty50_slip3"]["metrics"]["cagr"]
    n100_3 = all_results["Nifty100_slip3"]["metrics"]["cagr"]
    print(f"  Nifty 50  (0bps): CAGR={n50_0}%   vs biased baseline 7.6%   diff={n50_0-7.6:+.1f}pp")
    print(f"  Nifty 100 (0bps): CAGR={n100_0}%  vs biased baseline 7.6%   diff={n100_0-7.6:+.1f}pp")
    if n50_0 > 0 and n100_0 > 0:
        print(f"  ✓ Survivorship not the dominant explanation. Edge survives in clean universe.")
    elif n50_0 < 0 and n100_0 < 0:
        print(f"  ✗ Edge collapses in clean universe → biased baseline was mostly survivorship.")
    else:
        print(f"  ~ Mixed: one passes, one fails. Re-examine the methodology.")


if __name__ == "__main__":
    main()

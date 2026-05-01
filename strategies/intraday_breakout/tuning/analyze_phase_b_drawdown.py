"""Phase B drawdown deep-dive: equity curve, top drawdowns, monthly returns,
distribution of daily returns. Re-runs Phase B at 0bps slippage and dumps
day-level data for analysis.
"""
import sys, json, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import load_daily_data, load_minute_data, SECONDS_IN_ONE_DAY
from run_phase_b_momentum_filter import (
    compute_weekly_universes, simulate_orb_day, metrics, CONFIG,
)
import polars as pl
from datetime import datetime, timezone
from collections import defaultdict
import statistics


def drawdown_analysis(equity):
    """Compute peak-to-trough drawdowns; return ranked list."""
    if len(equity) < 2:
        return []
    peak = equity[0][1]
    peak_idx = 0
    drawdowns = []
    in_dd = False
    dd_start_eq = None
    dd_start_idx = None
    for i, (ep, eq) in enumerate(equity):
        if eq > peak:
            if in_dd:
                trough_idx, trough_eq = min(
                    enumerate(equity[dd_start_idx:i+1], start=dd_start_idx),
                    key=lambda x: x[1][1]
                )
                trough_eq = trough_eq[1]
                drawdowns.append({
                    "start_date": datetime.fromtimestamp(equity[dd_start_idx][0], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "trough_date": datetime.fromtimestamp(equity[trough_idx][0], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "recovery_date": datetime.fromtimestamp(equity[i][0], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "peak_equity": round(dd_start_eq, 2),
                    "trough_equity": round(trough_eq, 2),
                    "depth_pct": round((trough_eq - dd_start_eq) / dd_start_eq * 100, 2),
                    "duration_days": (equity[i][0] - equity[dd_start_idx][0]) // SECONDS_IN_ONE_DAY,
                })
                in_dd = False
            peak = eq
            peak_idx = i
            dd_start_eq = eq
            dd_start_idx = i
        elif eq < peak:
            if not in_dd:
                in_dd = True
                dd_start_eq = peak
                dd_start_idx = peak_idx
    # Close out an unrecovered drawdown
    if in_dd:
        trough_idx, trough_eq = min(
            enumerate(equity[dd_start_idx:], start=dd_start_idx),
            key=lambda x: x[1][1]
        )
        trough_eq = trough_eq[1]
        drawdowns.append({
            "start_date": datetime.fromtimestamp(equity[dd_start_idx][0], tz=timezone.utc).strftime("%Y-%m-%d"),
            "trough_date": datetime.fromtimestamp(equity[trough_idx][0], tz=timezone.utc).strftime("%Y-%m-%d"),
            "recovery_date": "(not recovered)",
            "peak_equity": round(dd_start_eq, 2),
            "trough_equity": round(trough_eq, 2),
            "depth_pct": round((trough_eq - dd_start_eq) / dd_start_eq * 100, 2),
            "duration_days": (equity[-1][0] - equity[dd_start_idx][0]) // SECONDS_IN_ONE_DAY,
        })
    return sorted(drawdowns, key=lambda d: d["depth_pct"])


def monthly_returns(equity):
    """Group equity by month, compute month-end - month-start return."""
    by_month = defaultdict(list)
    for ep, eq in equity:
        ym = datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m")
        by_month[ym].append(eq)
    out = []
    prev_end = None
    for ym in sorted(by_month):
        eqs = by_month[ym]
        start = prev_end if prev_end is not None else eqs[0]
        end = eqs[-1]
        out.append({
            "month": ym,
            "start": round(start, 2),
            "end": round(end, 2),
            "return_pct": round((end - start) / start * 100, 2) if start else 0,
        })
        prev_end = end
    return out


def daily_returns(equity):
    rets = []
    for i in range(1, len(equity)):
        prev_eq = equity[i-1][1]
        cur_eq = equity[i][1]
        if prev_eq > 0:
            rets.append((cur_eq - prev_eq) / prev_eq * 100)
    return rets


def main():
    print("="*70)
    print("PHASE B DRAWDOWN ANALYSIS — 0bps slippage baseline")
    print("="*70)

    end_ep = int(datetime.strptime(CONFIG["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    start_ep = int(datetime.strptime(CONFIG["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    prefetch_ep = start_ep - 200 * SECONDS_IN_ONE_DAY

    print("Loading daily and minute data...")
    df_daily = load_daily_data(prefetch_ep, end_ep)
    df_minute = load_minute_data(symbols=None)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64).alias("dateEpoch")])

    print("Building weekly universes...")
    weekly_universes = compute_weekly_universes(df_daily, CONFIG)

    print("Simulating Phase B at 0bps...")
    cfg = {**CONFIG, "slippage_bps": 0}
    margin = float(cfg["initial_capital"])
    all_trades = []
    equity = [(start_ep, margin)]

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
        trades, pnl = simulate_orb_day(day_df, fmp_syms, cfg, margin)
        if trades:
            margin += pnl
            equity.append((day_epoch, margin))
            all_trades.extend(trades)

    m = metrics(equity, all_trades, cfg["initial_capital"])
    print(f"\nOverall: CAGR={m['cagr']}% MDD={m['mdd']}% Sharpe={m['sharpe']} "
          f"Calmar={m['calmar']} Trades={m['trades']} WR={m['win_rate']}%")

    # Drawdown analysis
    dds = drawdown_analysis(equity)
    print(f"\nTotal drawdowns: {len(dds)}")
    print(f"\nTop 10 worst drawdowns:")
    print(f"{'Start':<12} {'Trough':<12} {'Recovery':<12} {'Depth %':>8} {'Days':>6} {'Peak Rs':>14} {'Trough Rs':>14}")
    for d in dds[:10]:
        print(f"{d['start_date']:<12} {d['trough_date']:<12} {d['recovery_date']:<12} "
              f"{d['depth_pct']:>8.2f} {int(d['duration_days']):>6d} "
              f"{d['peak_equity']:>14,.0f} {d['trough_equity']:>14,.0f}")

    # Monthly returns
    mrets = monthly_returns(equity)
    print(f"\nMonthly returns ({len(mrets)} months):")
    print(f"{'Month':<10} {'Start Rs':>14} {'End Rs':>14} {'Return %':>10}")
    for mr in mrets:
        print(f"{mr['month']:<10} {mr['start']:>14,.0f} {mr['end']:>14,.0f} {mr['return_pct']:>10.2f}")

    pos_months = sum(1 for x in mrets if x["return_pct"] > 0)
    neg_months = sum(1 for x in mrets if x["return_pct"] < 0)
    print(f"\n  Positive months: {pos_months}/{len(mrets)} ({pos_months/len(mrets)*100:.1f}%)")
    print(f"  Negative months: {neg_months}/{len(mrets)}")
    if mrets:
        print(f"  Best month:  {max(mrets, key=lambda x: x['return_pct'])}")
        print(f"  Worst month: {min(mrets, key=lambda x: x['return_pct'])}")

    # Daily returns distribution
    drets = daily_returns(equity)
    print(f"\nDaily returns ({len(drets)} active days):")
    print(f"  Mean:    {statistics.mean(drets):.3f}%")
    print(f"  Median:  {statistics.median(drets):.3f}%")
    print(f"  StDev:   {statistics.stdev(drets):.3f}%")
    print(f"  Min:     {min(drets):.3f}%  (worst single day)")
    print(f"  Max:     {max(drets):.3f}%  (best single day)")
    sorted_drets = sorted(drets)
    print(f"  P5:      {sorted_drets[int(len(drets)*0.05)]:.3f}%")
    print(f"  P95:     {sorted_drets[int(len(drets)*0.95)]:.3f}%")

    # Equity curve sample
    print(f"\nEquity curve checkpoints (every 6 months):")
    sample_idx = list(range(0, len(equity), max(1, len(equity)//8)))
    for idx in sample_idx:
        ep, eq = equity[idx]
        d = datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {d}: Rs {eq:,.0f}  ({(eq/1_000_000-1)*100:+.1f}% vs start)")

    # Save full data
    out = {
        "metrics": m,
        "equity_curve": [{"date": datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%d"),
                          "epoch": ep, "equity": eq} for ep, eq in equity],
        "drawdowns": dds,
        "monthly_returns": mrets,
        "daily_return_stats": {
            "mean": round(statistics.mean(drets), 4),
            "median": round(statistics.median(drets), 4),
            "stdev": round(statistics.stdev(drets), 4),
            "min": round(min(drets), 4),
            "max": round(max(drets), 4),
            "p5": round(sorted_drets[int(len(drets)*0.05)], 4),
            "p95": round(sorted_drets[int(len(drets)*0.95)], 4),
        },
        "n_active_days": len(drets),
        "positive_months_pct": round(pos_months/len(mrets)*100, 1),
    }
    with open("/home/swas/backtester/phase_b_drawdown_analysis.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved: /home/swas/backtester/phase_b_drawdown_analysis.json")


if __name__ == "__main__":
    main()

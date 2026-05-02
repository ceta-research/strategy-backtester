"""US Intraday Gap-Fade Strategy on QQQ (+ optional large-cap tech basket).

Hypothesis: Opening gaps (overnight move from prev close to today's open)
tend to fade within the first 60-90 minutes of US session. Documented in
academic literature (Caginalp 1998, Cooper 2004) and widely used by prop
traders.

Strategy:
  - Compute gap = (today_open - prev_close) / prev_close
  - If gap > +min_gap_pct → SHORT at entry_time, fade toward prev close
  - If gap < -min_gap_pct → LONG at entry_time, fade toward prev close
  - Target: gap × target_fill_pct (e.g., 50% of gap filled)
  - Stop: entry ± gap × stop_mult (e.g., 1.5× the gap size beyond entry)
  - Time exit: close position at max_hold_minutes or eod_exit_minute

Position sizing: 1 trade/day, 100% capital deployed (typical prop-firm style).
Cost model: fixed $0.005/share (IB-level, negligible for ETF with ~$450 price).

Data: FMP minute bars for QQQ (NASDAQ), 2020-01-02 to 2026-04-30.
In-sample: 2020-2023. OOS: 2024-2026.
"""
import sys, json, time
from datetime import datetime, timezone
from collections import defaultdict
import statistics
import numpy as np
import polars as pl

sys.path.insert(0, "/home/swas/backtester")

# ── Config ───────────────────────────────────────────────────────────────

CONFIGS = {
    "base": {
        "label": "Gap Fade v0 (QQQ, 50% fill target)",
        "symbols": ["QQQ"],
        "start_date": "2020-03-01",  # skip Jan-Feb 2020 (COVID onset noise)
        "end_date": "2026-04-30",
        "in_sample_end": "2023-12-31",
        "initial_capital": 100_000,   # prop-firm style
        "min_gap_pct": 0.3,           # minimum gap to trade (30bps)
        "max_gap_pct": 3.0,           # skip monster gaps (news/earnings)
        "entry_delay_minutes": 5,     # enter 5min after open (let dust settle)
        "target_fill_pct": 0.50,      # target = 50% of gap filled
        "stop_mult": 1.5,             # stop = 1.5x gap beyond entry
        "max_hold_minutes": 90,       # close after 90min if neither hit
        "eod_exit_minute": 955,       # 15:55 ET hard cutoff
        "cost_per_share": 0.005,      # USD per share RT (IB tiered)
    },
    "tight": {
        "label": "Gap Fade Tight (30% fill, 1.0x stop, 60min)",
        "symbols": ["QQQ"],
        "start_date": "2020-03-01",
        "end_date": "2026-04-30",
        "in_sample_end": "2023-12-31",
        "initial_capital": 100_000,
        "min_gap_pct": 0.3,
        "max_gap_pct": 3.0,
        "entry_delay_minutes": 5,
        "target_fill_pct": 0.30,
        "stop_mult": 1.0,
        "max_hold_minutes": 60,
        "eod_exit_minute": 955,
        "cost_per_share": 0.005,
    },
    "aggressive": {
        "label": "Gap Fade Aggressive (70% fill, 2.0x stop, 120min)",
        "symbols": ["QQQ"],
        "start_date": "2020-03-01",
        "end_date": "2026-04-30",
        "in_sample_end": "2023-12-31",
        "initial_capital": 100_000,
        "min_gap_pct": 0.2,
        "max_gap_pct": 4.0,
        "entry_delay_minutes": 5,
        "target_fill_pct": 0.70,
        "stop_mult": 2.0,
        "max_hold_minutes": 120,
        "eod_exit_minute": 955,
        "cost_per_share": 0.005,
    },
}

# US market hours (ET, stored as "local-time-labeled-UTC" in FMP)
US_OPEN_MINUTE = 570    # 09:30 ET
US_CLOSE_MINUTE = 960   # 16:00 ET


# ── Data loading ─────────────────────────────────────────────────────────

def load_us_minute(symbols: list[str]) -> pl.DataFrame:
    """Load US minute bars from FMP parquet on prod."""
    import glob
    t0 = time.time()
    base = "/opt/insydia/data/data_source=fmp/tick_data/stock/granularity=1min"
    target_set = set(symbols)
    dfs = []

    for exchange in ["NYSE", "NASDAQ"]:
        path = f"{base}/exchange={exchange}/"
        files = sorted(glob.glob(path + "*.parquet"))
        for f in files:
            df = pl.scan_parquet(f).filter(
                pl.col("symbol").is_in(list(target_set))
            ).collect()
            if df.height > 0:
                dfs.append(df)

    if not dfs:
        raise FileNotFoundError(f"No data found for {symbols}")

    full = pl.concat(dfs)
    full = full.with_columns([
        ((pl.col("dateEpoch") % 86400) // 60).cast(pl.Int32).alias("bar_minute"),
        (pl.col("dateEpoch") // 86400).cast(pl.Int32).alias("date_key"),
    ]).filter(
        (pl.col("bar_minute") >= US_OPEN_MINUTE)
        & (pl.col("bar_minute") < US_CLOSE_MINUTE)
    ).sort(["symbol", "dateEpoch"])

    elapsed = round(time.time() - t0, 1)
    print(f"  Loaded: {full.height:,} bars, {full['symbol'].n_unique()} symbols, "
          f"{full['date_key'].n_unique()} days ({elapsed}s)")
    return full


# ── Gap-fade simulator ───────────────────────────────────────────────────

def simulate_gap_fade(df: pl.DataFrame, config: dict) -> tuple[list, list]:
    """Run gap-fade strategy on daily minute data.

    Returns (trades, equity_curve).
    """
    min_gap = config["min_gap_pct"] / 100
    max_gap = config["max_gap_pct"] / 100
    entry_delay = config["entry_delay_minutes"]
    target_fill = config["target_fill_pct"]
    stop_mult = config["stop_mult"]
    max_hold = config["max_hold_minutes"]
    eod_exit = config["eod_exit_minute"]
    cost_per_share = config["cost_per_share"]

    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    # Filter to date range
    df_range = df.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") <= end_ep)
    )

    # Get unique trading days
    unique_days = sorted(df_range["date_key"].unique().to_list())
    print(f"  Trading days in range: {len(unique_days)}")

    margin = float(config["initial_capital"])
    equity = [(start_ep, margin)]
    trades = []
    prev_close = None

    for di, date_key in enumerate(unique_days):
        day_epoch = date_key * 86400
        day_df = df_range.filter(pl.col("date_key") == date_key).sort("bar_minute")
        bars = day_df.to_dicts()
        if len(bars) < 30:
            continue

        # Today's open (first bar's open)
        today_open = bars[0]["open"]
        if not today_open or today_open <= 0:
            prev_close = bars[-1]["close"]
            continue

        # Gap calculation
        if prev_close is None or prev_close <= 0:
            prev_close = bars[-1]["close"]
            continue

        gap = (today_open - prev_close) / prev_close

        # Update prev_close for next day (using today's last bar)
        day_close = bars[-1]["close"]

        # Check gap size
        if abs(gap) < min_gap or abs(gap) > max_gap:
            prev_close = day_close
            continue

        # Determine direction: fade the gap
        # Gap up → short (expect price to fall back toward prev close)
        # Gap down → long (expect price to rise back toward prev close)
        is_long = gap < 0  # gap down → go long to fade

        # Entry at open + entry_delay minutes
        entry_minute = US_OPEN_MINUTE + entry_delay
        entry_bar = None
        entry_idx = None
        for i, bar in enumerate(bars):
            if bar["bar_minute"] >= entry_minute:
                entry_bar = bar
                entry_idx = i
                break

        if entry_bar is None:
            prev_close = day_close
            continue

        entry_price = entry_bar["open"]  # enter at bar's open
        if not entry_price or entry_price <= 0:
            prev_close = day_close
            continue

        # Compute target and stop based on gap
        gap_dollars = abs(gap) * entry_price  # gap in dollar terms from entry

        if is_long:
            target_price = entry_price + gap_dollars * target_fill
            stop_price = entry_price - gap_dollars * stop_mult
        else:
            target_price = entry_price - gap_dollars * target_fill
            stop_price = entry_price + gap_dollars * stop_mult

        # Walk subsequent bars looking for exit
        exit_price = None
        exit_type = None
        exit_minute = None
        entry_bar_minute = entry_bar["bar_minute"]

        for j in range(entry_idx + 1, len(bars)):
            bar = bars[j]
            bar_high = bar["high"] or entry_price
            bar_low = bar["low"] or entry_price
            bar_close = bar["close"] or entry_price
            bar_minute = bar["bar_minute"]

            # Time-based exits
            minutes_held = bar_minute - entry_bar_minute
            if minutes_held >= max_hold or bar_minute >= eod_exit:
                exit_price = bar_close
                exit_type = "time" if minutes_held >= max_hold else "eod"
                exit_minute = bar_minute
                break

            if is_long:
                # Stop: bar_low <= stop
                if bar_low <= stop_price:
                    exit_price = stop_price
                    exit_type = "stop"
                    exit_minute = bar_minute
                    break
                # Target: bar_high >= target
                if bar_high >= target_price:
                    exit_price = target_price
                    exit_type = "target"
                    exit_minute = bar_minute
                    break
            else:
                # Stop: bar_high >= stop
                if bar_high >= stop_price:
                    exit_price = stop_price
                    exit_type = "stop"
                    exit_minute = bar_minute
                    break
                # Target: bar_low <= target
                if bar_low <= target_price:
                    exit_price = target_price
                    exit_type = "target"
                    exit_minute = bar_minute
                    break

        if exit_price is None:
            # Use last bar close
            exit_price = bars[-1]["close"] or entry_price
            exit_type = "eod_fallback"
            exit_minute = bars[-1]["bar_minute"]

        # PnL calculation
        shares = int(margin / entry_price)
        if shares <= 0:
            prev_close = day_close
            continue

        if is_long:
            pnl_gross = (exit_price - entry_price) * shares
        else:
            pnl_gross = (entry_price - exit_price) * shares

        cost = cost_per_share * shares * 2  # RT
        pnl = pnl_gross - cost
        margin += pnl

        year = datetime.fromtimestamp(day_epoch, tz=timezone.utc).year
        trades.append({
            "date_key": date_key,
            "year": year,
            "gap_pct": round(gap * 100, 4),
            "direction": "long" if is_long else "short",
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "target_price": round(target_price, 2),
            "stop_price": round(stop_price, 2),
            "exit_type": exit_type,
            "exit_minute": exit_minute,
            "shares": shares,
            "pnl_gross": round(pnl_gross, 2),
            "cost": round(cost, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / config["initial_capital"] * 100, 4),
        })

        equity.append((day_epoch, margin))
        prev_close = day_close

        if (di + 1) % 300 == 0:
            day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"    Day {di+1}/{len(unique_days)} ({day_str}): "
                  f"margin=${margin:,.0f}, trades={len(trades)}")

    return trades, equity


# ── Metrics ──────────────────────────────────────────────────────────────

def compute_metrics(equity, trades, initial_capital, label=""):
    if not equity or len(equity) < 2:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, trades=0, win_rate=0,
                    final=initial_capital, label=label)
    initial = equity[0][1]
    final = equity[-1][1]
    days = (equity[-1][0] - equity[0][0]) / 86400
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
        final=round(final, 2), label=label,
    )


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("US INTRADAY GAP-FADE — QQQ")
    print("=" * 70)

    # Load data once
    all_syms = set()
    for cfg in CONFIGS.values():
        all_syms.update(cfg["symbols"])
    print(f"\nLoading minute data for: {sorted(all_syms)}")
    df = load_us_minute(list(all_syms))

    all_results = {}
    for variant, config in CONFIGS.items():
        print(f"\n{'=' * 70}")
        print(f"VARIANT: {variant} — {config['label']}")
        print(f"{'=' * 70}")

        # Filter to requested symbols
        sym_df = df.filter(pl.col("symbol").is_in(config["symbols"]))
        trades, equity = simulate_gap_fade(sym_df, config)

        # Full-period metrics
        m_full = compute_metrics(equity, trades, config["initial_capital"], "full")

        # OOS metrics (2024-2026)
        in_sample_end_ep = int(datetime.strptime(config["in_sample_end"], "%Y-%m-%d")
                               .replace(tzinfo=timezone.utc).timestamp())
        oos_eq = [(t, v) for (t, v) in equity if t > in_sample_end_ep]
        oos_trades = [t for t in trades if t.get("year", 0) >= 2024]
        if oos_eq:
            is_anchor = next(
                (v for (t, v) in reversed(equity) if t <= in_sample_end_ep),
                config["initial_capital"],
            )
            scale = config["initial_capital"] / is_anchor if is_anchor > 0 else 1
            oos_norm = [(in_sample_end_ep, config["initial_capital"])]
            oos_norm += [(t, v * scale) for (t, v) in oos_eq]
            m_oos = compute_metrics(oos_norm, oos_trades, config["initial_capital"], "OOS 2024-2026")
        else:
            m_oos = None

        # Per-year
        per_year = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
        for t in trades:
            y = t["year"]
            per_year[y]["pnl"] += t["pnl"]
            per_year[y]["trades"] += 1
            if t["pnl"] > 0:
                per_year[y]["wins"] += 1
        py = {y: {
            "trades": v["trades"],
            "pnl_pct": round(v["pnl"] / config["initial_capital"] * 100, 2),
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
        } for y, v in sorted(per_year.items())}

        # Exit types
        exits = defaultdict(int)
        for t in trades:
            exits[t["exit_type"]] += 1

        all_results[variant] = {
            "metrics_full": m_full, "metrics_oos": m_oos,
            "per_year": py, "exit_types": dict(exits),
        }

        print(f"\n  FULL (IS-biased): CAGR={m_full['cagr']}% MDD={m_full['mdd']}% "
              f"Sharpe={m_full['sharpe']} Calmar={m_full['calmar']} "
              f"Trades={m_full['trades']} WR={m_full['win_rate']}%")
        if m_oos:
            print(f"  OOS 2024-2026:    CAGR={m_oos['cagr']}% MDD={m_oos['mdd']}% "
                  f"Sharpe={m_oos['sharpe']} Calmar={m_oos['calmar']} "
                  f"Trades={m_oos['trades']} WR={m_oos['win_rate']}%")

    # Save
    out_path = "/home/swas/backtester/us_gap_fade_v0.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")

    # Summary table
    print(f"\n{'=' * 70}")
    print("US GAP-FADE — SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n{'Variant':<12} {'Period':<12} {'CAGR':>7} {'MDD':>7} {'Calmar':>7} "
          f"{'Sharpe':>7} {'Trades':>6} {'WR':>5}")
    print("-" * 70)
    for variant, r in all_results.items():
        m = r["metrics_full"]
        print(f"{variant:<12} {'Full':<12} {m['cagr']:>6.1f}% {m['mdd']:>6.1f}% "
              f"{m['calmar']:>7.3f} {m['sharpe']:>7.3f} {m['trades']:>6d} {m['win_rate']:>4.1f}%")
        m2 = r.get("metrics_oos")
        if m2:
            print(f"{'':<12} {'OOS 24-26':<12} {m2['cagr']:>6.1f}% {m2['mdd']:>6.1f}% "
                  f"{m2['calmar']:>7.3f} {m2['sharpe']:>7.3f} {m2['trades']:>6d} {m2['win_rate']:>4.1f}%")

    print(f"\nPer-year (base variant):")
    for y, v in all_results["base"]["per_year"].items():
        print(f"  {y}: {v['trades']:>4d} trades, PnL={v['pnl_pct']:>+6.2f}%, WR={v['win_rate']}%")
    print(f"  Exits: {all_results['base']['exit_types']}")

    # Decision
    print(f"\n{'=' * 70}")
    print("DECISION")
    print(f"{'=' * 70}")
    best_oos = max(
        (r["metrics_oos"]["cagr"] if r.get("metrics_oos") else -999, k)
        for k, r in all_results.items()
    )
    best_name = best_oos[1]
    m_best = all_results[best_name]["metrics_oos"]
    if m_best and m_best["cagr"] > 10 and abs(m_best["mdd"]) < 3:
        print(f"  ✓ TARGET HIT (10% CAGR / <3% MDD OOS). Variant: {best_name}")
    elif m_best and m_best["cagr"] > 5:
        print(f"  ~ Positive OOS ({m_best['cagr']}% / {m_best['mdd']}% MDD). "
              f"Tune or ensemble to reach 10/3 target.")
    elif m_best and m_best["cagr"] > 0:
        print(f"  ~ Weak positive OOS ({m_best['cagr']}%). Needs work.")
    else:
        cagr = m_best["cagr"] if m_best else "N/A"
        print(f"  ✗ Negative OOS ({cagr}%). US gap-fade doesn't hold on QQQ at this calibration.")


if __name__ == "__main__":
    main()

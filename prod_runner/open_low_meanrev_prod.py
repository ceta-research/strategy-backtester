"""Open-Low Mean Reversion — prod runner.

Strategy: For each Nifty 50 stock per day:
  1. Observe opening price (first bar's open at 09:15)
  2. Place buy limit at `open_price × (1 - dip_threshold)`
  3. If filled, target = opening price (full reversion)
  4. Optional hard stop at `open_price × (1 - stop_pct)`
  5. EOD exit at `eod_exit_minute` (default 15:00, 30min before close)

Filters:
  - Skip if 5-day return < trend_filter_min_return_pct (avoid falling knives)
  - Skip stocks not present in FMP minute data

Position sizing:
  - max_positions per day, equal-weight (margin / max_positions per trade)
  - First N to fill (by fill time) take positions
  - Margin compounds across days

Bug-fix carryover from intraday_breakout_prod:
  - Entry: limit order fills at exact price OR better (gap-down opening)
  - Exit loop: range(entry_idx + 1, ...) to avoid same-bar entry+exit ambiguity
  - Stop / target gap handling: fill at bar_open if it's worse than the trigger

Universe: Nifty 50 (May 2026 constituents, hardcoded — survivorship-clean for
large-caps). See run_phase_b_nifty.py for the same list.

Self-contained. Loads from /opt/insydia/data/ parquet on prod.
"""
import sys, json, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import statistics
import polars as pl

sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_daily_data, load_minute_data,
    nse_intraday_charges, SECONDS_IN_ONE_DAY,
)

OR_OFFSET = 555      # 09:15 IST as bar_minute (timestamps are local-as-UTC)
NSE_CLOSE_MINUTE = 930  # 15:30 IST


# ── Index constituents (May 2026) ────────────────────────────────────────

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


# ── Variants ─────────────────────────────────────────────────────────────

CONFIGS = {
    "R0a": {
        "label": "R0a (no stop, EOD-30 exit, 5d trend filter)",
        "start_date": "2022-01-01",
        "end_date": "2025-12-31",
        "initial_capital": 1_000_000,
        "dip_threshold_pct": 0.7,
        "stop_pct": 0,                          # no stop
        "eod_exit_minute": 900,                 # 15:00 IST (30min before close)
        "max_positions": 5,
        "trend_filter_min_return_pct": -3.0,    # skip if 5d return < -3%
        "slippage_bps_list": [0, 3],
    },
    "R0b": {
        "label": "R0b (stop at 1.5x dip, EOD-30 exit, 5d trend filter)",
        "start_date": "2022-01-01",
        "end_date": "2025-12-31",
        "initial_capital": 1_000_000,
        "dip_threshold_pct": 0.7,
        "stop_pct": 1.05,                        # 1.5 × 0.7%
        "eod_exit_minute": 900,
        "max_positions": 5,
        "trend_filter_min_return_pct": -3.0,
        "slippage_bps_list": [0, 3],
    },
}


# ── 5-day return filter (precompute from daily data) ─────────────────────

def compute_prior_5d_returns(df_daily, allowed_set):
    """Per-day per-symbol 5-day trailing return (close-to-close).

    Returns dict[date_epoch -> dict[bare_symbol -> ret_5d]]
    where date_epoch is the SIGNAL DAY (the day on which we'd act).
    The 5d return is computed using closes from prior 5 trading days,
    so it's known at the start of the signal day.
    """
    df = df_daily.filter(pl.col("instrument").is_in(list(allowed_set)))
    df = df.sort(["instrument", "date_epoch"]).with_columns(
        (pl.col("close") / pl.col("close").shift(5).over("instrument") - 1).alias("ret_5d_prior")
    )
    df = df.filter(pl.col("ret_5d_prior").is_not_null())
    out: dict[int, dict[str, float]] = {}
    # ret_5d_prior on day T uses closes from T-5 to T. We want to know the
    # trend KNOWN at the start of T+1 (the day we'd trade), so map the
    # value to the NEXT trading day's date_epoch.
    # Build per-instrument series, then walk to assign to next trading day.
    per_inst = df.partition_by("instrument", as_dict=True)
    for key, idf in per_inst.items():
        inst = key[0] if isinstance(key, tuple) else key
        rows = idf.sort("date_epoch").to_dicts()
        for i in range(len(rows) - 1):
            next_epoch = rows[i + 1]["date_epoch"]
            out.setdefault(next_epoch, {})[inst] = rows[i]["ret_5d_prior"]
    return out


# ── Per-day simulator ────────────────────────────────────────────────────

def simulate_meanrev_day(day_minute_df, universe_fmp_syms, prior_5d_map, config, margin):
    """Open-low mean reversion: one day, given universe, margin available.

    Returns (trades, day_pnl).
    """
    dip = config["dip_threshold_pct"] / 100
    stop = config.get("stop_pct", 0) / 100
    eod = config["eod_exit_minute"]
    max_pos = config["max_positions"]
    slip = config["slippage_bps"]
    trend_min = config.get("trend_filter_min_return_pct", -100) / 100

    candidates = []

    for sym in universe_fmp_syms:
        bare_sym = sym.replace(".NS", "")

        # Trend filter (only if data available — if not, allow trade)
        ret_5d = prior_5d_map.get(bare_sym)
        if ret_5d is not None and ret_5d < trend_min:
            continue

        bars = day_minute_df.filter(pl.col("symbol") == sym).sort("bar_minute").to_dicts()
        if len(bars) < 10:
            continue

        first_bar = bars[0]
        if first_bar.get("bar_minute") != OR_OFFSET:
            continue  # data quality: missing first bar of session

        open_price = first_bar.get("open")
        if not open_price or open_price <= 0:
            continue

        limit_price = open_price * (1 - dip)
        target_price = open_price
        stop_price = open_price * (1 - stop) if stop > 0 else None

        # Find fill: first bar where bar_low <= limit_price (incl. first bar)
        fill_idx = None
        fill_price = None
        for i, bar in enumerate(bars):
            bar_minute = bar.get("bar_minute", OR_OFFSET)
            if bar_minute >= eod:
                break  # too late to enter
            bar_low = bar.get("low")
            if bar_low is None:
                continue
            if bar_low <= limit_price:
                # Gap-down opening: fill at bar_open (better than limit)
                bar_open = bar.get("open") or limit_price
                fill_price = min(bar_open, limit_price)
                fill_idx = i
                break

        if fill_idx is None:
            continue

        # Entry slippage (limit fills are mostly slip-free, but model a small
        # cost for queue position — keeps results conservative)
        fill_with_slip = fill_price * (1 + slip / 10000)

        candidates.append({
            "sym": sym,
            "fill_minute": bars[fill_idx]["bar_minute"],
            "fill_idx": fill_idx,
            "fill_price": fill_with_slip,
            "open_price": open_price,
            "target_price": target_price,
            "stop_price": stop_price,
            "bars": bars,
        })

    if not candidates:
        return [], 0.0

    # First N to fill take positions
    candidates.sort(key=lambda c: (c["fill_minute"], c["sym"]))
    selected = candidates[:max_pos]
    order_value = margin / max_pos

    trades = []
    pnl_sum = 0.0

    for c in selected:
        bars = c["bars"]
        idx = c["fill_idx"]
        entry_price = c["fill_price"]
        target = c["target_price"]
        stop_p = c["stop_price"]

        exit_price = exit_type = None

        # range(idx + 1, ...) — avoid same-bar entry+exit ambiguity (Bug 2)
        for j in range(idx + 1, len(bars)):
            bar = bars[j]
            bar_high = bar.get("high") or entry_price
            bar_low = bar.get("low") or entry_price
            bar_open = bar.get("open") or entry_price
            bar_close = bar.get("close") or entry_price
            bar_minute = bar.get("bar_minute", OR_OFFSET)

            # Stop check first (conservative on tie)
            if stop_p is not None and bar_low <= stop_p:
                # Gap-down: fill at bar_open if worse than stop
                fill = min(stop_p, bar_open) if bar_open < stop_p else stop_p
                exit_price = fill
                exit_type = "stop"
                break

            # Target check
            if bar_high >= target:
                # Gap-up: fill at bar_open if better than target
                fill = max(target, bar_open) if bar_open > target else target
                exit_price = fill
                exit_type = "target"
                break

            # EOD-30 exit
            if bar_minute >= eod:
                exit_price = bar_close
                exit_type = "eod_close"
                break

        # Fallback: last bar's close (shouldn't happen with eod check above)
        if exit_price is None and bars:
            exit_price = bars[-1].get("close") or entry_price
            exit_type = "eod_close_fallback"

        if not exit_price or exit_price <= 0:
            continue

        # Exit slippage applied uniformly across all exit types (queue
        # position on limit fills, market-impact on stop/EOD). Matches the
        # entry-side slippage cost — consistent treatment.
        exit_price = exit_price * (1 - slip / 10000)

        charges = nse_intraday_charges(order_value)
        pnl = (exit_price - entry_price) / entry_price * order_value - charges
        pnl_sum += pnl

        first_dateep = bars[0].get("dateEpoch")
        year = datetime.fromtimestamp(first_dateep, tz=timezone.utc).year if first_dateep else 0

        trades.append({
            "symbol": c["sym"],
            "open_price": round(c["open_price"], 2),
            "fill_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "fill_minute": c["fill_minute"],
            "exit_type": exit_type,
            "pnl": round(pnl, 2),
            "year": year,
        })

    return trades, pnl_sum


# ── Metrics ──────────────────────────────────────────────────────────────

def metrics(equity, trades, initial_capital):
    if not equity or len(equity) < 2:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, trades=0, win_rate=0,
                    final=initial_capital)
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


# ── Driver ───────────────────────────────────────────────────────────────

def run_one(df_minute, prior_5d_map, config, label):
    print(f"\n--- {label} (slip={config['slippage_bps']}bps) ---")
    t0 = time.time()
    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    margin = float(config["initial_capital"])
    all_trades = []
    equity = [(start_ep, margin)]

    fmp_syms = [s + ".NS" for s in NIFTY_50]

    # Filter df_minute to date range
    df_in_range = df_minute.filter(
        (pl.col("dateEpoch") >= start_ep) & (pl.col("dateEpoch") < end_ep)
    )
    unique_dates = sorted(df_in_range["date_key"].unique().to_list())
    print(f"  Trading days in range: {len(unique_dates)}")

    for i, date_key in enumerate(unique_dates):
        day_df = df_in_range.filter(pl.col("date_key") == date_key)
        if day_df.is_empty():
            continue
        day_epoch = date_key * SECONDS_IN_ONE_DAY
        day_5d = prior_5d_map.get(day_epoch, {})
        trades, pnl = simulate_meanrev_day(day_df, fmp_syms, day_5d, config, margin)
        if trades:
            margin += pnl
            equity.append((day_epoch, margin))
            all_trades.extend(trades)
        if (i + 1) % 200 == 0:
            day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
            print(f"    Day {i+1}/{len(unique_dates)} ({day_str}): margin={margin:,.0f}, trades={len(all_trades)}")

    print(f"  Done in {time.time()-t0:.0f}s, {len(all_trades)} trades, final margin Rs {margin:,.0f}")
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
          f"WR={m['win_rate']}% Final=Rs {m['final']:,.0f}")
    return {"metrics": m, "per_year": py, "exit_types": dict(exit_dist)}


def main():
    print("=" * 70)
    print("OPEN-LOW MEAN REVERSION — Nifty 50, R0 baseline")
    print("=" * 70)

    end_ep = int(datetime.strptime("2025-12-31", "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    start_ep = int(datetime.strptime("2022-01-01", "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    prefetch_ep = start_ep - 30 * SECONDS_IN_ONE_DAY  # only need 5d return; small buffer

    print("\nLoading daily data (for 5d trend filter)...")
    df_daily = load_daily_data(prefetch_ep, end_ep)

    print("\nLoading minute data...")
    df_minute = load_minute_data(symbols=None)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64).alias("dateEpoch")])

    # Coverage check
    daily_syms = set(df_daily["instrument"].unique().to_list())
    minute_syms_bare = {s.replace(".NS", "") for s in df_minute["symbol"].unique().to_list()}
    n50_in_daily = [s for s in NIFTY_50 if s in daily_syms]
    n50_in_minute = [s for s in NIFTY_50 if s in minute_syms_bare]
    print(f"\nNifty 50 coverage: daily={len(n50_in_daily)}/50, minute={len(n50_in_minute)}/50")
    missing_d = set(NIFTY_50) - daily_syms
    missing_m = set(NIFTY_50) - minute_syms_bare
    if missing_d:
        print(f"  Missing from daily: {sorted(missing_d)}")
    if missing_m:
        print(f"  Missing from minute: {sorted(missing_m)}")

    print("\nComputing 5-day prior returns (filter)...")
    prior_5d_map = compute_prior_5d_returns(df_daily, set(NIFTY_50))
    print(f"  5d return map: {len(prior_5d_map)} signal days")

    all_results = {}
    for variant_name, config in CONFIGS.items():
        print(f"\n{'=' * 70}")
        print(f"VARIANT: {variant_name} — {config['label']}")
        print(f"{'=' * 70}")

        for slip in config["slippage_bps_list"]:
            cfg = {**config, "slippage_bps": slip}
            trades, equity = run_one(df_minute, prior_5d_map, cfg, variant_name)
            res = summarize(variant_name, slip, trades, equity, config["initial_capital"])
            all_results[f"{variant_name}_slip{slip}"] = res

    out_path = "/home/swas/backtester/open_low_meanrev_r0.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved: {out_path}")

    # Comparison table
    print(f"\n{'=' * 70}")
    print("OPEN-LOW MEAN REVERSION — SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n{'Variant':<12} {'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} "
          f"{'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 70)
    for key, r in all_results.items():
        m = r["metrics"]
        label, _, slip = key.partition("_slip")
        print(f"{label:<12} {int(slip):>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% "
              f"{m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    print("\nPer-year breakdown:")
    for key, r in all_results.items():
        print(f"\n{key}:")
        for y, v in r["per_year"].items():
            print(f"  {y}: {v['trades']:>4d} trades, "
                  f"PnL={v['pnl_pct_of_initial']:>+6.2f}%, WR={v['win_rate']}%")
        print(f"  Exit types: {r['exit_types']}")

    # Decision gate (per-variant — gate is "CAGR > 3% AND MDD < 5%" at 0bps)
    print(f"\n{'=' * 70}")
    print("DECISION GATE — per variant @ 0bps slippage")
    print(f"  Pass: CAGR > 3% AND |MDD| < 5%")
    print(f"{'=' * 70}")
    any_pass = False
    any_positive = False
    for variant in ("R0a", "R0b"):
        m0 = all_results[f"{variant}_slip0"]["metrics"]
        m3 = all_results[f"{variant}_slip3"]["metrics"]
        passed = (m0["cagr"] > 3) and (abs(m0["mdd"]) < 5)
        marker = "✓ PASS" if passed else ("~ pos" if m0["cagr"] > 0 else "✗ fail")
        print(f"  {variant} {marker}:  0bps CAGR {m0['cagr']:>+.2f}% / MDD {m0['mdd']:>+.2f}%  |  "
              f"3bps CAGR {m3['cagr']:>+.2f}% / MDD {m3['mdd']:>+.2f}%")
        if passed:
            any_pass = True
        if m0["cagr"] > 0:
            any_positive = True
    print()
    if any_pass:
        print(f"  ✓ At least one variant passes — proceed to parameter sweep.")
    elif any_positive:
        print(f"  ~ Positive edge but below gate. Consider: dip-threshold sweep, "
              f"trend-filter ablation, Nifty 100 expansion.")
    else:
        print(f"  ✗ Negative edge across variants. Open price is not a magnet at this calibration.")
        print(f"    Consider: per-stock rolling dip threshold, different universe, pivot.")


if __name__ == "__main__":
    main()

"""Rolling symbol performance filter.

Instead of blacklisting symbols (overfitting), we use adaptive filtering:
- Track each symbol's gap-up trade P&L over the last N months
- Only trade symbols that have been net positive recently
- Symbols rotate in/out based on recent performance

This is a momentum-based quality filter that adapts as market leadership rotates.
"""
import sys, json, math, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_daily_data, load_minute_data, compute_regime_hysteresis,
    select_universe, compute_daily_signals, simulate_intraday_day,
    nse_intraday_charges, _month_boundaries, SECONDS_IN_ONE_DAY
)
import polars as pl
from datetime import datetime, timezone
from collections import defaultdict

# Base config
START_DATE = "2022-01-01"
END_DATE = "2025-12-31"
INITIAL_CAPITAL = 1000000
PREFETCH_DAYS = 500

base_sim = {
    "target_pct": 1.0,
    "stop_pct": 0.5,
    "trailing_stop_pct": 0,
    "max_entry_bar": 15,
    "max_positions": 5,
    "eod_exit_minute": 925,
    "slippage_bps": 0,
    "entry_mode": "market",
    "require_gap_up": True,
}

start_epoch = int(datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
end_epoch = int(datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
prefetch_epoch = start_epoch - int(PREFETCH_DAYS * 1.5 * SECONDS_IN_ONE_DAY)

# ═══════════════════════════════════════════════════════════════════════════
# Load data (same as prod runner)
# ═══════════════════════════════════════════════════════════════════════════

print("\n--- Loading data ---")
df_daily = load_daily_data(prefetch_epoch, end_epoch)

months = _month_boundaries(START_DATE, END_DATE)
all_universe_symbols = set()
for _, _, month_epoch in months:
    universe = select_universe(df_daily, month_epoch, 50, 500_000_000)
    for inst in universe:
        all_universe_symbols.add(inst.replace("NSE:", "") + ".NS")

df_minute = load_minute_data(symbols=all_universe_symbols)

minute_by_date = {}
for date_key in df_minute["date_key"].unique().to_list():
    minute_by_date[date_key] = df_minute.filter(pl.col("date_key") == date_key)

minute_daily_highs = {}
daily_high_df = (
    df_minute.group_by(["date_key", "symbol"])
    .agg(pl.col("high").max().alias("day_high"))
)
for row in daily_high_df.to_dicts():
    dk = row["date_key"]
    if dk not in minute_daily_highs:
        minute_daily_highs[dk] = {}
    minute_daily_highs[dk][row["symbol"]] = row["day_high"]

# Regime
df_regime = df_daily.with_columns(
    (pl.col("turnover") >= 500_000_000).alias("scanner_pass")
)
bull_epochs = compute_regime_hysteresis(
    df_regime, sma_period=50, entry_threshold=0.4, exit_threshold=0.35
)


def run_with_rolling_filter(lookback_months: int, min_trades: int = 3,
                            min_win_rate: float = 0.0, min_pnl: float = 0.0) -> dict:
    """Run backtest with rolling symbol performance filter.

    Args:
        lookback_months: How many months of history to consider
        min_trades: Minimum trades in lookback to be eligible (avoid cold-start)
        min_win_rate: Minimum win rate in lookback (0 = disabled)
        min_pnl: Minimum total P&L in lookback (0 = any positive)

    Logic:
        - First N months: trade all symbols (warmup period)
        - After warmup: only trade symbols with positive recent performance
        - Performance tracked per-symbol across rolling window
    """
    margin = float(INITIAL_CAPITAL)
    all_trades = []
    equity_points = [(start_epoch, margin)]

    # Rolling trade history per symbol: {symbol: [(date, pnl), ...]}
    symbol_history = defaultdict(list)

    all_daily_epochs = sorted(
        df_daily.filter(pl.col("date_epoch") >= start_epoch)["date_epoch"].unique().to_list()
    )
    next_day_map = {all_daily_epochs[i]: all_daily_epochs[i+1]
                    for i in range(len(all_daily_epochs) - 1)}

    for month_start, month_end, month_epoch in months:
        universe = select_universe(df_daily, month_epoch, 50, 500_000_000)
        if not universe:
            continue

        m_start_epoch = int(datetime.strptime(month_start, "%Y-%m-%d")
                            .replace(tzinfo=timezone.utc).timestamp())
        m_end_epoch = int(datetime.strptime(month_end, "%Y-%m-%d")
                          .replace(tzinfo=timezone.utc).timestamp())

        signals, _ = compute_daily_signals(
            df_daily, universe, m_start_epoch, m_end_epoch,
            n_day_high=3, n_day_ma=10, regime_bull_epochs=bull_epochs,
        )
        if not signals:
            continue

        # Determine which symbols pass the rolling filter
        current_month_dt = datetime.strptime(month_start, "%Y-%m-%d")
        months_elapsed = (current_month_dt.year - 2022) * 12 + current_month_dt.month - 1

        allowed_symbols = set()
        if months_elapsed < lookback_months:
            # Warmup: allow all
            allowed_symbols = set(universe)
        else:
            # Filter based on recent performance
            cutoff_epoch = m_start_epoch - lookback_months * 30 * SECONDS_IN_ONE_DAY
            for sym in universe:
                recent = [(d, p) for d, p in symbol_history[sym] if d >= cutoff_epoch]
                if len(recent) < min_trades:
                    # Not enough data: allow (benefit of doubt) or skip
                    # Allow to avoid cold-start bias
                    allowed_symbols.add(sym)
                    continue
                total_pnl = sum(p for _, p in recent)
                wins = sum(1 for _, p in recent if p > 0)
                wr = wins / len(recent)
                if total_pnl > min_pnl and wr >= min_win_rate:
                    allowed_symbols.add(sym)

        for epoch in sorted(signals.keys()):
            next_epoch = next_day_map.get(epoch)
            if next_epoch is None:
                continue

            # Filter eligible instruments by rolling performance
            eligible = [inst for inst in signals[epoch] if inst in allowed_symbols]
            if not eligible:
                continue

            date_key = next_epoch // SECONDS_IN_ONE_DAY
            minute_df = minute_by_date.get(date_key, pl.DataFrame())
            if minute_df.is_empty():
                continue

            signal_date_key = epoch // SECONDS_IN_ONE_DAY
            signal_minute_highs = minute_daily_highs.get(signal_date_key, {})
            sig_highs = {}
            for inst in eligible:
                fmp_sym = inst.replace("NSE:", "") + ".NS"
                if fmp_sym in signal_minute_highs:
                    sig_highs[inst] = signal_minute_highs[fmp_sym]

            day_trades, day_pnl = simulate_intraday_day(
                minute_df, eligible, sig_highs, base_sim, margin
            )

            margin += day_pnl
            next_date_str = datetime.fromtimestamp(next_epoch, tz=timezone.utc).strftime("%Y-%m-%d")

            for t in day_trades:
                t["trade_date"] = next_date_str
                all_trades.append(t)
                # Track performance per symbol
                symbol_history[t["symbol"]].append((next_epoch, t["pnl"]))

            equity_points.append((next_epoch, margin))

    # Compute metrics
    years = 4.0
    cagr = ((margin / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if margin > 0 else -100
    peak = INITIAL_CAPITAL
    max_dd = 0
    for _, eq in equity_points:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak
        if dd < max_dd:
            max_dd = dd
    calmar = cagr / 100 / abs(max_dd) if max_dd != 0 else 0
    win_rate = sum(1 for t in all_trades if t["pnl"] > 0) / len(all_trades) * 100 if all_trades else 0

    return {
        "cagr": round(cagr, 2),
        "mdd": round(max_dd * 100, 2),
        "calmar": round(calmar, 3),
        "trades": len(all_trades),
        "win_rate": round(win_rate, 1),
        "final_margin": round(margin, 0),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Experiments
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  ROLLING SYMBOL PERFORMANCE FILTER")
print("=" * 70)

# Baseline (no filter)
print("\n  --- Baseline (no rolling filter, all symbols) ---")
r_base = run_with_rolling_filter(lookback_months=999, min_trades=0)  # effectively no filter
print("  CAGR=%.2f%% MDD=%.2f%% Calmar=%.3f Trades=%d WR=%.0f%%" % (
    r_base["cagr"], r_base["mdd"], r_base["calmar"], r_base["trades"], r_base["win_rate"]))

# Sweep lookback period
print("\n  --- Lookback Period Sweep (min_trades=3, any positive P&L) ---")
print("  %-12s %7s %7s %7s %6s %5s" % ("Lookback", "CAGR", "MDD", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for lb in [1, 2, 3, 6, 9, 12]:
    r = run_with_rolling_filter(lookback_months=lb, min_trades=3, min_pnl=0)
    print("  %-12s %6.2f%% %6.2f%% %7.3f %6d %4.0f%%" % (
        "%d months" % lb, r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Sweep min win rate threshold
print("\n  --- Win Rate Threshold (lookback=3mo, min_trades=3) ---")
print("  %-12s %7s %7s %7s %6s %5s" % ("Min WR", "CAGR", "MDD", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for wr in [0, 0.3, 0.4, 0.5, 0.6]:
    r = run_with_rolling_filter(lookback_months=3, min_trades=3, min_win_rate=wr)
    print("  %-12s %6.2f%% %6.2f%% %7.3f %6d %4.0f%%" % (
        "%.0f%%" % (wr * 100), r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Sweep min trades threshold
print("\n  --- Min Trades Threshold (lookback=3mo, any positive P&L) ---")
print("  %-12s %7s %7s %7s %6s %5s" % ("Min Trades", "CAGR", "MDD", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for mt in [1, 2, 3, 5, 10]:
    r = run_with_rolling_filter(lookback_months=3, min_trades=mt, min_pnl=0)
    print("  %-12s %6.2f%% %6.2f%% %7.3f %6d %4.0f%%" % (
        "%d" % mt, r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Compare: strict filter vs no filter at various slippage
print("\n  --- Strict Filter vs No Filter (slippage sensitivity) ---")
print("  Will need to modify base_sim slippage for this...")
best_filter_config = {"lookback_months": 3, "min_trades": 3, "min_pnl": 0}

for slip in [0, 2, 5]:
    base_sim["slippage_bps"] = slip
    r_no_filter = run_with_rolling_filter(lookback_months=999, min_trades=0)
    r_filter = run_with_rolling_filter(**best_filter_config)
    print("  Slip=%d: NoFilter CAGR=%.2f%% Calmar=%.3f | Filter CAGR=%.2f%% Calmar=%.3f | Delta=%+.2fpp" % (
        slip, r_no_filter["cagr"], r_no_filter["calmar"],
        r_filter["cagr"], r_filter["calmar"],
        r_filter["cagr"] - r_no_filter["cagr"]))

base_sim["slippage_bps"] = 0  # reset

print("\n  Done.")

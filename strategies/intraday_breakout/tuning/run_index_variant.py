"""Index variant: test gap-up breakout on NIFTY50 ETF (NIFTYBEES).

Hypothesis: Indices have near-zero slippage on market orders. If the gap-up
breakout signal works on an index, we avoid stock selection entirely and
trade a single highly liquid instrument.

Differences from stock variant:
  - Universe = just NIFTYBEES (or BANKNIFTY equivalent)
  - Regime: still use internal regime from top-50 universe breadth
  - Breakout signal: NIFTYBEES close >= 3d high, > 10d MA, > open
  - Gap-up: NIFTYBEES opens above prior-day high
  - Position: 100% of capital on signal (single instrument)
  - Slippage: effectively 0-1 bps (ETF, highly liquid)

Also tests without breakout filter (just gap-up + regime).
"""
import sys, json, math, time, os
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_daily_data, load_minute_data, compute_regime_hysteresis,
    nse_intraday_charges, _month_boundaries, SECONDS_IN_ONE_DAY,
    FMP_MINUTE_PATH
)
import polars as pl
from datetime import datetime, timezone
from collections import defaultdict

# Config
START_DATE = "2022-01-01"
END_DATE = "2025-12-31"
INITIAL_CAPITAL = 1000000
PREFETCH_DAYS = 500

INDEX_SYMBOLS = ["NIFTYBEES"]  # FMP: NIFTYBEES.NS
# Can also try: BANKBEES, JUNIORBEES

start_epoch = int(datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
end_epoch = int(datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
prefetch_epoch = start_epoch - int(PREFETCH_DAYS * 1.5 * SECONDS_IN_ONE_DAY)

# ═══════════════════════════════════════════════════════════════════════════
# Load data
# ═══════════════════════════════════════════════════════════════════════════

print("\n--- Loading data ---")
df_daily = load_daily_data(prefetch_epoch, end_epoch)

# Load minute data for index ETF(s)
index_fmp_symbols = {s + ".NS" for s in INDEX_SYMBOLS}
df_minute = load_minute_data(symbols=index_fmp_symbols)

if df_minute.is_empty():
    # NIFTYBEES might be stored differently. Try alternate FMP naming conventions.
    print("  No minute data for NIFTYBEES.NS, trying alternate names...")
    alt_sets = [
        {"NIFTYBEES"},
        {"NIFTYBEES.NSE"},
        {"NIFTY-BEES.NS"},
    ]
    for alt in alt_sets:
        df_minute = load_minute_data(symbols=alt)
        if not df_minute.is_empty():
            index_fmp_symbols = alt
            print(f"  Found data with symbol(s): {alt}")
            break
    if df_minute.is_empty():
        # Last resort: scan one parquet file to discover available NIFTY-related symbols
        import glob as glob_mod
        sample_files = sorted(glob_mod.glob(os.path.join(FMP_MINUTE_PATH, "*.parquet")))[:1]
        if sample_files:
            sample = pl.read_parquet(sample_files[0])
            nifty_matches = [s for s in sample["symbol"].unique().to_list()
                            if "NIFTY" in s.upper() or "BEES" in s.upper()]
            print(f"  Available NIFTY-related in sample: {nifty_matches}")
            if nifty_matches:
                index_fmp_symbols = set(nifty_matches)
                df_minute = load_minute_data(symbols=index_fmp_symbols)

print(f"  Index minute data: {df_minute.height} rows")
if df_minute.is_empty():
    print("  ERROR: No index minute data found. Cannot proceed.")
    print("  Available index symbols need to be checked on prod.")
    sys.exit(1)

# Index minute data by date_key
minute_by_date = {}
if df_minute.height > 0:
    for date_key in df_minute["date_key"].unique().to_list():
        minute_by_date[date_key] = df_minute.filter(pl.col("date_key") == date_key)

# Compute daily highs from minute data
minute_daily_highs = {}
if df_minute.height > 0:
    daily_high_df = (
        df_minute.group_by(["date_key", "symbol"])
        .agg(pl.col("high").max().alias("day_high"))
    )
    for row in daily_high_df.to_dicts():
        dk = row["date_key"]
        if dk not in minute_daily_highs:
            minute_daily_highs[dk] = {}
        minute_daily_highs[dk][row["symbol"]] = row["day_high"]

# Compute regime from top-50 universe (breadth of market)
print("\n--- Computing regime from top-50 breadth ---")
df_regime = df_daily.with_columns(
    (pl.col("turnover") >= 500_000_000).alias("scanner_pass")
)
bull_epochs = compute_regime_hysteresis(
    df_regime, sma_period=50, entry_threshold=0.4, exit_threshold=0.35
)
n_bull = len([e for e in bull_epochs if start_epoch <= e <= end_epoch])
print(f"  Regime: {n_bull} bull days")


def run_index_backtest(config: dict) -> dict:
    """Run backtest for index instrument."""
    target_pct = config.get("target_pct", 1.0) / 100
    stop_pct = config.get("stop_pct", 0.5) / 100
    trailing_stop_pct = config.get("trailing_stop_pct", 0) / 100
    slippage_bps = config.get("slippage_bps", 0)
    n_day_high = config.get("n_day_high", 3)
    n_day_ma = config.get("n_day_ma", 10)
    require_breakout = config.get("require_breakout", True)
    use_regime = config.get("use_regime", True)
    min_gap_bps = config.get("min_gap_bps", 0)

    # Get daily data for the index instrument
    index_inst = INDEX_SYMBOLS[0]  # Use first index
    fmp_sym = list(index_fmp_symbols)[0]

    df_idx = df_daily.filter(pl.col("instrument") == index_inst).sort("date_epoch")
    if df_idx.is_empty():
        # Try without NSE: prefix
        df_idx = df_daily.filter(pl.col("instrument").str.contains(index_inst)).sort("date_epoch")
    if df_idx.is_empty():
        return {"error": f"No daily data for {index_inst}"}

    # Compute signals
    df_idx = df_idx.with_columns([
        pl.col("close").rolling_mean(window_size=n_day_ma, min_samples=1).alias("ma"),
        pl.col("close").rolling_max(window_size=n_day_high, min_samples=1).alias("high_n"),
    ])

    trades = []
    equity = float(INITIAL_CAPITAL)
    equity_curve = [(start_epoch, equity)]

    rows = df_idx.filter(
        (pl.col("date_epoch") >= start_epoch) & (pl.col("date_epoch") <= end_epoch)
    ).to_dicts()

    for i, row in enumerate(rows[:-1]):  # skip last day (can't trade next day)
        epoch = row["date_epoch"]
        next_row = rows[i + 1]
        next_epoch = next_row["date_epoch"]

        # Regime check
        if use_regime and epoch not in bull_epochs:
            continue

        # Breakout signal check
        if require_breakout:
            if not (row["close"] >= row["high_n"] and
                    row["close"] > row["ma"] and
                    row["close"] > row["open"]):
                continue

        # Next day: check gap-up from minute data
        date_key = next_epoch // SECONDS_IN_ONE_DAY
        day_minute = minute_by_date.get(date_key, pl.DataFrame())
        if day_minute.is_empty():
            continue

        # Get signal day high from minute data
        signal_date_key = epoch // SECONDS_IN_ONE_DAY
        signal_highs = minute_daily_highs.get(signal_date_key, {})
        prior_high = signal_highs.get(fmp_sym)
        if not prior_high or prior_high <= 0:
            continue

        bars = day_minute.filter(pl.col("symbol") == fmp_sym).sort("bar_minute").to_dicts()
        if not bars:
            continue

        first_bar = bars[0]
        first_open = first_bar.get("open")
        if not first_open or first_open <= prior_high:
            continue  # no gap-up

        # Min gap check
        gap_bps = (first_open - prior_high) / prior_high * 10000
        if gap_bps < min_gap_bps:
            continue

        # Entry at open with slippage
        entry_price = first_open * (1 + slippage_bps / 10000)
        order_value = equity  # full capital on single instrument

        # Simulate exit
        max_price = entry_price
        exit_price = None
        exit_type = None

        for bar in bars:
            bar_high = bar.get("high", entry_price) or entry_price
            bar_low = bar.get("low", entry_price) or entry_price
            bar_close = bar.get("close", entry_price) or entry_price
            max_price = max(max_price, bar_high)

            if target_pct > 0 and bar_high >= entry_price * (1 + target_pct):
                exit_price = entry_price * (1 + target_pct)
                exit_type = "target"
                break
            if stop_pct > 0 and bar_low <= entry_price * (1 - stop_pct):
                exit_price = entry_price * (1 - stop_pct)
                exit_type = "stop"
                break
            if trailing_stop_pct > 0 and max_price > entry_price:
                trail = max_price * (1 - trailing_stop_pct)
                if bar_low <= trail:
                    exit_price = trail
                    exit_type = "trailing_stop"
                    break
            if bar.get("bar_minute", 0) >= 925:
                exit_price = bar_close
                exit_type = "eod_close"
                break

        if exit_price is None:
            exit_price = bars[-1].get("close", entry_price) or entry_price
            exit_type = "eod_close"

        exit_price *= (1 - slippage_bps / 10000)
        charges = nse_intraday_charges(order_value)
        pnl = (exit_price - entry_price) / entry_price * order_value - charges
        equity += pnl
        equity_curve.append((next_epoch, equity))

        trade_date = datetime.fromtimestamp(next_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
        trades.append({
            "trade_date": trade_date,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "exit_type": exit_type,
            "pnl": round(pnl, 2),
            "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 4),
            "gap_bps": round(gap_bps, 1),
        })

    # Metrics
    years = 4.0
    cagr = ((equity / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if equity > 0 else -100
    peak = INITIAL_CAPITAL
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak
        if dd < max_dd:
            max_dd = dd

    win_rate = sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100 if trades else 0
    calmar = cagr / 100 / abs(max_dd) if max_dd != 0 else 0

    return {
        "cagr": round(cagr, 2),
        "mdd": round(max_dd * 100, 2),
        "calmar": round(calmar, 3),
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "total_return": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
        "trade_log": trades,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Experiments
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  INDEX VARIANT: NIFTYBEES Gap-Up Breakout")
print("=" * 70)

# Experiment 1: Target/Stop sweep
print("\n  --- Experiment 1: Target × Stop (with breakout + regime) ---")
print("  %-7s %-6s %7s %7s %7s %6s %5s" % ("Target", "Stop", "CAGR", "MDD", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)

for target in [0.5, 0.75, 1.0, 1.5, 2.0]:
    for stop in [0.25, 0.5, 0.75, 1.0]:
        r = run_index_backtest({"target_pct": target, "stop_pct": stop, "slippage_bps": 0})
        if "error" in r:
            print(f"  ERROR: {r['error']}")
            break
        print("  %-7s %-6s %6.2f%% %6.2f%% %7.3f %6d %4.0f%%" % (
            "%.1f%%" % target, "%.2f%%" % stop,
            r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Experiment 2: Without breakout filter (just regime + gap-up)
print("\n  --- Experiment 2: No breakout filter (regime + gap-up only) ---")
for target in [0.5, 0.75, 1.0]:
    for stop in [0.25, 0.5]:
        r = run_index_backtest({
            "target_pct": target, "stop_pct": stop,
            "require_breakout": False, "slippage_bps": 0
        })
        if "error" in r:
            break
        print("  t=%.1f%% s=%.2f%% → CAGR=%6.2f%% MDD=%6.2f%% Calmar=%7.3f Trades=%d WR=%.0f%%" % (
            target, stop, r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Experiment 3: Without regime (just breakout + gap-up)
print("\n  --- Experiment 3: No regime filter (breakout + gap-up only) ---")
for target in [0.5, 0.75, 1.0]:
    for stop in [0.25, 0.5]:
        r = run_index_backtest({
            "target_pct": target, "stop_pct": stop,
            "use_regime": False, "slippage_bps": 0
        })
        if "error" in r:
            break
        print("  t=%.1f%% s=%.2f%% → CAGR=%6.2f%% MDD=%6.2f%% Calmar=%7.3f Trades=%d WR=%.0f%%" % (
            target, stop, r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# Experiment 4: Best config with leverage
print("\n  --- Experiment 4: Best index config with leverage ---")
best = run_index_backtest({"target_pct": 1.0, "stop_pct": 0.5, "slippage_bps": 1})
if "error" not in best and best["trades"] > 0:
    print("  Base (1x, 1bps): CAGR=%.2f%% MDD=%.2f%% Calmar=%.3f Trades=%d" % (
        best["cagr"], best["mdd"], best["calmar"], best["trades"]))

    # Simple leverage approximation: amplify daily P&L
    for lev in [2, 3, 5, 10]:
        lev_equity = INITIAL_CAPITAL
        lev_peak = lev_equity
        lev_max_dd = 0
        daily_pnl_map = defaultdict(float)
        for t in best["trade_log"]:
            daily_pnl_map[t["trade_date"]] += t["pnl"]
        for d in sorted(daily_pnl_map.keys()):
            lev_equity += daily_pnl_map[d] * lev
            if lev_equity > lev_peak:
                lev_peak = lev_equity
            dd = (lev_equity - lev_peak) / lev_peak if lev_peak > 0 else 0
            if dd < lev_max_dd:
                lev_max_dd = dd
        lev_cagr = ((lev_equity / INITIAL_CAPITAL) ** (1/4) - 1) * 100 if lev_equity > 0 else -100
        lev_calmar = lev_cagr / 100 / abs(lev_max_dd) if lev_max_dd != 0 else 0
        print("  %dx: CAGR=%.1f%% MDD=%.2f%% Calmar=%.3f" % (
            lev, lev_cagr, lev_max_dd * 100, lev_calmar))

print("\n  Done.")

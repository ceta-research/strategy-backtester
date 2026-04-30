"""Intraday breakout pipeline — prod runner (local parquet, no API).

Self-contained. Reads daily + minute data from /opt/insydia/data/ parquet files.
No chunking — loads all data into memory (prod has 251GB RAM).

Usage:
    python3 intraday_breakout_prod.py config.yaml [--output results.json]
"""

from __future__ import annotations

import gc
import json
import math
import os
import sys
import time
import glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from itertools import product

import polars as pl

SECONDS_IN_ONE_DAY = 86400

# Data paths on prod
NSE_DAILY_PATH = "/opt/insydia/data/data_source=nse/charting/granularity=day/"
FMP_MINUTE_PATH = "/opt/insydia/data/data_source=fmp/tick_data/stock/granularity=1min/exchange=NSE/"


# ── Charges (from engine/charges.py) ─────────────────────────────────────

NSE_BROKERAGE_RATE = 0.0003
NSE_BROKERAGE_CAP = 20.0
NSE_STT_INTRADAY_SELL = 0.00025
NSE_EXCHANGE_RATE = 0.0000297
NSE_SEBI_RATE = 0.000001
NSE_GST_RATE = 0.18
NSE_STAMP_DUTY_INTRADAY = 0.00003


def nse_intraday_charges(order_value: float) -> float:
    """Round-trip intraday charges for NSE equity (Zerodha MIS)."""
    brokerage_per_leg = min(order_value * NSE_BROKERAGE_RATE, NSE_BROKERAGE_CAP)
    brokerage = brokerage_per_leg * 2
    stt = order_value * NSE_STT_INTRADAY_SELL
    exchange = order_value * NSE_EXCHANGE_RATE * 2
    sebi = order_value * NSE_SEBI_RATE * 2
    stamp = order_value * NSE_STAMP_DUTY_INTRADAY
    gst = (brokerage + exchange) * NSE_GST_RATE
    return round(brokerage + stt + exchange + sebi + stamp + gst, 2)


# ── Data loading ─────────────────────────────────────────────────────────

def load_daily_data(start_epoch: int, end_epoch: int) -> pl.DataFrame:
    """Load NSE daily OHLCV from local parquet files."""
    t0 = time.time()
    parquet_files = sorted(glob.glob(os.path.join(NSE_DAILY_PATH, "*.parquet")))
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files in {NSE_DAILY_PATH}")

    dfs = []
    for f in parquet_files:
        df = pl.read_parquet(f)
        # Filter to relevant columns and date range
        if "date_epoch" in df.columns:
            df = df.filter(
                (pl.col("date_epoch") >= start_epoch)
                & (pl.col("date_epoch") <= end_epoch)
            )
        dfs.append(df)

    df = pl.concat(dfs, how="vertical_relaxed")

    # Normalize column names
    rename_map = {}
    if "symbol" in df.columns and "instrument" not in df.columns:
        rename_map["symbol"] = "instrument"
    if rename_map:
        df = df.rename(rename_map)

    # Add turnover if missing
    if "turnover" not in df.columns and "close" in df.columns and "volume" in df.columns:
        df = df.with_columns(
            (pl.col("close") * pl.col("volume")).alias("turnover")
        )

    # Filter valid rows
    df = df.filter(
        (pl.col("close") > 0)
        & (pl.col("volume") > 0)
    ).sort(["instrument", "date_epoch"])

    elapsed = round(time.time() - t0, 1)
    print(f"  Daily data: {df.height:,} rows, {df['instrument'].n_unique()} instruments ({elapsed}s)")
    return df


def load_minute_data(symbols: set[str] | None = None) -> pl.DataFrame:
    """Load NSE minute OHLCV from local parquet files.

    If symbols is provided, filter to those symbols only.
    FMP uses SYMBOL.NS format.
    """
    t0 = time.time()
    parquet_files = sorted(glob.glob(os.path.join(FMP_MINUTE_PATH, "*.parquet")))
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files in {FMP_MINUTE_PATH}")

    print(f"  Loading minute data: {len(parquet_files)} files...")
    dfs = []
    for i, f in enumerate(parquet_files):
        df = pl.read_parquet(f)
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        # Compute bar_minute from dateEpoch (timestamps are local time labeled UTC)
        # NSE session: 09:15 (555) to 15:30 (930)
        df = df.with_columns(
            ((pl.col("dateEpoch") % 86400) // 60).cast(pl.Int32).alias("bar_minute"),
            (pl.col("dateEpoch") // 86400).cast(pl.Int32).alias("date_key"),
        )
        # Filter to trading hours
        df = df.filter(
            (pl.col("bar_minute") >= 555)
            & (pl.col("bar_minute") <= 930)
        )
        dfs.append(df)
        if (i + 1) % 20 == 0:
            print(f"    Loaded {i + 1}/{len(parquet_files)} files...")

    df = pl.concat(dfs, how="vertical_relaxed")
    df = df.sort(["symbol", "dateEpoch"])

    elapsed = round(time.time() - t0, 1)
    print(f"  Minute data: {df.height:,} rows, {df['symbol'].n_unique()} symbols ({elapsed}s)")
    return df


# ── Internal regime (from engine/internal_regime.py) ─────────────────────

def compute_regime_hysteresis(
    df: pl.DataFrame,
    sma_period: int = 50,
    entry_threshold: float = 0.45,
    exit_threshold: float = 0.35,
) -> set:
    """Compute internal regime with hysteresis from daily data.

    Expects df with columns: instrument, date_epoch, close, scanner_pass (bool).
    """
    df_passed = df.filter(pl.col("scanner_pass"))
    if df_passed.is_empty():
        return set()

    df_passed = df_passed.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("close")
        .rolling_mean(window_size=sma_period, min_samples=max(1, sma_period // 2))
        .over("instrument")
        .alias("_sma")
    )

    daily = (
        df_passed.group_by("date_epoch", maintain_order=True)
        .agg((pl.col("close") > pl.col("_sma")).mean().alias("regime_score"))
        .sort("date_epoch")
    )

    epochs = daily["date_epoch"].to_list()
    scores = daily["regime_score"].to_list()

    bull_set = set()
    is_bull = False

    for epoch, score in zip(epochs, scores):
        if score is None:
            continue
        if is_bull:
            if score < exit_threshold:
                is_bull = False
            else:
                bull_set.add(epoch)
        else:
            if score > entry_threshold:
                is_bull = True
                bull_set.add(epoch)

    return bull_set


# ── Universe selection ───────────────────────────────────────────────────

def select_universe(df_daily: pl.DataFrame, month_epoch: int,
                    top_n: int = 100,
                    min_turnover: float = 500_000_000) -> list[str]:
    """Select top N stocks by avg daily turnover in the prior month."""
    window_start = month_epoch - 30 * SECONDS_IN_ONE_DAY

    avg_turnover = (
        df_daily.filter(
            (pl.col("date_epoch") >= window_start)
            & (pl.col("date_epoch") < month_epoch)
        )
        .group_by("instrument")
        .agg(pl.col("turnover").mean().alias("avg_turnover"))
        .filter(pl.col("avg_turnover") >= min_turnover)
        .sort("avg_turnover", descending=True)
        .head(top_n)
    )

    return avg_turnover["instrument"].to_list()


# ── Daily signal computation ─────────────────────────────────────────────

def compute_daily_signals(
    df_daily: pl.DataFrame,
    universe: list[str],
    start_epoch: int,
    end_epoch: int,
    n_day_high: int = 3,
    n_day_ma: int = 10,
    regime_bull_epochs: set = None,
) -> tuple[dict, dict]:
    """Compute breakout signals + signal-day highs.

    Returns:
        signals: {date_epoch: [instruments]}
        signal_day_highs: {date_epoch: {instrument: high}}
    """
    df = (df_daily
          .filter(pl.col("instrument").is_in(universe))
          .sort(["instrument", "date_epoch"]))

    df = df.with_columns([
        pl.col("close")
        .rolling_mean(window_size=n_day_ma, min_samples=1)
        .over("instrument")
        .alias("n_day_ma"),
        pl.col("close")
        .rolling_max(window_size=n_day_high, min_samples=1)
        .over("instrument")
        .alias("n_day_high_val"),
    ])

    df = df.filter(
        (pl.col("date_epoch") >= start_epoch)
        & (pl.col("date_epoch") <= end_epoch)
    )

    # Entry filter
    entry_filter = (
        (pl.col("close") >= pl.col("n_day_high_val"))
        & (pl.col("close") > pl.col("n_day_ma"))
        & (pl.col("close") > pl.col("open"))
    )
    if regime_bull_epochs:
        entry_filter = entry_filter & pl.col("date_epoch").is_in(list(regime_bull_epochs))

    df_signals = df.filter(entry_filter)

    signals: dict = {}
    signal_day_highs: dict = {}

    for row in df_signals.select(["date_epoch", "instrument", "high"]).to_dicts():
        epoch = row["date_epoch"]
        if epoch not in signals:
            signals[epoch] = []
            signal_day_highs[epoch] = {}
        signals[epoch].append(row["instrument"])
        signal_day_highs[epoch][row["instrument"]] = row["high"]

    return signals, signal_day_highs


# ── Intraday simulation ──────────────────────────────────────────────────

def simulate_intraday_day(
    minute_df: pl.DataFrame,
    eligible_instruments: list[str],
    signal_day_highs: dict[str, float],
    config: dict,
    margin: float,
) -> tuple[list[dict], float]:
    """Simulate one trading day on minute bars."""
    target_pct = config.get("target_pct", 1.5) / 100
    stop_pct = config.get("stop_pct", 0.75) / 100
    trailing_stop_pct = config.get("trailing_stop_pct", 0) / 100
    max_entry_bar = config.get("max_entry_bar", 120)
    max_entry_minute = 555 + max_entry_bar
    eod_exit_minute = config.get("eod_exit_minute", 925)
    max_positions = config.get("max_positions", 5)
    slippage_bps = config.get("slippage_bps", 5)  # per side

    # Position sizing: base_position_slots controls per-trade size.
    # margin / base_position_slots = order value per trade.
    # max_positions caps how many trades we take.
    # When base_position_slots < max_positions, we deploy more capital
    # on high-signal days without diluting per-trade size.
    base_position_slots = config.get("base_position_slots", max_positions)
    order_value = margin / base_position_slots if base_position_slots > 0 else 0

    if order_value <= 0 or minute_df.is_empty():
        return [], 0.0

    trades = []
    day_pnl = 0.0
    positions_taken = 0

    margin_used = 0.0

    for inst in eligible_instruments:
        if positions_taken >= max_positions:
            break
        # Margin check: don't over-deploy
        if margin_used + order_value > margin:
            break

        fmp_sym = inst.replace("NSE:", "") + ".NS"
        prior_high = signal_day_highs.get(inst)
        if prior_high is None or prior_high <= 0:
            continue

        inst_bars = minute_df.filter(pl.col("symbol") == fmp_sym).sort("bar_minute")
        if inst_bars.is_empty():
            continue

        bars = inst_bars.to_dicts()

        # Entry mode:
        #   "market" (default): enter at breakout bar, apply slippage_bps
        #   "limit":  place limit at prior_high. If open > prior_high (gap-up),
        #             fill at open. If open < prior_high and bar crosses it,
        #             fill at exact prior_high (0 slippage).
        #   "limit_no_gap": same as limit but SKIP gap-up stocks entirely.
        entry_mode = config.get("entry_mode", "market")
        max_gap_bps = config.get("max_gap_bps", 9999)  # for limit mode: skip gaps > this
        require_gap_up = config.get("require_gap_up", False)

        entry_price = None
        entry_idx = None
        entry_minute = None

        first_bar = bars[0] if bars else None

        # Gap-up filter: only trade stocks that open above prior-day high
        if require_gap_up and first_bar:
            first_open = first_bar.get("open")
            if not first_open or first_open <= prior_high:
                continue  # skip: didn't gap up

        if entry_mode in ("limit", "limit_no_gap") and first_bar:
            first_open = first_bar.get("open")
            if first_open and first_open > 0:
                if first_open > prior_high:
                    # Gap-up: stock opens above our limit
                    gap_bps = (first_open - prior_high) / prior_high * 10000
                    if entry_mode == "limit_no_gap":
                        # Skip this stock entirely
                        continue
                    elif gap_bps > max_gap_bps:
                        # Gap too large, skip
                        continue
                    else:
                        # Fill at open price (worse than limit)
                        entry_price = first_open
                        entry_idx = 0
                        entry_minute = first_bar.get("bar_minute", 555)
                else:
                    # Open below limit — look for intraday cross
                    for i, bar in enumerate(bars):
                        if bar["bar_minute"] > max_entry_minute:
                            break
                        if bar["high"] and bar["high"] > prior_high:
                            # Fill at exact limit price (0 slippage)
                            entry_price = prior_high
                            entry_idx = i
                            entry_minute = bar["bar_minute"]
                            break
        else:
            # Market order mode (original logic)
            for i, bar in enumerate(bars):
                if bar["bar_minute"] > max_entry_minute:
                    break
                if bar["high"] and bar["high"] > prior_high:
                    entry_price = prior_high * (1 + slippage_bps / 10000)
                    entry_idx = i
                    entry_minute = bar["bar_minute"]
                    break

        if entry_price is None:
            continue

        # Simulate exit
        max_price = entry_price
        exit_price = None
        exit_type = None

        for j in range(entry_idx, len(bars)):
            bar = bars[j]
            bar_high = bar["high"] or entry_price
            bar_low = bar["low"] or entry_price
            bar_close = bar["close"] or entry_price

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
                trail_stop = max_price * (1 - trailing_stop_pct)
                if bar_low <= trail_stop:
                    exit_price = trail_stop
                    exit_type = "trailing_stop"
                    break

            if bar["bar_minute"] >= eod_exit_minute:
                exit_price = bar_close
                exit_type = "eod_close"
                break

        if exit_price is None and bars:
            exit_price = bars[-1]["close"] or entry_price
            exit_type = "eod_close"

        if exit_price is None or exit_price <= 0:
            continue

        # Apply exit slippage
        exit_price = exit_price * (1 - slippage_bps / 10000)

        charges = nse_intraday_charges(order_value)
        pnl = (exit_price - entry_price) / entry_price * order_value - charges

        trades.append({
            "symbol": inst,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "entry_minute": entry_minute,
            "exit_type": exit_type,
            "pnl": round(pnl, 2),
            "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 4),
            "charges": round(charges, 2),
            "order_value": round(order_value, 2),
        })

        day_pnl += pnl
        positions_taken += 1
        margin_used += order_value

    return trades, day_pnl


# ── Pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(config: dict) -> dict:
    """Run full intraday breakout backtest from config dict."""
    pipeline_start = time.time()

    start_date = config["start_date"]
    end_date = config["end_date"]
    initial_capital = config.get("initial_capital", 1_000_000)
    prefetch_days = config.get("prefetch_days", 500)

    start_epoch = int(datetime.strptime(start_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp())
    end_epoch = int(datetime.strptime(end_date, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp())
    prefetch_epoch = start_epoch - int(prefetch_days * 1.5 * SECONDS_IN_ONE_DAY)

    # Phase 1: Load all data
    print("\n--- Loading data ---")
    df_daily = load_daily_data(prefetch_epoch, end_epoch)

    # Get universe to filter minute data
    top_n = config.get("top_n", 100)
    min_turnover = config.get("min_avg_turnover", 500_000_000)

    # Build set of all possible universe symbols across all months
    months = _month_boundaries(start_date, end_date)
    all_universe_symbols = set()
    for _, _, month_epoch in months:
        universe = select_universe(df_daily, month_epoch, top_n, min_turnover)
        for inst in universe:
            fmp_sym = inst.replace("NSE:", "") + ".NS"
            all_universe_symbols.add(fmp_sym)
    print(f"  Universe symbols (all months): {len(all_universe_symbols)}")

    df_minute = load_minute_data(symbols=all_universe_symbols)

    # Pre-index minute data by date_key for fast lookup
    print("  Indexing minute data by date...")
    minute_by_date: dict[int, pl.DataFrame] = {}
    if df_minute.height > 0:
        for date_key in df_minute["date_key"].unique().to_list():
            minute_by_date[date_key] = df_minute.filter(pl.col("date_key") == date_key)
    print(f"  Indexed {len(minute_by_date)} trading days")

    # Pre-compute per-symbol per-day high FROM MINUTE DATA.
    # This avoids split-adjustment mismatch between daily (NSE charting)
    # and minute (FMP) data sources. The daily high for breakout level
    # must come from the same price basis as minute entries/exits.
    print("  Computing per-day highs from minute data...")
    minute_daily_highs: dict[int, dict[str, float]] = {}  # {date_key: {fmp_symbol: high}}
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
    print(f"  Computed daily highs for {len(minute_daily_highs)} days")

    # Phase 2: Compute regime
    print("\n--- Computing regime ---")
    regime_sma = config.get("internal_regime_sma_period", 50)
    regime_entry_thr = config.get("internal_regime_threshold", 0.4)
    regime_exit_thr = config.get("internal_regime_exit_threshold", 0.35)

    # Mark scanner-passed rows (liquidity filter for regime computation)
    df_for_regime = df_daily.with_columns(
        (pl.col("turnover") >= min_turnover).alias("scanner_pass")
    )
    bull_epochs = compute_regime_hysteresis(
        df_for_regime, sma_period=regime_sma,
        entry_threshold=regime_entry_thr,
        exit_threshold=regime_exit_thr,
    )
    n_bull = len([e for e in bull_epochs if start_epoch <= e <= end_epoch])
    print(f"  Regime: {n_bull} bull days (hysteresis {regime_entry_thr}/{regime_exit_thr})")

    # Phase 3: Build sweep configs
    sweep_keys = ["target_pct", "stop_pct", "trailing_stop_pct", "max_entry_bar",
                   "max_positions", "eod_exit_minute", "slippage_bps",
                   "entry_mode", "max_gap_bps", "base_position_slots",
                   "require_gap_up"]
    sweep_params = {}
    for k in sweep_keys:
        v = config.get(k)
        if v is not None:
            sweep_params[k] = v if isinstance(v, list) else [v]

    combos = _cartesian(sweep_params) if sweep_params else [{}]
    n_day_high = config.get("n_day_high", 3)
    n_day_ma = config.get("n_day_ma", 10)

    print(f"\n--- Running {len(combos)} config(s) ---")
    results = []

    for cfg_idx, sim_params in enumerate(combos):
        cfg_start = time.time()
        margin = float(initial_capital)
        all_trades = []
        equity_points = [(start_epoch, margin)]

        for month_start, month_end, month_epoch in months:
            universe = select_universe(df_daily, month_epoch, top_n, min_turnover)
            if not universe:
                continue

            m_start_epoch = int(datetime.strptime(month_start, "%Y-%m-%d")
                                .replace(tzinfo=timezone.utc).timestamp())
            m_end_epoch = int(datetime.strptime(month_end, "%Y-%m-%d")
                              .replace(tzinfo=timezone.utc).timestamp())

            signals, _daily_highs = compute_daily_signals(
                df_daily, universe, m_start_epoch, m_end_epoch,
                n_day_high=n_day_high, n_day_ma=n_day_ma,
                regime_bull_epochs=bull_epochs,
            )
            # _daily_highs from daily data is NOT used for execution prices
            # (split-adjustment mismatch). We use minute-derived highs instead.

            if not signals:
                continue

            # Find next trading days
            all_daily_epochs = sorted(
                df_daily.filter(pl.col("instrument").is_in(universe))
                ["date_epoch"].unique().to_list()
            )
            next_day_map = {}
            for i, ep in enumerate(all_daily_epochs):
                if i + 1 < len(all_daily_epochs):
                    next_day_map[ep] = all_daily_epochs[i + 1]

            for epoch in sorted(signals.keys()):
                next_epoch = next_day_map.get(epoch)
                if next_epoch is None:
                    continue

                date_key = next_epoch // SECONDS_IN_ONE_DAY
                minute_df = minute_by_date.get(date_key, pl.DataFrame())
                if minute_df.is_empty():
                    continue

                # Build signal-day highs from MINUTE data (consistent price basis).
                # Signal day = epoch. Its date_key:
                signal_date_key = epoch // SECONDS_IN_ONE_DAY
                signal_minute_highs = minute_daily_highs.get(signal_date_key, {})

                # Convert instrument names to match minute data format
                # signals[epoch] has bare symbols like "RELIANCE"
                # minute_daily_highs has FMP format like "RELIANCE.NS"
                sig_highs_for_sim = {}
                for inst in signals[epoch]:
                    fmp_sym = inst.replace("NSE:", "") + ".NS"
                    if fmp_sym in signal_minute_highs:
                        sig_highs_for_sim[inst] = signal_minute_highs[fmp_sym]

                day_trades, day_pnl = simulate_intraday_day(
                    minute_df, signals[epoch],
                    sig_highs_for_sim,
                    sim_params, margin,
                )

                margin += day_pnl
                next_date_str = datetime.fromtimestamp(
                    next_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                all_trades.extend([
                    {**t, "trade_date": next_date_str} for t in day_trades
                ])
                equity_points.append((next_epoch, margin))

        # Compute metrics
        cfg_elapsed = round(time.time() - cfg_start, 1)
        total_return = (margin / initial_capital - 1) * 100
        years = (end_epoch - start_epoch) / (365.25 * SECONDS_IN_ONE_DAY)
        cagr = ((margin / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

        # Max drawdown from equity curve
        peak = initial_capital
        max_dd = 0
        for _, val in equity_points:
            if val > peak:
                peak = val
            dd = (val - peak) / peak
            if dd < max_dd:
                max_dd = dd

        # Win/loss stats
        pnls = [t["pnl"] for t in all_trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]
        win_rate = len(winners) / len(pnls) * 100 if pnls else 0
        avg_win = sum(winners) / len(winners) if winners else 0
        avg_loss = sum(losers) / len(losers) if losers else 0

        # Sharpe (daily returns)
        daily_returns = []
        for i in range(1, len(equity_points)):
            prev_val = equity_points[i - 1][1]
            cur_val = equity_points[i][1]
            if prev_val > 0:
                daily_returns.append(cur_val / prev_val - 1)
        if daily_returns and len(daily_returns) > 1:
            mean_r = sum(daily_returns) / len(daily_returns)
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            vol = math.sqrt(var_r) * math.sqrt(252)
            sharpe = (cagr / 100 - 0.065) / vol if vol > 0 else 0
        else:
            sharpe = 0
            vol = 0

        calmar = cagr / 100 / abs(max_dd) if max_dd != 0 else 0

        result = {
            "params": sim_params,
            "cagr": round(cagr, 2),
            "mdd": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 3),
            "calmar": round(calmar, 3),
            "total_return": round(total_return, 2),
            "trades": len(all_trades),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "final_margin": round(margin, 2),
            "elapsed": cfg_elapsed,
            "equity_points": equity_points,
            "trade_log": all_trades,
        }
        results.append(result)

        print(f"  [{cfg_idx+1}/{len(combos)}] CAGR={cagr:.2f}% MDD={max_dd*100:.2f}% "
              f"Sharpe={sharpe:.3f} Calmar={calmar:.3f} "
              f"Trades={len(all_trades)} WR={win_rate:.0f}% ({cfg_elapsed}s)")

    total_elapsed = round(time.time() - pipeline_start, 1)
    print(f"\n--- Pipeline Complete: {total_elapsed}s ---")

    return {
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "n_configs": len(combos),
        "results": results,
    }


# ── Helpers ──────────────────────────────────────────────────────────────

def _month_boundaries(start_date: str, end_date: str) -> list[tuple]:
    months = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end_dt:
        month_start = dt.strftime("%Y-%m-%d")
        if dt.month == 12:
            next_month = dt.replace(year=dt.year + 1, month=1, day=1)
        else:
            next_month = dt.replace(month=dt.month + 1, day=1)
        month_end_dt = min(next_month - timedelta(days=1), end_dt)
        month_end = month_end_dt.strftime("%Y-%m-%d")
        month_epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
        months.append((month_start, month_end, month_epoch))
        dt = next_month
    return months


def _cartesian(params: dict) -> list[dict]:
    if not params:
        return [{}]
    keys = list(params.keys())
    vals = [params[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in product(*vals)]


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import yaml

    if len(sys.argv) < 2:
        print("Usage: python3 intraday_breakout_prod.py config.yaml [--output results.json]")
        sys.exit(1)

    config_path = sys.argv[1]
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # Flatten config sections into single dict
    config = {}
    for section in ("static", "universe", "daily_signal", "intraday_entry",
                     "intraday_exit", "simulation"):
        if section in raw:
            config.update(raw[section])

    output = run_pipeline(config)

    # Save results
    output_path = None
    if "--output" in sys.argv:
        output_path = sys.argv[sys.argv.index("--output") + 1]
    if output_path:
        # Strip equity_points and trade_log for compact output
        save_data = {
            **{k: v for k, v in output.items() if k != "results"},
            "results": [
                {k: v for k, v in r.items() if k not in ("equity_points", "trade_log")}
                for r in output["results"]
            ],
        }
        with open(output_path, "w") as f:
            json.dump(save_data, f, indent=2)
        print(f"  Saved {output_path}")

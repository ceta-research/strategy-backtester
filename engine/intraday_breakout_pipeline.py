"""Intraday breakout pipeline — day-trading version of eod_breakout.

Hybrid daily + minute architecture:
  Phase 1: Fetch daily OHLCV, compute universe + breakout signals + regime
  Phase 2: For eligible stocks on each day, fetch minute bars
  Phase 3: Simulate intraday entries/exits, accumulate equity

Data is processed month-by-month for memory management.
Minute data is only fetched for stocks passing the daily signal filter.

Usage:
    python -m engine.intraday_breakout_pipeline strategies/intraday_breakout/config.yaml
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from itertools import product

import polars as pl
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from engine.charges import nse_intraday_charges
from engine.internal_regime import compute_internal_regime_epochs_hysteresis, compute_internal_regime_epochs
from lib.backtest_result import BacktestResult, SweepResult
from lib.cr_client import CetaResearch
from lib.equity_curve import Frequency

SECONDS_IN_ONE_DAY = 86400


# ── Data fetching ────────────────────────────────────────────────────────

def fetch_daily_data(client: CetaResearch, start_date: str, end_date: str,
                     prefetch_days: int = 500) -> pl.DataFrame:
    """Fetch daily OHLCV for all NSE stocks."""
    # Compute prefetch start
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    pf_dt = dt - timedelta(days=int(prefetch_days * 1.5))  # calendar days
    pf_date = pf_dt.strftime("%Y-%m-%d")

    sql = f"""
    SELECT
        symbol AS instrument,
        CAST(epoch(date) AS BIGINT) AS date_epoch,
        open, high, low, close, volume,
        close * volume AS turnover
    FROM nse.nse_charting_day
    WHERE date BETWEEN '{pf_date}' AND '{end_date}'
      AND close > 0
      AND volume > 0
    ORDER BY symbol, date
    """
    print(f"  Fetching daily OHLCV: {pf_date} to {end_date}")
    rows = client.query(sql, memory_mb=16384, threads=6, timeout=600,
                        format="parquet")
    df = pl.DataFrame(rows) if isinstance(rows, list) else rows
    print(f"  Daily data: {df.height} rows, {df['instrument'].n_unique()} instruments")
    return df


def fetch_minute_bars(client: CetaResearch, symbols: list[str],
                      date_str: str) -> pl.DataFrame:
    """Fetch minute bars for specific symbols on a specific date.

    FMP minute data: timestamps are LOCAL time labeled UTC.
    NSE session: 09:15 - 15:30 (375 bars).
    """
    if not symbols:
        return pl.DataFrame()

    # Build symbol list for SQL
    sym_list = ", ".join(f"'{s}'" for s in symbols)

    sql = f"""
    SELECT
        symbol,
        date AS bar_timestamp,
        CAST(epoch(date) AS BIGINT) AS bar_epoch,
        open, high, low, close, volume
    FROM fmp.stock_prices_minute
    WHERE symbol IN ({sym_list})
      AND CAST(date AS DATE) = '{date_str}'
      AND EXTRACT(HOUR FROM date) >= 9
      AND EXTRACT(HOUR FROM date) < 16
    ORDER BY symbol, date
    """
    try:
        rows = client.query(sql, memory_mb=4096, threads=4, timeout=120,
                            format="parquet")
        df = pl.DataFrame(rows) if isinstance(rows, list) else rows
        return df
    except Exception as e:
        print(f"    Minute fetch failed for {date_str}: {e}")
        return pl.DataFrame()


def fetch_minute_bars_batch(client: CetaResearch,
                            daily_signals: dict[int, list[str]],
                            month_start: str, month_end: str) -> dict:
    """Fetch minute bars for all eligible stocks in a month, one query.

    Returns dict of {date_str: pl.DataFrame of minute bars for that day}.
    """
    # Collect all unique symbols needed this month
    all_symbols = set()
    date_strs = {}
    for epoch, symbols in daily_signals.items():
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        date_strs[epoch] = date_str
        for s in symbols:
            # Convert instrument format: NSE:SYMBOL -> SYMBOL.NS (FMP format)
            fmp_sym = s.replace("NSE:", "") + ".NS"
            all_symbols.add(fmp_sym)

    if not all_symbols:
        return {}

    sym_list = ", ".join(f"'{s}'" for s in sorted(all_symbols))

    sql = f"""
    SELECT
        symbol,
        CAST(date AS DATE) AS trade_date,
        date AS bar_timestamp,
        EXTRACT(HOUR FROM date) * 60 + EXTRACT(MINUTE FROM date) AS bar_minute,
        open, high, low, close, volume
    FROM fmp.stock_prices_minute
    WHERE symbol IN ({sym_list})
      AND CAST(date AS DATE) BETWEEN '{month_start}' AND '{month_end}'
      AND EXTRACT(HOUR FROM date) >= 9
      AND EXTRACT(HOUR FROM date) < 16
    ORDER BY symbol, date
    """
    print(f"    Fetching minute bars: {len(all_symbols)} symbols, {month_start} to {month_end}")
    try:
        rows = client.query(sql, memory_mb=8192, threads=6, timeout=300,
                            format="parquet")
        df = pl.DataFrame(rows) if isinstance(rows, list) else rows
        print(f"    Minute data: {df.height} rows")

        # Group by trade_date
        result = {}
        if df.height > 0:
            for date_val in df["trade_date"].unique().to_list():
                day_df = df.filter(pl.col("trade_date") == date_val)
                result[str(date_val)] = day_df

        return result
    except Exception as e:
        print(f"    Minute batch fetch failed: {e}")
        return {}


# ── Universe selection ───────────────────────────────────────────────────

def select_universe(df_daily: pl.DataFrame, month_epoch: int,
                    top_n: int = 100,
                    min_turnover: float = 500_000_000) -> list[str]:
    """Select top N stocks by avg daily turnover in the prior month."""
    # Prior month window: 30 trading days before month_epoch
    window_start = month_epoch - 30 * SECONDS_IN_ONE_DAY
    window_end = month_epoch

    df_window = df_daily.filter(
        (pl.col("date_epoch") >= window_start)
        & (pl.col("date_epoch") < window_end)
    )

    avg_turnover = (
        df_window.group_by("instrument")
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
    regime_sma_period: int = 50,
    regime_entry_threshold: float = 0.4,
    regime_exit_threshold: float = 0.35,
) -> dict[int, list[str]]:
    """Compute breakout + regime signals on daily data.

    Returns dict mapping date_epoch -> list of eligible instruments.
    Only dates within [start_epoch, end_epoch] are returned.
    """
    df = df_daily.filter(pl.col("instrument").is_in(universe))
    df = df.sort(["instrument", "date_epoch"])

    # Compute indicators
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

    # Compute regime: internal breadth with hysteresis
    # Need scanner_config_ids for internal regime — mark all as passed
    df_for_regime = df.with_columns(
        pl.lit("1").alias("scanner_config_ids")
    )

    if regime_exit_threshold > 0:
        bull_epochs = compute_internal_regime_epochs_hysteresis(
            df_for_regime, sma_period=regime_sma_period,
            entry_threshold=regime_entry_threshold,
            exit_threshold=regime_exit_threshold,
        )
    else:
        bull_epochs = compute_internal_regime_epochs(
            df_for_regime, sma_period=regime_sma_period,
            threshold=regime_entry_threshold,
        )

    n_bull = len([e for e in bull_epochs if start_epoch <= e <= end_epoch])
    total_days = len(df.filter(
        (pl.col("date_epoch") >= start_epoch)
        & (pl.col("date_epoch") <= end_epoch)
    )["date_epoch"].unique())
    print(f"    Regime: {n_bull} bull days (universe breadth, hysteresis {regime_entry_threshold}/{regime_exit_threshold})")

    # Filter to sim range
    df = df.filter(
        (pl.col("date_epoch") >= start_epoch)
        & (pl.col("date_epoch") <= end_epoch)
    )

    # Entry filter: breakout + bullish candle + regime
    df_signals = df.filter(
        (pl.col("close") >= pl.col("n_day_high_val"))
        & (pl.col("close") > pl.col("n_day_ma"))
        & (pl.col("close") > pl.col("open"))
        & (pl.col("date_epoch").is_in(list(bull_epochs)))
    )

    # Group by date_epoch -> list of eligible instruments
    signals: dict[int, list[str]] = {}
    for row in df_signals.select(["date_epoch", "instrument", "close"]).to_dicts():
        epoch = row["date_epoch"]
        if epoch not in signals:
            signals[epoch] = []
        signals[epoch].append(row["instrument"])

    print(f"    Daily signals: {len(signals)} days with entries, "
          f"{sum(len(v) for v in signals.values())} total candidates")

    return signals


# ── Intraday simulation ──────────────────────────────────────────────────

def simulate_intraday_day(
    minute_df: pl.DataFrame,
    eligible_instruments: list[str],
    prior_day_highs: dict[str, float],
    config: dict,
    margin: float,
) -> tuple[list[dict], float]:
    """Simulate one trading day on minute bars.

    Entry: price breaks above prior day's high.
    Exit: target/stop/trailing/time-stop/EOD close.

    Returns (trade_list, day_pnl).
    """
    target_pct = config.get("target_pct", 1.5) / 100
    stop_pct = config.get("stop_pct", 0.75) / 100
    trailing_stop_pct = config.get("trailing_stop_pct", 0) / 100
    max_entry_bar = config.get("max_entry_bar", 120)  # max bar_minute for entry
    eod_exit_minute = config.get("eod_exit_minute", 925)  # 15:25 = 925
    max_positions = config.get("max_positions", 5)
    order_value = margin / max_positions if max_positions > 0 else 0

    if order_value <= 0 or minute_df.is_empty():
        return [], 0.0

    trades = []
    day_pnl = 0.0
    positions_taken = 0

    for inst in eligible_instruments:
        if positions_taken >= max_positions:
            break

        # Get instrument's FMP symbol
        fmp_sym = inst.replace("NSE:", "") + ".NS"
        prior_high = prior_day_highs.get(inst)
        if prior_high is None or prior_high <= 0:
            continue

        # Get this instrument's minute bars, sorted by time
        inst_bars = minute_df.filter(pl.col("symbol") == fmp_sym).sort("bar_minute")
        if inst_bars.is_empty():
            continue

        bars = inst_bars.to_dicts()

        # Find entry: first bar where high > prior_day_high within max_entry_bar
        entry_price = None
        entry_idx = None
        entry_minute = None

        for i, bar in enumerate(bars):
            if bar["bar_minute"] > max_entry_bar + 555:  # 555 = 9*60+15 (session start)
                break
            if bar["high"] and bar["high"] > prior_high:
                # Entry at breakout price (prior_high + small buffer)
                entry_price = prior_high
                entry_idx = i
                entry_minute = bar["bar_minute"]
                break

        if entry_price is None:
            continue

        # Simulate exit from entry bar onward
        max_price = entry_price
        exit_price = None
        exit_type = None

        for j in range(entry_idx, len(bars)):
            bar = bars[j]
            bar_high = bar["high"] or entry_price
            bar_low = bar["low"] or entry_price
            bar_close = bar["close"] or entry_price

            max_price = max(max_price, bar_high)

            # Target check
            if target_pct > 0 and bar_high >= entry_price * (1 + target_pct):
                exit_price = entry_price * (1 + target_pct)
                exit_type = "target"
                break

            # Stop check
            if stop_pct > 0 and bar_low <= entry_price * (1 - stop_pct):
                exit_price = entry_price * (1 - stop_pct)
                exit_type = "stop"
                break

            # Trailing stop check
            if trailing_stop_pct > 0 and max_price > entry_price:
                trail_stop = max_price * (1 - trailing_stop_pct)
                if bar_low <= trail_stop:
                    exit_price = trail_stop
                    exit_type = "trailing_stop"
                    break

            # EOD exit
            if bar["bar_minute"] >= eod_exit_minute:
                exit_price = bar_close
                exit_type = "eod_close"
                break

        # If no exit triggered, use last bar's close
        if exit_price is None and bars:
            exit_price = bars[-1]["close"] or entry_price
            exit_type = "eod_close"

        if exit_price is None or exit_price <= 0:
            continue

        # Compute P&L with intraday charges
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

    return trades, day_pnl


# ── Pipeline orchestrator ────────────────────────────────────────────────

def run_intraday_breakout_pipeline(config_path: str) -> SweepResult:
    """Run the full intraday breakout backtest."""
    pipeline_start = time.time()

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    static = raw["static"]
    start_date = static["start_date"]
    end_date = static["end_date"]
    initial_capital = static.get("initial_capital", 1_000_000)
    chunk_months = static.get("chunk_months", 1)

    universe_cfg = raw.get("universe", {})
    daily_cfg = raw.get("daily_signal", {})
    entry_cfg = raw.get("intraday_entry", {})
    exit_cfg = raw.get("intraday_exit", {})
    sim_cfg = raw.get("simulation", {})

    print(f"Loading config: {config_path}")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Initial capital: {initial_capital:,}")

    # Build param combos for sweep
    sweep_params = {}
    for section in (universe_cfg, daily_cfg, entry_cfg, exit_cfg, sim_cfg):
        for k, v in section.items():
            sweep_params[k] = v if isinstance(v, list) else [v]

    combos = list(_cartesian(sweep_params))
    print(f"  Config combinations: {len(combos)}")

    sweep = SweepResult("intraday_breakout", "PORTFOLIO", "NSE",
                        initial_capital, description="Intraday breakout sweep")

    client = CetaResearch()

    # Fetch daily data once (covers full range + prefetch)
    df_daily = fetch_daily_data(client, start_date, end_date,
                                prefetch_days=static.get("prefetch_days", 500))

    start_epoch = int(datetime.strptime(start_date, "%Y-%m-%d")
                      .replace(tzinfo=timezone.utc).timestamp())
    end_epoch = int(datetime.strptime(end_date, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp())

    for cfg_idx, params in enumerate(combos):
        print(f"\n--- Config {cfg_idx + 1}/{len(combos)}: {params} ---")

        top_n = params.get("top_n", 100)
        min_turnover = params.get("min_avg_turnover", 500_000_000)

        # Build month boundaries
        months = _month_boundaries(start_date, end_date)
        print(f"  Processing {len(months)} months")

        margin = float(initial_capital)
        all_trades = []
        equity_points = []  # (epoch, value)
        equity_points.append((start_epoch, margin))

        for month_start, month_end, month_epoch in months:
            # Phase 1: Universe selection
            universe = select_universe(df_daily, month_epoch, top_n, min_turnover)
            if not universe:
                continue

            # Phase 2: Daily signals for this month
            m_start_epoch = int(datetime.strptime(month_start, "%Y-%m-%d")
                                .replace(tzinfo=timezone.utc).timestamp())
            m_end_epoch = int(datetime.strptime(month_end, "%Y-%m-%d")
                              .replace(tzinfo=timezone.utc).timestamp())

            signals = compute_daily_signals(
                df_daily, universe, m_start_epoch, m_end_epoch,
                n_day_high=params.get("n_day_high", 3),
                n_day_ma=params.get("n_day_ma", 10),
                regime_sma_period=params.get("internal_regime_sma_period", 50),
                regime_entry_threshold=params.get("internal_regime_threshold", 0.4),
                regime_exit_threshold=params.get("internal_regime_exit_threshold", 0.35),
            )

            if not signals:
                print(f"    {month_start}: 0 signal days, skipping minute fetch")
                continue

            # Compute prior-day highs for the universe
            prior_highs = _compute_prior_day_highs(df_daily, universe,
                                                    m_start_epoch, m_end_epoch)

            # Phase 3: Fetch minute data for eligible stocks
            minute_data = fetch_minute_bars_batch(
                client, signals, month_start, month_end
            )

            # Phase 4: Simulate each day
            month_trades = 0
            for epoch in sorted(signals.keys()):
                dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")

                # The signal fires on day T. We trade on day T+1.
                # Find the next trading day's date
                next_date = _next_trading_day(df_daily, epoch, universe)
                if next_date is None:
                    continue

                minute_df = minute_data.get(next_date, pl.DataFrame())
                if minute_df.is_empty():
                    continue

                day_trades, day_pnl = simulate_intraday_day(
                    minute_df, signals[epoch],
                    prior_highs.get(epoch, {}),
                    params, margin,
                )

                margin += day_pnl
                all_trades.extend([
                    {**t, "trade_date": next_date} for t in day_trades
                ])
                month_trades += len(day_trades)

                # Record equity point
                next_epoch = int(datetime.strptime(next_date, "%Y-%m-%d")
                                 .replace(tzinfo=timezone.utc).timestamp())
                equity_points.append((next_epoch, margin))

            print(f"    {month_start}: {month_trades} trades, "
                  f"margin={margin:,.0f}")

            gc.collect()

        # Build BacktestResult
        br = BacktestResult("intraday_breakout", params, "PORTFOLIO", "NSE",
                            initial_capital, risk_free_rate=0.065,
                            equity_curve_frequency=Frequency.DAILY_TRADING)

        for ep, val in equity_points:
            br.add_equity_point(ep, val)

        for t in all_trades:
            trade_epoch = int(datetime.strptime(t["trade_date"], "%Y-%m-%d")
                              .replace(tzinfo=timezone.utc).timestamp())
            qty = max(int(t["order_value"] / t["entry_price"]), 1) if t["entry_price"] > 0 else 1
            br.add_trade(trade_epoch, trade_epoch, t["entry_price"],
                         t["exit_price"], qty, charges=t["charges"])

        sweep.add_config(params, br)

        s = br.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        max_dd = (s.get("max_drawdown") or 0) * 100
        sharpe = s.get("sharpe_ratio") or 0
        calmar = s.get("calmar_ratio") or 0
        print(f"\n  Result: CAGR={cagr:.2f}% MDD={max_dd:.2f}% "
              f"Sharpe={sharpe:.3f} Calmar={calmar:.3f} "
              f"Trades={len(all_trades)}")

    elapsed = round(time.time() - pipeline_start, 1)
    print(f"\n--- Intraday Breakout Pipeline Complete: {elapsed}s ---")

    return sweep


# ── Helpers ──────────────────────────────────────────────────────────────

def _month_boundaries(start_date: str, end_date: str) -> list[tuple]:
    """Generate (month_start, month_end, month_start_epoch) tuples."""
    months = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    while dt <= end_dt:
        month_start = dt.strftime("%Y-%m-%d")
        # Month end: last day of this month
        if dt.month == 12:
            next_month = dt.replace(year=dt.year + 1, month=1, day=1)
        else:
            next_month = dt.replace(month=dt.month + 1, day=1)
        month_end_dt = next_month - timedelta(days=1)
        if month_end_dt > end_dt:
            month_end_dt = end_dt
        month_end = month_end_dt.strftime("%Y-%m-%d")

        month_epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
        months.append((month_start, month_end, month_epoch))

        dt = next_month

    return months


def _compute_prior_day_highs(df_daily: pl.DataFrame, universe: list[str],
                              start_epoch: int, end_epoch: int) -> dict:
    """Compute prior day's high for each instrument on each date.

    Returns: {date_epoch: {instrument: prior_day_high}}
    """
    df = (df_daily
          .filter(pl.col("instrument").is_in(universe))
          .sort(["instrument", "date_epoch"]))

    df = df.with_columns(
        pl.col("high").shift(1).over("instrument").alias("prior_high")
    )

    df = df.filter(
        (pl.col("date_epoch") >= start_epoch)
        & (pl.col("date_epoch") <= end_epoch)
        & pl.col("prior_high").is_not_null()
    )

    result: dict = {}
    for row in df.select(["date_epoch", "instrument", "prior_high"]).to_dicts():
        epoch = row["date_epoch"]
        if epoch not in result:
            result[epoch] = {}
        result[epoch][row["instrument"]] = row["prior_high"]

    return result


def _next_trading_day(df_daily: pl.DataFrame, epoch: int,
                       universe: list[str]) -> str | None:
    """Find the next trading day after epoch for any instrument in universe."""
    next_days = (df_daily
                 .filter(
                     (pl.col("date_epoch") > epoch)
                     & pl.col("instrument").is_in(universe)
                 )
                 .select("date_epoch")
                 .unique()
                 .sort("date_epoch")
                 .head(1))

    if next_days.is_empty():
        return None

    next_epoch = next_days["date_epoch"][0]
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _cartesian(params: dict) -> list[dict]:
    """Cartesian product of param lists."""
    if not params:
        return [{}]
    keys = list(params.keys())
    vals = [params[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in product(*vals)]


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m engine.intraday_breakout_pipeline <config.yaml> [--output path]")
        sys.exit(1)

    config_path = sys.argv[1]
    output_path = None
    if "--output" in sys.argv:
        output_path = sys.argv[sys.argv.index("--output") + 1]

    sweep = run_intraday_breakout_pipeline(config_path)

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        sweep.save(output_path)
        print(f"  Saved {output_path}")

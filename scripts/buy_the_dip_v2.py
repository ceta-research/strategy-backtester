#!/usr/bin/env python3
"""Buy-the-dip v2: Composite technical indicator scoring on NIFTYBEES.

Entry: Composite score from RSI(2), RSI(5), Bollinger Bands, IBS, SMA(50), drawdown.
Buy when score >= threshold, deploy X% of available cash.
Exit: Price recovers above N-day MA AND in profit, OR hold forever.
"""

import sys
import os
import math
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
import polars as pl


@dataclass
class Config:
    # Entry scoring thresholds
    rsi2_threshold: float = 15.0     # RSI(2) < this = +2 points
    rsi5_threshold: float = 30.0     # RSI(5) < this = +1 point
    bb_period: int = 20              # Bollinger Band period
    bb_std: float = 2.0              # Bollinger Band std devs
    ibs_threshold: float = 0.2       # IBS < this = +1 point
    sma_period: int = 50             # Below SMA(N) = +1 point
    drawdown_lookback: int = 50      # N-day high for drawdown
    drawdown_threshold: float = 5.0  # Drawdown > X% = +1 point

    # Buy trigger
    min_score: int = 3               # Buy when composite score >= this
    buy_fraction: float = 0.5        # Deploy X% of available cash
    min_days_between_buys: int = 5   # Min days between buys
    min_cash_reserve: float = 0.05   # Keep 5% reserve

    # Exit
    exit_mode: str = "recovery_ma"   # "never", "recovery_ma"
    exit_ma_period: int = 20         # Sell when price > SMA(N) AND in profit
    sell_all_on_exit: bool = True

    # Capital
    start_capital: float = 10_000_000


def compute_rsi(closes, period):
    """Compute RSI using Wilder's smoothing. Returns list same length as closes."""
    rsi = [50.0] * len(closes)  # default neutral
    if len(closes) < period + 1:
        return rsi

    # Initial avg gain/loss
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period, len(closes)):
        if i > period:
            change = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period

        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def compute_sma(values, period):
    """Simple moving average. Returns list same length."""
    sma = [None] * len(values)
    running = 0
    for i in range(len(values)):
        running += values[i]
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            sma[i] = running / period
        else:
            sma[i] = running / (i + 1)  # partial window
    return sma


def compute_bb(closes, period, num_std):
    """Bollinger Bands. Returns (upper, middle, lower) lists."""
    middle = compute_sma(closes, period)
    upper = [0.0] * len(closes)
    lower = [0.0] * len(closes)

    for i in range(len(closes)):
        start = max(0, i - period + 1)
        window = closes[start:i + 1]
        if len(window) < 2:
            upper[i] = middle[i]
            lower[i] = middle[i]
            continue
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = math.sqrt(variance)
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std

    return upper, middle, lower


def fetch_niftybees(start_epoch, end_epoch):
    cr = CetaResearch()
    sql = f"""SELECT date_epoch, open, high, low, close, volume
              FROM nse.nse_charting_day
              WHERE symbol = 'NIFTYBEES'
                AND date_epoch >= {start_epoch} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    print("Fetching NIFTYBEES data...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return pl.DataFrame()
    df = pl.DataFrame(results).with_columns([
        pl.col("date_epoch").cast(pl.Int64),
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
    ]).sort("date_epoch")
    return df


@dataclass
class Position:
    entry_epoch: int
    entry_price: float
    quantity: float
    cost: float


def simulate(df: pl.DataFrame, cfg: Config):
    closes = df["close"].to_list()
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    epochs = df["date_epoch"].to_list()
    n = len(closes)

    # Precompute indicators
    rsi2 = compute_rsi(closes, 2)
    rsi5 = compute_rsi(closes, 5)
    sma50 = compute_sma(closes, cfg.sma_period)
    bb_upper, bb_mid, bb_lower = compute_bb(closes, cfg.bb_period, cfg.bb_std)
    exit_ma = compute_sma(closes, cfg.exit_ma_period)

    cash = cfg.start_capital
    positions = []
    last_buy_idx = -999
    total_buys = 0
    total_sells = 0
    day_log = []
    score_log = []

    for i in range(n):
        close = closes[i]
        high = highs[i]
        low = lows[i]
        epoch = epochs[i]

        # IBS
        ibs = (close - low) / (high - low) if (high - low) > 0 else 0.5

        # Drawdown from N-day high
        dd_start = max(0, i - cfg.drawdown_lookback + 1)
        rolling_peak = max(closes[dd_start:i + 1])
        drawdown_pct = (rolling_peak - close) / rolling_peak * 100 if rolling_peak > 0 else 0

        # --- COMPOSITE SCORE ---
        score = 0
        signals = []

        if rsi2[i] < cfg.rsi2_threshold:
            score += 2
            signals.append(f"RSI2={rsi2[i]:.0f}")

        if rsi5[i] < cfg.rsi5_threshold:
            score += 1
            signals.append(f"RSI5={rsi5[i]:.0f}")

        if close < bb_lower[i]:
            score += 2
            signals.append("BB_low")

        if ibs < cfg.ibs_threshold:
            score += 1
            signals.append(f"IBS={ibs:.2f}")

        if sma50[i] is not None and close < sma50[i]:
            score += 1
            signals.append("<SMA50")

        if drawdown_pct >= cfg.drawdown_threshold:
            score += 1
            signals.append(f"DD={drawdown_pct:.1f}%")

        # --- EXIT LOGIC ---
        if positions and cfg.exit_mode == "recovery_ma":
            avg_entry = sum(p.cost for p in positions) / sum(p.quantity for p in positions)
            if close > exit_ma[i] and close > avg_entry:
                total_qty = sum(p.quantity for p in positions)
                cash += total_qty * close
                total_sells += len(positions)
                positions = []

        # --- ENTRY LOGIC ---
        days_since_buy = i - last_buy_idx
        min_cash = cfg.start_capital * cfg.min_cash_reserve

        if (score >= cfg.min_score
                and days_since_buy >= cfg.min_days_between_buys
                and cash > min_cash):
            buy_amount = (cash - min_cash) * cfg.buy_fraction
            if buy_amount > 0 and close > 0:
                qty = buy_amount / close
                positions.append(Position(epoch, close, qty, buy_amount))
                cash -= buy_amount
                total_buys += 1
                last_buy_idx = i

                if total_buys <= 10 or total_buys % 50 == 0:
                    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                    score_log.append(f"  BUY #{total_buys} {dt} @ {close:.1f} score={score} [{', '.join(signals)}]")

        # Portfolio value
        position_value = sum(p.quantity * close for p in positions)
        total_value = cash + position_value

        day_log.append({
            "epoch": epoch,
            "close": close,
            "total_value": total_value,
            "cash": cash,
            "position_value": position_value,
            "n_positions": len(positions),
        })

    return day_log, total_buys, total_sells, score_log


def compute_metrics(day_log):
    if len(day_log) < 2:
        return {}
    values = [d["total_value"] for d in day_log]
    start_val = values[0]
    end_val = values[-1]

    start_epoch = day_log[0]["epoch"]
    end_epoch = day_log[-1]["epoch"]
    years = (end_epoch - start_epoch) / (365.25 * 86400)
    total_return = end_val / start_val
    cagr = (total_return ** (1 / years) - 1) * 100 if years > 0 and total_return > 0 else 0

    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    yearly = {}
    for d in day_log:
        yr = datetime.fromtimestamp(d["epoch"], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": d["total_value"], "last": d["total_value"],
                          "peak": d["total_value"], "trough": d["total_value"]}
        yearly[yr]["last"] = d["total_value"]
        yearly[yr]["peak"] = max(yearly[yr]["peak"], d["total_value"])
        yearly[yr]["trough"] = min(yearly[yr]["trough"], d["total_value"])

    return {"cagr": cagr, "max_dd": max_dd, "total_return": total_return, "years": years, "yearly": yearly}


def run_sweep(df):
    """Sweep key parameters."""
    configs = []

    for min_score in [2, 3, 4, 5]:
        for buy_frac in [0.3, 0.5, 0.7]:
            for min_days in [3, 5, 10]:
                for exit_mode in ["never", "recovery_ma"]:
                    for exit_ma in [10, 20, 50]:
                        if exit_mode != "recovery_ma" and exit_ma != 20:
                            continue
                        for rsi2_thresh in [10, 15, 20]:
                            for dd_thresh in [3, 5, 8]:
                                configs.append(Config(
                                    min_score=min_score,
                                    buy_fraction=buy_frac,
                                    min_days_between_buys=min_days,
                                    exit_mode=exit_mode,
                                    exit_ma_period=exit_ma,
                                    rsi2_threshold=rsi2_thresh,
                                    drawdown_threshold=dd_thresh,
                                ))

    print(f"\nSweeping {len(configs)} configs...")
    results = []

    for i, cfg in enumerate(configs):
        day_log, buys, sells, _ = simulate(df, cfg)
        metrics = compute_metrics(day_log)
        if metrics and metrics["cagr"] != 0:
            calmar = metrics["cagr"] / abs(metrics["max_dd"]) if metrics["max_dd"] != 0 else 0
            results.append({
                "cfg": cfg, "cagr": metrics["cagr"], "max_dd": metrics["max_dd"],
                "calmar": calmar, "total_return": metrics["total_return"],
                "buys": buys, "sells": sells, "metrics": metrics,
            })
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(configs)} done...")

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*130}")
    print(f"{'#':<4} {'Score':>5} {'RSI2':>5} {'DD%':>4} {'BuyF':>5} {'MinD':>5} {'Exit':>12} {'ExMA':>5} "
          f"{'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Growth':>8} {'Buys':>5} {'Sells':>5}")
    print(f"{'='*130}")

    for i, r in enumerate(results[:25]):
        c = r["cfg"]
        print(f"{i+1:<4} {c.min_score:>5} {c.rsi2_threshold:>5.0f} {c.drawdown_threshold:>4.0f} "
              f"{c.buy_fraction:>5.1f} {c.min_days_between_buys:>5} {c.exit_mode:>12} {c.exit_ma_period:>5} "
              f"{r['cagr']:>7.1f}% {r['max_dd']:>7.1f}% {r['calmar']:>8.2f} "
              f"{r['total_return']:>7.1f}x {r['buys']:>5} {r['sells']:>5}")

    # Run best config with detailed output
    best_cfg = results[0]["cfg"]
    print(f"\n{'='*80}")
    print(f"BEST CONFIG (by Calmar):")
    print(f"  min_score={best_cfg.min_score}, RSI2<{best_cfg.rsi2_threshold}, DD>{best_cfg.drawdown_threshold}%")
    print(f"  buy_frac={best_cfg.buy_fraction}, min_days={best_cfg.min_days_between_buys}")
    print(f"  exit={best_cfg.exit_mode}, exit_ma={best_cfg.exit_ma_period}d")
    print(f"{'='*80}")

    day_log, buys, sells, score_log = simulate(df, best_cfg)
    metrics = compute_metrics(day_log)

    print(f"\nSample trades:")
    for line in score_log[:15]:
        print(line)
    if len(score_log) > 15:
        print(f"  ... ({len(score_log)} total logged)")

    print(f"\n{'Year':<6} {'Return':>10} {'Max DD':>10} {'End Value':>14}")
    print("-" * 45)
    for yr in sorted(metrics["yearly"].keys()):
        y = metrics["yearly"][yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100
        print(f"{yr:<6} {ret:>+9.1f}% {dd:>9.1f}% {y['last']:>14,.0f}")

    # Also show best "never sell" for comparison
    never_results = [r for r in results if r["cfg"].exit_mode == "never"]
    if never_results:
        best_never = never_results[0]
        c = best_never["cfg"]
        print(f"\nBEST 'NEVER SELL' CONFIG:")
        print(f"  min_score={c.min_score}, RSI2<{c.rsi2_threshold}, DD>{c.drawdown_threshold}%")
        print(f"  buy_frac={c.buy_fraction}, min_days={c.min_days_between_buys}")
        print(f"  CAGR={best_never['cagr']:.1f}%, MaxDD={best_never['max_dd']:.1f}%, "
              f"Calmar={best_never['calmar']:.2f}, Growth={best_never['total_return']:.1f}x, "
              f"Buys={best_never['buys']}")

        day_log2, _, _, _ = simulate(df, c)
        metrics2 = compute_metrics(day_log2)
        print(f"\n{'Year':<6} {'Return':>10} {'Max DD':>10} {'End Value':>14}")
        print("-" * 45)
        for yr in sorted(metrics2["yearly"].keys()):
            y = metrics2["yearly"][yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100
            print(f"{yr:<6} {ret:>+9.1f}% {dd:>9.1f}% {y['last']:>14,.0f}")

    return results


def main():
    start_epoch = 1104537600  # 2005-01-01
    end_epoch = 1773878400    # 2026-03-19

    df = fetch_niftybees(start_epoch, end_epoch)
    if df.is_empty():
        print("No data")
        return

    print(f"  {df.height} rows, {datetime.fromtimestamp(df['date_epoch'].min(), tz=timezone.utc).date()} "
          f"to {datetime.fromtimestamp(df['date_epoch'].max(), tz=timezone.utc).date()}")

    run_sweep(df)


if __name__ == "__main__":
    main()

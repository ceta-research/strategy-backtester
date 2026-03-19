#!/usr/bin/env python3
"""Buy-the-dip strategy on NIFTYBEES.

Buys fractions of available cash as the price drops from recent highs.
Sells when price recovers above a moving average (or holds forever).
"""

import sys
import os
import io
from datetime import datetime, timezone
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
import polars as pl


@dataclass
class Position:
    entry_epoch: int
    entry_price: float
    quantity: float
    cost: float  # entry_price * quantity


@dataclass
class Config:
    # When to buy
    peak_lookback: int = 50         # N-day rolling high to measure drawdown
    dip_threshold: float = 5.0      # buy when price drops X% from peak
    buy_fraction: float = 0.5       # buy with X% of available cash
    min_days_between_buys: int = 5  # wait N days between buys
    min_cash_reserve: float = 0.05  # keep 5% as reserve (never go all-in)

    # When to sell
    exit_mode: str = "recovery_ma"  # "never", "recovery_ma", "recovery_pct"
    exit_ma_period: int = 20        # sell when price > N-day MA (for recovery_ma)
    exit_recovery_pct: float = 5.0  # sell when price rises X% from trough (for recovery_pct)
    sell_all_on_exit: bool = True   # sell all positions at once, or one at a time

    # Capital
    start_capital: float = 10_000_000  # 1 Cr


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


def simulate(df: pl.DataFrame, cfg: Config):
    closes = df["close"].to_list()
    epochs = df["date_epoch"].to_list()
    opens = df["open"].to_list()
    n = len(closes)

    cash = cfg.start_capital
    positions: list[Position] = []
    last_buy_idx = -999
    total_invested = 0.0
    total_buys = 0
    total_sells = 0

    # Day-wise log
    day_log = []

    # Precompute rolling high and MA
    peak_lookback = cfg.peak_lookback
    ma_period = cfg.exit_ma_period

    for i in range(n):
        close = closes[i]
        epoch = epochs[i]

        # Rolling peak (N-day high of close)
        start_idx = max(0, i - peak_lookback + 1)
        rolling_peak = max(closes[start_idx:i + 1])

        # Rolling MA
        ma_start = max(0, i - ma_period + 1)
        rolling_ma = sum(closes[ma_start:i + 1]) / (i - ma_start + 1)

        # Drawdown from peak
        drawdown_pct = (rolling_peak - close) / rolling_peak * 100 if rolling_peak > 0 else 0

        # --- EXIT LOGIC ---
        if positions and cfg.exit_mode != "never":
            should_exit = False

            if cfg.exit_mode == "recovery_ma" and close > rolling_ma:
                # Only exit if we're in profit (price above avg entry)
                avg_entry = sum(p.cost for p in positions) / sum(p.quantity for p in positions)
                if close > avg_entry:
                    should_exit = True

            elif cfg.exit_mode == "recovery_pct":
                avg_entry = sum(p.cost for p in positions) / sum(p.quantity for p in positions)
                gain_pct = (close - avg_entry) / avg_entry * 100
                if gain_pct > cfg.exit_recovery_pct:
                    should_exit = True

            if should_exit:
                if cfg.sell_all_on_exit:
                    total_qty = sum(p.quantity for p in positions)
                    proceeds = total_qty * close
                    cash += proceeds
                    total_sells += len(positions)
                    positions = []
                else:
                    # Sell oldest position
                    p = positions.pop(0)
                    proceeds = p.quantity * close
                    cash += proceeds
                    total_sells += 1

        # --- ENTRY LOGIC ---
        days_since_buy = i - last_buy_idx
        min_cash = cfg.start_capital * cfg.min_cash_reserve

        if (drawdown_pct >= cfg.dip_threshold
                and days_since_buy >= cfg.min_days_between_buys
                and cash > min_cash):

            buy_amount = (cash - min_cash) * cfg.buy_fraction
            if buy_amount > 0 and close > 0:
                qty = buy_amount / close
                positions.append(Position(
                    entry_epoch=epoch,
                    entry_price=close,
                    quantity=qty,
                    cost=buy_amount,
                ))
                cash -= buy_amount
                total_invested += buy_amount
                total_buys += 1
                last_buy_idx = i

        # Portfolio value
        position_value = sum(p.quantity * close for p in positions)
        total_value = cash + position_value

        day_log.append({
            "epoch": epoch,
            "close": close,
            "cash": cash,
            "position_value": position_value,
            "total_value": total_value,
            "n_positions": len(positions),
            "drawdown_pct": drawdown_pct,
            "rolling_ma": rolling_ma,
        })

    return day_log, total_buys, total_sells


def compute_metrics(day_log, cfg):
    if len(day_log) < 2:
        return {}

    values = [d["total_value"] for d in day_log]
    start_val = values[0]
    end_val = values[-1]

    # CAGR
    start_epoch = day_log[0]["epoch"]
    end_epoch = day_log[-1]["epoch"]
    years = (end_epoch - start_epoch) / (365.25 * 86400)
    total_return = end_val / start_val
    cagr = (total_return ** (1 / years) - 1) * 100 if years > 0 else 0

    # Max drawdown
    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # Year-wise returns
    yearly = {}
    for d in day_log:
        yr = datetime.fromtimestamp(d["epoch"], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": d["total_value"], "last": d["total_value"],
                          "peak": d["total_value"], "trough": d["total_value"]}
        yearly[yr]["last"] = d["total_value"]
        yearly[yr]["peak"] = max(yearly[yr]["peak"], d["total_value"])
        yearly[yr]["trough"] = min(yearly[yr]["trough"], d["total_value"])

    return {
        "cagr": cagr,
        "max_dd": max_dd,
        "total_return": total_return,
        "years": years,
        "yearly": yearly,
    }


def run_sweep(df):
    """Sweep key parameters and find best config."""
    configs = []

    for peak_lb in [20, 50, 100]:
        for dip_thresh in [3, 5, 8, 10, 15]:
            for buy_frac in [0.3, 0.5, 0.7]:
                for min_days in [3, 5, 10, 20]:
                    for exit_mode in ["never", "recovery_ma"]:
                        for exit_ma in [10, 20, 50]:
                            if exit_mode != "recovery_ma" and exit_ma != 20:
                                continue  # skip irrelevant combos
                            configs.append(Config(
                                peak_lookback=peak_lb,
                                dip_threshold=dip_thresh,
                                buy_fraction=buy_frac,
                                min_days_between_buys=min_days,
                                exit_mode=exit_mode,
                                exit_ma_period=exit_ma,
                            ))

    print(f"\nSweeping {len(configs)} configs...")
    results = []

    for i, cfg in enumerate(configs):
        day_log, buys, sells = simulate(df, cfg)
        metrics = compute_metrics(day_log, cfg)
        if metrics:
            results.append({
                "cfg": cfg,
                "cagr": metrics["cagr"],
                "max_dd": metrics["max_dd"],
                "calmar": metrics["cagr"] / abs(metrics["max_dd"]) if metrics["max_dd"] != 0 else 0,
                "total_return": metrics["total_return"],
                "buys": buys,
                "sells": sells,
                "metrics": metrics,
            })

        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(configs)} done...")

    # Sort by Calmar
    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*120}")
    print(f"{'Rank':<5} {'Peak':>5} {'Dip%':>5} {'BuyF':>5} {'MinD':>5} {'Exit':>12} {'ExMA':>5} "
          f"{'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Growth':>8} {'Buys':>5} {'Sells':>5}")
    print(f"{'='*120}")

    for i, r in enumerate(results[:30]):
        c = r["cfg"]
        print(f"{i+1:<5} {c.peak_lookback:>5} {c.dip_threshold:>5.0f} {c.buy_fraction:>5.1f} "
              f"{c.min_days_between_buys:>5} {c.exit_mode:>12} {c.exit_ma_period:>5} "
              f"{r['cagr']:>7.1f}% {r['max_dd']:>7.1f}% {r['calmar']:>8.2f} "
              f"{r['total_return']:>7.1f}x {r['buys']:>5} {r['sells']:>5}")

    # Show year-wise for the best config
    best = results[0]
    print(f"\n{'='*80}")
    print(f"BEST CONFIG: peak={best['cfg'].peak_lookback}d, dip={best['cfg'].dip_threshold}%, "
          f"buy_frac={best['cfg'].buy_fraction}, min_days={best['cfg'].min_days_between_buys}, "
          f"exit={best['cfg'].exit_mode}, exit_ma={best['cfg'].exit_ma_period}d")
    print(f"{'='*80}")

    yearly = best["metrics"]["yearly"]
    print(f"\n{'Year':<6} {'Return':>10} {'Max DD':>10}")
    print("-" * 30)
    for yr in sorted(yearly.keys()):
        y = yearly[yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100
        print(f"{yr:<6} {ret:>+9.1f}% {dd:>9.1f}%")

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

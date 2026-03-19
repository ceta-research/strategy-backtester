#!/usr/bin/env python3
"""Buy-the-dip v3: Extended indicators + smart exits + regime filter.

New entry signals: MACD, volume spike, consecutive red days, Stochastic
New exit signals: RSI overbought, upper Bollinger Band, extended above MA
Regime filter: 200-day MA slope (only buy in uptrends)
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
    # --- ENTRY SCORING ---
    rsi2_threshold: float = 15.0      # RSI(2) < this = +2 pts
    rsi5_threshold: float = 30.0      # RSI(5) < this = +1 pt
    bb_period: int = 20               # Bollinger Band period
    bb_std: float = 2.0               # BB std devs
    ibs_threshold: float = 0.2        # IBS < this = +1 pt
    sma_period: int = 50              # Below SMA(N) = +1 pt
    drawdown_lookback: int = 50
    drawdown_threshold: float = 5.0   # DD > X% from peak = +1 pt
    stoch_period: int = 14            # Stochastic %K period
    stoch_threshold: float = 20.0     # Stoch %K < this = +1 pt
    consecutive_red: int = 3          # N consecutive red candles = +1 pt
    volume_spike_mult: float = 2.0    # Volume > Nx avg = +1 pt (capitulation)
    volume_avg_period: int = 20       # Volume average lookback
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9              # MACD < signal = +1 pt

    # --- REGIME FILTER ---
    use_regime_filter: bool = False    # Only buy when 200d MA is rising
    regime_ma_period: int = 200
    regime_slope_days: int = 20       # MA must be higher than N days ago

    # --- BUY TRIGGER ---
    min_score: int = 3
    buy_fraction: float = 0.5
    min_days_between_buys: int = 5
    min_cash_reserve: float = 0.05

    # --- EXIT ---
    exit_mode: str = "never"          # "never", "smart", "recovery_ma"
    exit_ma_period: int = 20          # For recovery_ma
    # Smart exit scoring
    exit_rsi2_threshold: float = 85.0   # RSI(2) > this = +2 pts
    exit_rsi5_threshold: float = 70.0   # RSI(5) > this = +1 pt
    exit_above_upper_bb: bool = True    # Close > upper BB = +2 pts
    exit_extended_pct: float = 10.0     # Close > X% above SMA(50) = +1 pt
    exit_min_score: int = 3             # Sell when exit score >= this
    exit_min_profit_pct: float = 5.0    # Only sell if in profit by > X%

    start_capital: float = 10_000_000


def compute_rsi(closes, period):
    rsi = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains, losses = [], []
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
            rsi[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return rsi


def compute_sma(values, period):
    sma = [0.0] * len(values)
    running = 0.0
    for i in range(len(values)):
        running += values[i]
        if i >= period:
            running -= values[i - period]
        sma[i] = running / min(i + 1, period)
    return sma


def compute_ema(values, period):
    ema = [0.0] * len(values)
    mult = 2.0 / (period + 1)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = values[i] * mult + ema[i - 1] * (1 - mult)
    return ema


def compute_bb(closes, period, num_std):
    middle = compute_sma(closes, period)
    upper = [0.0] * len(closes)
    lower = [0.0] * len(closes)
    for i in range(len(closes)):
        start = max(0, i - period + 1)
        window = closes[start:i + 1]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / max(len(window), 1)
        std = math.sqrt(variance)
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
    return upper, middle, lower


def compute_stochastic(highs, lows, closes, period):
    """Stochastic %K."""
    stoch = [50.0] * len(closes)
    for i in range(period - 1, len(closes)):
        h = max(highs[i - period + 1:i + 1])
        l = min(lows[i - period + 1:i + 1])
        if h - l > 0:
            stoch[i] = (closes[i] - l) / (h - l) * 100
        else:
            stoch[i] = 50.0
    return stoch


def compute_macd(closes, fast, slow, signal_period):
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = compute_ema(macd_line, signal_period)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def fetch_niftybees(start_epoch, end_epoch):
    cr = CetaResearch()
    # Fetch extra data for 200d MA warmup
    warmup_epoch = start_epoch - 300 * 86400
    sql = f"""SELECT date_epoch, open, high, low, close, volume
              FROM nse.nse_charting_day
              WHERE symbol = 'NIFTYBEES'
                AND date_epoch >= {warmup_epoch} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    print("Fetching NIFTYBEES data (with warmup)...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return pl.DataFrame(), 0
    df = pl.DataFrame(results).with_columns([
        pl.col("date_epoch").cast(pl.Int64),
        pl.col("open").cast(pl.Float64), pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64), pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
    ]).sort("date_epoch")
    # Find index where actual simulation starts
    sim_start_idx = df.filter(pl.col("date_epoch") >= start_epoch).select(pl.first("date_epoch")).item()
    epochs = df["date_epoch"].to_list()
    start_idx = next(i for i, e in enumerate(epochs) if e >= start_epoch)
    return df, start_idx


@dataclass
class Position:
    entry_epoch: int
    entry_price: float
    quantity: float
    cost: float


def simulate(df: pl.DataFrame, cfg: Config, sim_start_idx: int):
    closes = df["close"].to_list()
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    opens = df["open"].to_list()
    volumes = df["volume"].to_list()
    epochs = df["date_epoch"].to_list()
    n = len(closes)

    # Precompute all indicators on full data (including warmup)
    rsi2 = compute_rsi(closes, 2)
    rsi5 = compute_rsi(closes, 5)
    sma50 = compute_sma(closes, cfg.sma_period)
    sma200 = compute_sma(closes, cfg.regime_ma_period)
    bb_upper, bb_mid, bb_lower = compute_bb(closes, cfg.bb_period, cfg.bb_std)
    stoch = compute_stochastic(highs, lows, closes, cfg.stoch_period)
    macd_line, macd_signal, macd_hist = compute_macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    vol_avg = compute_sma(volumes, cfg.volume_avg_period)
    exit_ma = compute_sma(closes, cfg.exit_ma_period)

    cash = cfg.start_capital
    positions = []
    last_buy_idx = -999
    total_buys = 0
    total_sells = 0
    day_log = []

    for i in range(sim_start_idx, n):
        close = closes[i]
        high = highs[i]
        low = lows[i]
        open_p = opens[i]
        volume = volumes[i]
        epoch = epochs[i]

        # --- ENTRY COMPOSITE SCORE ---
        entry_score = 0

        # RSI signals
        if rsi2[i] < cfg.rsi2_threshold:
            entry_score += 2
        if rsi5[i] < cfg.rsi5_threshold:
            entry_score += 1

        # Bollinger Band
        if close < bb_lower[i]:
            entry_score += 2

        # IBS
        ibs = (close - low) / (high - low) if (high - low) > 0 else 0.5
        if ibs < cfg.ibs_threshold:
            entry_score += 1

        # Below SMA
        if close < sma50[i]:
            entry_score += 1

        # Drawdown from peak
        dd_start = max(0, i - cfg.drawdown_lookback + 1)
        rolling_peak = max(closes[dd_start:i + 1])
        drawdown_pct = (rolling_peak - close) / rolling_peak * 100 if rolling_peak > 0 else 0
        if drawdown_pct >= cfg.drawdown_threshold:
            entry_score += 1

        # Stochastic
        if stoch[i] < cfg.stoch_threshold:
            entry_score += 1

        # Consecutive red days
        red_count = 0
        for j in range(i, max(i - 10, sim_start_idx) - 1, -1):
            if closes[j] < opens[j]:
                red_count += 1
            else:
                break
        if red_count >= cfg.consecutive_red:
            entry_score += 1

        # Volume spike (capitulation)
        if vol_avg[i] > 0 and volume > cfg.volume_spike_mult * vol_avg[i]:
            entry_score += 1

        # MACD bearish (line below signal)
        if macd_line[i] < macd_signal[i]:
            entry_score += 1

        # --- REGIME FILTER ---
        regime_ok = True
        if cfg.use_regime_filter and i >= cfg.regime_slope_days:
            if sma200[i] <= sma200[i - cfg.regime_slope_days]:
                regime_ok = False

        # --- EXIT LOGIC ---
        if positions:
            should_exit = False

            if cfg.exit_mode == "smart":
                exit_score = 0
                if rsi2[i] > cfg.exit_rsi2_threshold:
                    exit_score += 2
                if rsi5[i] > cfg.exit_rsi5_threshold:
                    exit_score += 1
                if cfg.exit_above_upper_bb and close > bb_upper[i]:
                    exit_score += 2
                ext_pct = (close - sma50[i]) / sma50[i] * 100 if sma50[i] > 0 else 0
                if ext_pct > cfg.exit_extended_pct:
                    exit_score += 1
                # Stochastic overbought
                if stoch[i] > 80:
                    exit_score += 1
                # MACD bullish exhaustion (histogram declining from peak)
                if i >= 2 and macd_hist[i] < macd_hist[i-1] and macd_hist[i-1] > 0:
                    exit_score += 1

                avg_entry = sum(p.cost for p in positions) / sum(p.quantity for p in positions)
                profit_pct = (close - avg_entry) / avg_entry * 100

                if exit_score >= cfg.exit_min_score and profit_pct > cfg.exit_min_profit_pct:
                    should_exit = True

            elif cfg.exit_mode == "recovery_ma":
                avg_entry = sum(p.cost for p in positions) / sum(p.quantity for p in positions)
                if close > exit_ma[i] and close > avg_entry:
                    should_exit = True

            if should_exit:
                total_qty = sum(p.quantity for p in positions)
                cash += total_qty * close
                total_sells += len(positions)
                positions = []

        # --- ENTRY ---
        days_since_buy = i - last_buy_idx
        min_cash = cfg.start_capital * cfg.min_cash_reserve

        if (entry_score >= cfg.min_score
                and days_since_buy >= cfg.min_days_between_buys
                and cash > min_cash
                and regime_ok):
            buy_amount = (cash - min_cash) * cfg.buy_fraction
            if buy_amount > 0 and close > 0:
                qty = buy_amount / close
                positions.append(Position(epoch, close, qty, buy_amount))
                cash -= buy_amount
                total_buys += 1
                last_buy_idx = i

        # Portfolio value
        position_value = sum(p.quantity * close for p in positions)
        total_value = cash + position_value
        day_log.append({"epoch": epoch, "total_value": total_value})

    return day_log, total_buys, total_sells


def compute_metrics(day_log):
    if len(day_log) < 2:
        return None
    values = [d["total_value"] for d in day_log]
    start_val, end_val = values[0], values[-1]
    years = (day_log[-1]["epoch"] - day_log[0]["epoch"]) / (365.25 * 86400)
    if years <= 0 or end_val <= 0:
        return None
    total_return = end_val / start_val
    cagr = (total_return ** (1 / years) - 1) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    yearly = {}
    for d in day_log:
        yr = datetime.fromtimestamp(d["epoch"], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": d["total_value"], "last": d["total_value"],
                          "peak": d["total_value"], "trough": d["total_value"]}
        yearly[yr]["last"] = d["total_value"]
        yearly[yr]["peak"] = max(yearly[yr]["peak"], d["total_value"])
        yearly[yr]["trough"] = min(yearly[yr]["trough"], d["total_value"])

    return {"cagr": cagr, "max_dd": max_dd, "total_return": total_return, "yearly": yearly}


def run_sweep(df, sim_start_idx):
    configs = []

    for min_score in [3, 4, 5, 6]:
        for buy_frac in [0.3, 0.5, 0.7, 1.0]:
            for min_days in [1, 3, 5, 10]:
                for exit_mode in ["never", "smart", "recovery_ma"]:
                    for use_regime in [False, True]:
                        # Smart exit sub-configs
                        if exit_mode == "smart":
                            for exit_min_sc in [3, 4]:
                                for exit_min_prof in [3, 5, 10]:
                                    configs.append(Config(
                                        min_score=min_score, buy_fraction=buy_frac,
                                        min_days_between_buys=min_days, exit_mode=exit_mode,
                                        use_regime_filter=use_regime,
                                        exit_min_score=exit_min_sc, exit_min_profit_pct=exit_min_prof,
                                    ))
                        elif exit_mode == "recovery_ma":
                            for exit_ma in [10, 20, 50]:
                                configs.append(Config(
                                    min_score=min_score, buy_fraction=buy_frac,
                                    min_days_between_buys=min_days, exit_mode=exit_mode,
                                    use_regime_filter=use_regime, exit_ma_period=exit_ma,
                                ))
                        else:  # never
                            configs.append(Config(
                                min_score=min_score, buy_fraction=buy_frac,
                                min_days_between_buys=min_days, exit_mode=exit_mode,
                                use_regime_filter=use_regime,
                            ))

    print(f"\nSweeping {len(configs)} configs...")
    results = []

    for i, cfg in enumerate(configs):
        day_log, buys, sells = simulate(df, cfg, sim_start_idx)
        metrics = compute_metrics(day_log)
        if metrics and abs(metrics["max_dd"]) > 0:
            calmar = metrics["cagr"] / abs(metrics["max_dd"])
            results.append({
                "cfg": cfg, "cagr": metrics["cagr"], "max_dd": metrics["max_dd"],
                "calmar": calmar, "total_return": metrics["total_return"],
                "buys": buys, "sells": sells, "metrics": metrics,
            })
        if (i + 1) % 500 == 0:
            print(f"  {i+1}/{len(configs)} done...")

    results.sort(key=lambda r: r["calmar"], reverse=True)

    # Print top results grouped by exit mode
    for mode_label, mode_filter in [("NEVER SELL", "never"), ("SMART EXIT", "smart"), ("RECOVERY MA", "recovery_ma")]:
        filtered = [r for r in results if r["cfg"].exit_mode == mode_filter][:10]
        if not filtered:
            continue
        print(f"\n{'='*130}")
        print(f"TOP 10: {mode_label}")
        print(f"{'='*130}")
        print(f"{'#':<3} {'Sc':>3} {'BuyF':>5} {'MinD':>4} {'Rgm':>4} {'ExSc':>4} {'ExPr':>5} {'ExMA':>4} "
              f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Buy':>4} {'Sel':>4}")
        print("-" * 90)
        for j, r in enumerate(filtered):
            c = r["cfg"]
            rgm = "Y" if c.use_regime_filter else "N"
            exsc = str(c.exit_min_score) if c.exit_mode == "smart" else "-"
            expr = f"{c.exit_min_profit_pct:.0f}" if c.exit_mode == "smart" else "-"
            exma = str(c.exit_ma_period) if c.exit_mode == "recovery_ma" else "-"
            print(f"{j+1:<3} {c.min_score:>3} {c.buy_fraction:>5.1f} {c.min_days_between_buys:>4} "
                  f"{rgm:>4} {exsc:>4} {expr:>5} {exma:>4} "
                  f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
                  f"{r['total_return']:>5.1f}x {r['buys']:>4} {r['sells']:>4}")

    # Show year-wise for overall best + best smart exit
    for label, result_set in [("OVERALL BEST (by Calmar)", results[:1]),
                               ("BEST SMART EXIT", [r for r in results if r["cfg"].exit_mode == "smart"][:1]),
                               ("BEST WITH REGIME FILTER", [r for r in results if r["cfg"].use_regime_filter][:1])]:
        if not result_set:
            continue
        best = result_set[0]
        c = best["cfg"]
        print(f"\n{'='*80}")
        print(f"{label}: CAGR={best['cagr']:.1f}%, MaxDD={best['max_dd']:.1f}%, "
              f"Calmar={best['calmar']:.2f}, Growth={best['total_return']:.1f}x")
        print(f"  score>={c.min_score}, buy_frac={c.buy_fraction}, min_days={c.min_days_between_buys}, "
              f"exit={c.exit_mode}, regime={'ON' if c.use_regime_filter else 'OFF'}")
        if c.exit_mode == "smart":
            print(f"  exit_min_score={c.exit_min_score}, exit_min_profit={c.exit_min_profit_pct}%")

        yearly = best["metrics"]["yearly"]
        print(f"\n{'Year':<6} {'Return':>10} {'Max DD':>10} {'End Value':>14}")
        print("-" * 45)
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100
            print(f"{yr:<6} {ret:>+9.1f}% {dd:>9.1f}% {y['last']:>14,.0f}")

    return results


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    df, sim_start_idx = fetch_niftybees(start_epoch, end_epoch)
    if df.is_empty():
        print("No data")
        return
    print(f"  {df.height} total rows, sim starts at index {sim_start_idx}")

    run_sweep(df, sim_start_idx)


if __name__ == "__main__":
    main()

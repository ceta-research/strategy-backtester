#!/usr/bin/env python3
"""Buy-the-dip on multiple global indices. Runs 3 strategies per index:
1. Never sell (pure buy-the-dip)
2. Smart exit (sell on overbought signals)
3. Buy-and-hold benchmark
"""

import sys
import os
import math
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch


INDICES = [
    ("^GSPC", "S&P 500"),
    ("^NSEI", "NIFTY 50"),
    ("^BSESN", "Sensex"),
    ("^FTSE", "FTSE 100"),
    ("^N225", "Nikkei 225"),
    ("^HSI", "Hang Seng"),
    ("^GDAXI", "DAX"),
    ("^KS11", "KOSPI"),
    ("^TWII", "Taiwan"),
    ("^BVSP", "Bovespa"),
    ("^JKSE", "Jakarta"),
    ("^STI", "Singapore"),
    ("SPY", "SPY ETF"),
    ("QQQ", "QQQ ETF"),
]


def fetch_index(cr, symbol, start_epoch, end_epoch):
    warmup_epoch = start_epoch - 300 * 86400
    sql = f"""SELECT dateEpoch as date_epoch, open, high, low, adjClose as close, volume
              FROM fmp.stock_eod
              WHERE symbol = '{symbol}'
                AND dateEpoch >= {warmup_epoch} AND dateEpoch <= {end_epoch}
              ORDER BY dateEpoch"""
    import time
    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(5)
            else:
                return [], 0
    if not results:
        return [], 0

    data = []
    for r in results:
        data.append({
            "epoch": int(r["date_epoch"]),
            "open": float(r.get("open") or 0),
            "high": float(r.get("high") or 0),
            "low": float(r.get("low") or 0),
            "close": float(r.get("close") or 0),
            "volume": float(r.get("volume") or 0),
        })

    # Filter out zero-close rows
    data = [d for d in data if d["close"] > 0]
    data.sort(key=lambda x: x["epoch"])

    # Find sim start index
    start_idx = 0
    for i, d in enumerate(data):
        if d["epoch"] >= start_epoch:
            start_idx = i
            break

    return data, start_idx


def compute_rsi(closes, period):
    rsi = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsi
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        avg_gain += max(change, 0)
        avg_loss += max(-change, 0)
    avg_gain /= period
    avg_loss /= period
    for i in range(period, len(closes)):
        if i > period:
            change = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        rsi[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
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


def compute_bb_lower(closes, period, num_std):
    sma = compute_sma(closes, period)
    lower = [0.0] * len(closes)
    for i in range(len(closes)):
        start = max(0, i - period + 1)
        window = closes[start:i + 1]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / max(len(window), 1)
        lower[i] = sma[i] - num_std * math.sqrt(variance)
    return lower


def simulate_buy_the_dip(data, start_idx, exit_mode="never"):
    """Run buy-the-dip with composite scoring.

    exit_mode: "never" or "smart"
    """
    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    volumes = [d["volume"] for d in data]
    n = len(closes)

    rsi2 = compute_rsi(closes, 2)
    rsi5 = compute_rsi(closes, 5)
    sma50 = compute_sma(closes, 50)
    bb_lower = compute_bb_lower(closes, 20, 2.0)

    capital = 10_000_000
    cash = capital
    positions = []  # (price, qty, cost)
    last_buy_idx = -999
    total_buys = 0

    values = []

    for i in range(start_idx, n):
        close = closes[i]
        high = highs[i]
        low = lows[i]

        # --- EXIT (smart) ---
        if positions and exit_mode == "smart":
            exit_score = 0
            if rsi2[i] > 85:
                exit_score += 2
            if rsi5[i] > 70:
                exit_score += 1
            ext_pct = (close - sma50[i]) / sma50[i] * 100 if sma50[i] > 0 else 0
            if ext_pct > 10:
                exit_score += 1

            avg_entry = sum(p[2] for p in positions) / sum(p[1] for p in positions)
            profit_pct = (close - avg_entry) / avg_entry * 100

            if exit_score >= 3 and profit_pct > 10:
                total_qty = sum(p[1] for p in positions)
                cash += total_qty * close
                positions = []

        # --- ENTRY SCORE ---
        entry_score = 0
        if rsi2[i] < 15:
            entry_score += 2
        if rsi5[i] < 30:
            entry_score += 1
        if close < bb_lower[i]:
            entry_score += 2

        ibs = (close - low) / (high - low) if (high - low) > 0 else 0.5
        if ibs < 0.2:
            entry_score += 1
        if close < sma50[i]:
            entry_score += 1

        dd_start = max(0, i - 50 + 1)
        peak = max(closes[dd_start:i + 1])
        dd_pct = (peak - close) / peak * 100 if peak > 0 else 0
        if dd_pct >= 5:
            entry_score += 1

        days_since = i - last_buy_idx
        min_cash = capital * 0.05

        if entry_score >= 5 and days_since >= 3 and cash > min_cash:
            buy_amount = (cash - min_cash) * 0.7
            if buy_amount > 0 and close > 0:
                qty = buy_amount / close
                positions.append((close, qty, buy_amount))
                cash -= buy_amount
                total_buys += 1
                last_buy_idx = i

        pos_val = sum(p[1] * close for p in positions)
        values.append(cash + pos_val)

    return values, total_buys


def simulate_buy_and_hold(data, start_idx):
    """Simple buy-and-hold from day 1."""
    closes = [d["close"] for d in data]
    start_price = closes[start_idx]
    capital = 10_000_000
    qty = capital / start_price
    values = [qty * closes[i] for i in range(start_idx, len(closes))]
    return values


def compute_stats(values, data, start_idx):
    if len(values) < 2:
        return None

    start_val = values[0]
    end_val = values[-1]
    start_epoch = data[start_idx]["epoch"]
    end_epoch = data[start_idx + len(values) - 1]["epoch"]
    years = (end_epoch - start_epoch) / (365.25 * 86400)

    if years <= 0 or end_val <= 0 or start_val <= 0:
        return None

    total_return = end_val / start_val
    cagr = (total_return ** (1 / years) - 1) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    calmar = cagr / abs(max_dd) if max_dd != 0 else 0

    # Year-wise
    yearly = {}
    for j, v in enumerate(values):
        epoch = data[start_idx + j]["epoch"]
        yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v}
        yearly[yr]["last"] = v

    return {"cagr": cagr, "max_dd": max_dd, "calmar": calmar, "total_return": total_return,
            "years": years, "yearly": yearly}


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    # Summary table
    summary = []

    for symbol, name in INDICES:
        print(f"\n--- {name} ({symbol}) ---")
        data, start_idx = fetch_index(cr, symbol, start_epoch, end_epoch)
        if not data or start_idx >= len(data) - 100:
            print(f"  Insufficient data, skipping")
            continue

        n_rows = len(data) - start_idx
        print(f"  {n_rows} trading days")

        # Run 3 strategies
        vals_never, buys_never = simulate_buy_the_dip(data, start_idx, "never")
        vals_smart, buys_smart = simulate_buy_the_dip(data, start_idx, "smart")
        vals_bh = simulate_buy_and_hold(data, start_idx)

        stats_never = compute_stats(vals_never, data, start_idx)
        stats_smart = compute_stats(vals_smart, data, start_idx)
        stats_bh = compute_stats(vals_bh, data, start_idx)

        if stats_never and stats_smart and stats_bh:
            summary.append({
                "name": name, "symbol": symbol,
                "bh": stats_bh, "never": stats_never, "smart": stats_smart,
                "buys_never": buys_never, "buys_smart": buys_smart,
            })
            print(f"  Buy&Hold:   CAGR={stats_bh['cagr']:>6.1f}%  MaxDD={stats_bh['max_dd']:>6.1f}%  Calmar={stats_bh['calmar']:.2f}  Growth={stats_bh['total_return']:.1f}x")
            print(f"  Dip(never): CAGR={stats_never['cagr']:>6.1f}%  MaxDD={stats_never['max_dd']:>6.1f}%  Calmar={stats_never['calmar']:.2f}  Growth={stats_never['total_return']:.1f}x  Buys={buys_never}")
            print(f"  Dip(smart): CAGR={stats_smart['cagr']:>6.1f}%  MaxDD={stats_smart['max_dd']:>6.1f}%  Calmar={stats_smart['calmar']:.2f}  Growth={stats_smart['total_return']:.1f}x  Buys={buys_smart}")

    # Print summary comparison table
    print(f"\n{'='*140}")
    print(f"{'Index':<14} {'':>3} {'BH CAGR':>8} {'BH DD':>7} {'BH Calm':>8} "
          f"{'Dip CAGR':>9} {'Dip DD':>7} {'Dip Calm':>9} "
          f"{'Smrt CAGR':>10} {'Smrt DD':>8} {'Smrt Calm':>10} "
          f"{'Alpha':>7} {'Best':>8}")
    print(f"{'='*140}")

    for s in summary:
        bh = s["bh"]
        nv = s["never"]
        sm = s["smart"]
        alpha = nv["cagr"] - bh["cagr"]

        # Pick best by Calmar
        best = "B&H"
        best_calmar = bh["calmar"]
        if nv["calmar"] > best_calmar:
            best = "Dip"
            best_calmar = nv["calmar"]
        if sm["calmar"] > best_calmar:
            best = "Smart"

        print(f"{s['name']:<14} "
              f"{bh['cagr']:>+7.1f}% {bh['max_dd']:>6.1f}% {bh['calmar']:>7.2f}   "
              f"{nv['cagr']:>+8.1f}% {nv['max_dd']:>6.1f}% {nv['calmar']:>8.2f}   "
              f"{sm['cagr']:>+9.1f}% {sm['max_dd']:>7.1f}% {sm['calmar']:>9.2f}   "
              f"{alpha:>+6.1f}% {best:>7}")

    # Year-wise for top 3 indices
    print(f"\n{'='*100}")
    print("YEAR-WISE: Buy-the-Dip (never sell) vs Buy-and-Hold")
    print(f"{'='*100}")

    top_indices = sorted(summary, key=lambda s: s["never"]["cagr"] - s["bh"]["cagr"], reverse=True)[:5]

    years_all = set()
    for s in top_indices:
        years_all.update(s["never"]["yearly"].keys())
    years_sorted = sorted(years_all)

    header = f"{'Year':<6}"
    for s in top_indices:
        header += f"  {s['name'][:10]:>10} {'(alpha)':>8}"
    print(header)
    print("-" * (6 + 20 * len(top_indices)))

    for yr in years_sorted:
        row = f"{yr:<6}"
        for s in top_indices:
            y_nv = s["never"]["yearly"].get(yr)
            y_bh = s["bh"]["yearly"].get(yr)
            if y_nv and y_bh:
                ret_nv = (y_nv["last"] - y_nv["first"]) / y_nv["first"] * 100
                ret_bh = (y_bh["last"] - y_bh["first"]) / y_bh["first"] * 100
                alpha = ret_nv - ret_bh
                row += f"  {ret_nv:>+9.1f}% {alpha:>+7.1f}%"
            else:
                row += f"  {'—':>10} {'—':>8}"
        print(row)


if __name__ == "__main__":
    main()

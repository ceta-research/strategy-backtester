#!/usr/bin/env python3
"""Buy at next open when close is at 2-day high. Trailing SL exit.

Entry: close[i] >= max(close[i-1], close[i-2]) → buy at open[i+1]
Exit:  Position drops X% from peak → sell at next open (MOC-like)

This is realistic:
  - Signal: observe today's close, compare to last 2 days (known info)
  - Execute: submit order for tomorrow's open (pre-market order)
  - No look-ahead bias

Sweep TSL from 5% to 20%.
"""

import sys
import os
import math
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.metrics import compute_metrics as compute_full_metrics

CAPITAL = 10_000_000
SLIPPAGE = 0.0005


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_niftybees(cr, start_epoch, end_epoch):
    warmup = start_epoch - 100 * 86400
    sql = f"""SELECT date_epoch, open, high, low, close, volume
              FROM nse.nse_charting_day
              WHERE symbol = 'NIFTYBEES'
                AND date_epoch >= {warmup} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return [], 0
    data = []
    for r in results:
        c = float(r.get("close") or 0)
        o = float(r.get("open") or 0)
        if c > 0 and o > 0:
            data.append({
                "epoch": int(r["date_epoch"]),
                "open": o,
                "high": float(r.get("high") or c),
                "low": float(r.get("low") or c),
                "close": c,
                "volume": float(r.get("volume") or 0),
            })
    data.sort(key=lambda x: x["epoch"])
    start_idx = next((i for i, d in enumerate(data) if d["epoch"] >= start_epoch), 0)
    return data, start_idx


# ── Simulation ───────────────────────────────────────────────────────────────

def simulate(data, start_idx, *,
             trailing_sl=10,           # trailing SL %
             buy_fraction=0.95,        # fraction of cash to deploy
             lookback_days=2,          # close >= N-day high to trigger
             ):
    """
    Entry: close[i] >= max(close[i-1], ..., close[i-lookback]) → buy at open[i+1]
    Exit:  position value drops TSL% from peak → sell at next open
    """
    n = len(data)
    closes = [d["close"] for d in data]
    opens = [d["open"] for d in data]
    epochs = [d["epoch"] for d in data]

    cash = CAPITAL
    position = None  # (qty, entry_price, entry_idx)
    position_peak = 0.0
    total_buys = 0
    total_sells = 0
    total_charges = 0.0
    total_slippage = 0.0
    values = []

    # Pending signals
    pending_buy = False
    pending_sell = False

    for i in range(start_idx, n):
        close = closes[i]
        open_price = opens[i]

        # ── EXECUTE pending signals at today's open ──
        if pending_sell and position:
            qty, ep, ei = position
            sell_val = qty * open_price
            ch = calculate_charges("NSE", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
            sl = sell_val * SLIPPAGE
            cash += sell_val - ch - sl
            total_charges += ch
            total_slippage += sl
            total_sells += 1
            position = None
            position_peak = 0.0
            pending_sell = False

        if pending_buy and position is None:
            invest = cash * buy_fraction
            if invest > 0 and open_price > 0:
                qty = int(invest / open_price)
                if qty > 0:
                    cost = qty * open_price
                    ch = calculate_charges("NSE", cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                    sl = cost * SLIPPAGE
                    if cost + ch + sl <= cash:
                        position = (qty, open_price, i)
                        position_peak = qty * open_price
                        cash -= cost + ch + sl
                        total_charges += ch
                        total_slippage += sl
                        total_buys += 1
            pending_buy = False

        # ── UPDATE position peak (use close for MTM) ──
        if position:
            qty, ep, ei = position
            pos_val = qty * close
            position_peak = max(position_peak, pos_val)

        # ── CHECK EXIT: trailing SL ──
        if position and trailing_sl > 0 and not pending_sell:
            qty, ep, ei = position
            pos_val = qty * close
            dd_from_peak = (position_peak - pos_val) / position_peak * 100
            if dd_from_peak >= trailing_sl:
                pending_sell = True  # sell at tomorrow's open

        # ── CHECK ENTRY: close at N-day high ──
        if position is None and not pending_buy and not pending_sell:
            if i >= lookback_days:
                past_closes = closes[i - lookback_days:i]  # previous N days (not including today)
                if close >= max(past_closes):
                    pending_buy = True  # buy at tomorrow's open

        # Portfolio value
        if position:
            qty, ep, ei = position
            values.append(cash + qty * close)
        else:
            values.append(cash)

    return values, total_buys, total_sells, total_charges, total_slippage


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_stats(values, epochs_sub):
    if len(values) < 2:
        return None
    sv, ev = values[0], values[-1]
    yrs = (epochs_sub[-1] - epochs_sub[0]) / (365.25 * 86400)
    if yrs <= 0 or ev <= 0 or sv <= 0:
        return None
    tr = ev / sv
    cagr = (tr ** (1 / yrs) - 1) * 100
    peak = sv
    mdd = 0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    dr = [(values[i] - values[i-1]) / values[i-1] if values[i-1] > 0 else 0
          for i in range(1, len(values))]
    sharpe = sortino = None
    if len(dr) >= 20:
        full = compute_full_metrics(dr, [0.0]*len(dr), periods_per_year=252)
        p = full["portfolio"]
        sharpe = p.get("sharpe_ratio")
        sortino = p.get("sortino_ratio")
    yearly = {}
    for j, v in enumerate(values):
        yr = datetime.fromtimestamp(epochs_sub[j], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v, "peak": v, "trough": v}
        yearly[yr]["last"] = v
        yearly[yr]["peak"] = max(yearly[yr]["peak"], v)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], v)
    return {"cagr": cagr, "mdd": mdd, "calmar": calmar, "tr": tr, "yrs": yrs,
            "sharpe": sharpe, "sortino": sortino, "yearly": yearly}


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()
    print("Fetching NIFTYBEES (with open prices)...")
    data, start_idx = fetch_niftybees(cr, start_epoch, end_epoch)
    if not data:
        print("No data")
        return
    n_trading = len(data) - start_idx
    print(f"  {n_trading} trading days")

    epochs = [d["epoch"] for d in data]
    closes = [d["close"] for d in data]

    # Buy-and-hold baseline
    bh_vals = [closes[i] / closes[start_idx] * CAPITAL for i in range(start_idx, len(data))]
    bh_s = compute_stats(bh_vals, epochs[start_idx:])
    print(f"\n  Buy & Hold: CAGR={bh_s['cagr']:.1f}%, MDD={bh_s['mdd']:.1f}%, "
          f"Calmar={bh_s['calmar']:.2f}")

    # ── Sweep ──
    trailing_sls = [5, 7, 8, 10, 12, 15, 20, 0]  # 0 = no TSL (hold forever)
    lookback_days_list = [2, 3, 5]
    buy_fractions = [0.7, 0.95]

    print(f"\n{'='*120}")
    print(f"  ENTRY: close >= {lookback_days_list}-day high → buy at NEXT OPEN")
    print(f"  EXIT: trailing SL {trailing_sls}%")
    print(f"  Real NSE charges + 5bps slippage")
    print(f"{'='*120}")

    results = []
    for lb in lookback_days_list:
        for tsl in trailing_sls:
            for bf in buy_fractions:
                vals, buys, sells, ch, sl = simulate(
                    data, start_idx,
                    trailing_sl=tsl, buy_fraction=bf, lookback_days=lb)
                ep = epochs[start_idx:]
                s = compute_stats(vals, ep)
                if s:
                    results.append({
                        "lb": lb, "tsl": tsl, "bf": bf,
                        "buys": buys, "sells": sells,
                        "charges": ch, "slippage": sl, **s})

    # Sort by Calmar
    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n  ALL CONFIGS sorted by Calmar:")
    print(f"  {'#':<3} {'LB':>3} {'TSL%':>5} {'BuyF':>5} "
          f"{'CAGR':>7} {'MDD':>7} {'Cal':>5} {'Shrp':>5} {'Sort':>5} "
          f"{'Grwth':>6} {'Buy':>5} {'Sel':>5} {'Cost%':>6}")
    print(f"  {'-'*90}")

    for i, r in enumerate(results):
        cost_pct = (r["charges"] + r["slippage"]) / CAPITAL * 100
        tsl_str = f"{r['tsl']:>5}" if r['tsl'] > 0 else " none"
        print(f"  {i+1:<3} {r['lb']:>3} {tsl_str} {r['bf']:>5.2f} "
              f"{r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>5.02f} "
              f"{r.get('sharpe') or 0:>5.2f} {r.get('sortino') or 0:>5.02f} "
              f"{r['tr']:>5.1f}x {r['buys']:>5} {r['sells']:>5} {cost_pct:>5.1f}%")

    # Year-wise for best Calmar and best CAGR
    by_cagr = sorted(results, key=lambda r: r["cagr"], reverse=True)

    for label, best in [("BEST by Calmar", results[0] if results else None),
                         ("BEST by CAGR", by_cagr[0] if by_cagr else None)]:
        if not best:
            continue
        cost_pct = (best["charges"] + best["slippage"]) / CAPITAL * 100
        tsl_str = f"{best['tsl']}%" if best['tsl'] > 0 else "none"
        print(f"\n  {label}: lookback={best['lb']}d, TSL={tsl_str}, frac={best['bf']}")
        print(f"  CAGR={best['cagr']:.1f}%, MDD={best['mdd']:.1f}%, Calmar={best['calmar']:.2f}, "
              f"Sharpe={best.get('sharpe') or 0:.2f}, Sortino={best.get('sortino') or 0:.2f}")
        print(f"  {best['buys']} buys, {best['sells']} sells, costs={cost_pct:.1f}% of capital")
        print(f"\n  {'Year':<6} {'Return':>9} {'MaxDD':>9} {'EndValue':>14}")
        print(f"  {'-'*42}")
        for yr in sorted(best["yearly"].keys()):
            y = best["yearly"][yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}% {y['last']:>14,.0f}")


if __name__ == "__main__":
    main()

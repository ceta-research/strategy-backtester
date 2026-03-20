#!/usr/bin/env python3
"""Buy at next open when close is at N-day high. Trailing SL exit.

Entry: close[i] >= max(close[i-1], ..., close[i-lookback]) → buy at open[i+1]
Exit:  Position drops X% from peak → sell at next open (MOC-like)

This is realistic:
  - Signal: observe today's close, compare to last N days (known info)
  - Execute: submit order for tomorrow's open (pre-market order)
  - No look-ahead bias

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult

CAPITAL = 10_000_000
SLIPPAGE = 0.0005
STRATEGY_NAME = "buy_nday_high_tsl"
DESCRIPTION = ("Buy at next open when close >= N-day high. "
               "Exit when position drops X% from peak (trailing SL) at next open.")


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_ohlcv(cr, symbol, table, start_epoch, end_epoch):
    """Fetch OHLCV data with warmup period."""
    warmup = start_epoch - 100 * 86400
    sql = f"""SELECT date_epoch, open, high, low, close, volume
              FROM {table}
              WHERE symbol = '{symbol}'
                AND date_epoch >= {warmup} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
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

def simulate(data, start_idx, bm_epochs, bm_values, *,
             trailing_sl=10, buy_fraction=0.95, lookback_days=2,
             exchange="NSE"):
    """Run simulation and return a BacktestResult.

    Args:
        data: List of OHLCV dicts.
        start_idx: Index in data where the backtest period starts.
        bm_epochs: Benchmark epoch list (buy-and-hold).
        bm_values: Benchmark value list (buy-and-hold).
        trailing_sl: Trailing stop-loss percentage (0 = hold forever).
        buy_fraction: Fraction of cash to deploy per buy.
        lookback_days: Entry trigger — close >= N-day high.
        exchange: Exchange for charge calculation.
    """
    params = {"lookback_days": lookback_days, "trailing_sl_pct": trailing_sl,
              "buy_fraction": buy_fraction}
    symbol = data[0].get("symbol", "NIFTYBEES") if data else "NIFTYBEES"
    result = BacktestResult(
        STRATEGY_NAME, params, symbol, exchange, CAPITAL,
        slippage_bps=int(SLIPPAGE * 10000), description=DESCRIPTION,
    )

    n = len(data)
    closes = [d["close"] for d in data]
    opens = [d["open"] for d in data]
    epochs = [d["epoch"] for d in data]

    cash = CAPITAL
    position = None  # (qty, entry_price, entry_epoch_idx)
    position_peak = 0.0
    buy_charges = 0.0
    buy_slippage = 0.0

    pending_buy = False
    pending_sell = False

    for i in range(start_idx, n):
        close = closes[i]
        open_price = opens[i]

        # ── EXECUTE pending sell at today's open ──
        if pending_sell and position:
            qty, ep, ei = position
            sell_val = qty * open_price
            sell_ch = calculate_charges(exchange, sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
            sell_sl = sell_val * SLIPPAGE
            cash += sell_val - sell_ch - sell_sl
            result.add_trade(
                entry_epoch=epochs[ei], exit_epoch=epochs[i],
                entry_price=ep, exit_price=open_price,
                quantity=qty, side="LONG",
                charges=buy_charges + sell_ch,
                slippage=buy_slippage + sell_sl,
            )
            position = None
            position_peak = 0.0
            buy_charges = 0.0
            buy_slippage = 0.0
            pending_sell = False

        # ── EXECUTE pending buy at today's open ──
        if pending_buy and position is None:
            invest = cash * buy_fraction
            if invest > 0 and open_price > 0:
                qty = int(invest / open_price)
                if qty > 0:
                    cost = qty * open_price
                    ch = calculate_charges(exchange, cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                    sl = cost * SLIPPAGE
                    if cost + ch + sl <= cash:
                        position = (qty, open_price, i)
                        position_peak = qty * open_price
                        cash -= cost + ch + sl
                        buy_charges = ch
                        buy_slippage = sl
            pending_buy = False

        # ── UPDATE position peak ──
        if position:
            qty, ep, ei = position
            pos_val = qty * close
            position_peak = max(position_peak, pos_val)

        # ── CHECK EXIT: trailing SL ──
        if position and trailing_sl > 0 and not pending_sell:
            qty, ep, ei = position
            pos_val = qty * close
            dd_pct = (position_peak - pos_val) / position_peak * 100
            if dd_pct >= trailing_sl:
                pending_sell = True

        # ── CHECK ENTRY: close at N-day high ──
        if position is None and not pending_buy and not pending_sell:
            if i >= lookback_days:
                past_closes = closes[i - lookback_days:i]
                if close >= max(past_closes):
                    pending_buy = True

        # ── Record daily portfolio value ──
        if position:
            qty, ep, ei = position
            result.add_equity_point(epochs[i], cash + qty * close)
        else:
            result.add_equity_point(epochs[i], cash)

    result.set_benchmark_values(bm_epochs, bm_values)
    return result


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()
    print("Fetching NIFTYBEES...")
    data, start_idx = fetch_ohlcv(cr, "NIFTYBEES", "nse.nse_charting_day",
                                   start_epoch, end_epoch)
    if not data:
        print("No data")
        return
    print(f"  {len(data) - start_idx} trading days")

    epochs = [d["epoch"] for d in data]
    closes = [d["close"] for d in data]

    # Buy-and-hold benchmark
    bm_epochs = epochs[start_idx:]
    bm_values = [closes[i] / closes[start_idx] * CAPITAL
                 for i in range(start_idx, len(data))]

    # ── Sweep ──
    trailing_sls = [5, 7, 8, 10, 12, 15, 20, 0]
    lookback_days_list = [2, 3, 5]
    buy_fractions = [0.7, 0.95]

    sweep = SweepResult(STRATEGY_NAME, "NIFTYBEES", "NSE", CAPITAL,
                        slippage_bps=int(SLIPPAGE * 10000), description=DESCRIPTION)

    total = len(trailing_sls) * len(lookback_days_list) * len(buy_fractions)
    print(f"\n  Sweeping {total} configs...")

    for lb in lookback_days_list:
        for tsl in trailing_sls:
            for bf in buy_fractions:
                r = simulate(data, start_idx, bm_epochs, bm_values,
                             trailing_sl=tsl, buy_fraction=bf, lookback_days=lb)
                sweep.add_config(
                    {"lookback_days": lb, "trailing_sl_pct": tsl, "buy_fraction": bf},
                    r,
                )

    # ── Output ──
    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    # Also print detailed summary for best config
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        best_result.print_summary()


if __name__ == "__main__":
    main()

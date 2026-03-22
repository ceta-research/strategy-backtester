#!/usr/bin/env python3
"""Corrected dip-buy strategy on NIFTYBEES.

Entry: Price dropped X% from N-day rolling peak → buy at NEXT day's close (MOC)
Exit:  Position gained Y% from entry price → sell at NEXT day's close (MOC)

MOC execution: signal from day i → execute at day i+1 close.
Real NSE delivery charges + 5bps slippage.

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult

CAPITAL = 10_000_000
SLIPPAGE = 0.0005  # 5 bps
STRATEGY_NAME = "dip_buy_moc"
DESCRIPTION = ("Dip-buy on NIFTYBEES with multi-tier averaging, target profit exit, "
               "and trailing stop-loss. MOC execution (signal day i, execute day i+1 close).")


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_niftybees(cr, start_epoch, end_epoch):
    warmup = start_epoch - 300 * 86400
    sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
              WHERE symbol = 'NIFTYBEES'
                AND date_epoch >= {warmup} AND date_epoch <= {end_epoch}
              ORDER BY date_epoch"""
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True, memory_mb=16384, threads=6)
    if not results:
        return [], 0
    data = []
    for r in results:
        c = float(r.get("close") or 0)
        if c > 0:
            data.append({"epoch": int(r["date_epoch"]), "close": c})
    data.sort(key=lambda x: x["epoch"])
    start_idx = next((i for i, d in enumerate(data) if d["epoch"] >= start_epoch), 0)
    return data, start_idx


# ── Simulation ───────────────────────────────────────────────────────────────

def simulate(data, start_idx, *, bm_epochs=None, bm_values=None,
             dip_threshold,          # buy when price drops X% from peak
             peak_lookback=50,       # N-day rolling peak window
             target_profit,          # sell when position gains Y% (0 = no TP)
             trailing_sl=0,          # trailing stop-loss % (0 = no TSL)
             buy_fraction=0.5,       # fraction of available cash to deploy
             min_days_between=5,     # minimum days between buys
             n_tiers=1,              # 1=single buy, 2-3=average down at 2x/3x threshold
             ):
    """Run dip-buy with target-profit exit and trailing stop-loss.

    MOC model: signal computed from day i's close, execution at day i+1's close.

    Trailing SL: tracks the highest portfolio value since entry. If portfolio
    drops X% from that peak, sell everything.

    Returns a BacktestResult.
    """
    params = {
        "dip_threshold": dip_threshold,
        "target_profit": target_profit,
        "trailing_sl": trailing_sl,
        "buy_fraction": buy_fraction,
        "peak_lookback": peak_lookback,
        "n_tiers": n_tiers,
    }
    result = BacktestResult(
        STRATEGY_NAME, params, "NIFTYBEES", "NSE", CAPITAL,
        slippage_bps=int(SLIPPAGE * 10000), description=DESCRIPTION,
    )

    closes = [d["close"] for d in data]
    epochs = [d["epoch"] for d in data]
    n = len(closes)

    cash = CAPITAL
    positions = []  # list of (entry_price, qty, cost)
    last_buy_idx = -999

    # Trade tracking for BacktestResult
    first_buy_epoch_idx = None
    accum_buy_charges = 0.0
    accum_buy_slippage = 0.0

    # Trailing SL state
    position_peak_value = 0.0  # highest position value since entry

    # Pending signals (for MOC)
    pending_buy = False
    pending_buy_tier = 0
    pending_sell = False

    for i in range(start_idx, n):
        close = closes[i]

        # ── EXECUTE pending signals from yesterday (MOC) ──
        if pending_sell and positions:
            total_qty = sum(p[1] for p in positions)
            sell_val = total_qty * close
            ch = calculate_charges("NSE", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
            sl = sell_val * SLIPPAGE
            cash += sell_val - ch - sl
            # Record trade: weighted average entry across all tiers
            avg_entry = sum(p[2] for p in positions) / total_qty
            result.add_trade(
                entry_epoch=epochs[first_buy_epoch_idx],
                exit_epoch=epochs[i],
                entry_price=avg_entry,
                exit_price=close,
                quantity=total_qty,
                side="LONG",
                charges=accum_buy_charges + ch,
                slippage=accum_buy_slippage + sl,
            )

            positions = []
            position_peak_value = 0.0
            first_buy_epoch_idx = None
            accum_buy_charges = 0.0
            accum_buy_slippage = 0.0
            pending_sell = False

        if pending_buy:
            min_cash = CAPITAL * 0.05
            if cash > min_cash:
                tier_mult = min(pending_buy_tier, n_tiers)
                frac = buy_fraction * tier_mult
                frac = min(frac, 1.0)
                buy_amount = (cash - min_cash) * frac
                if buy_amount > 0 and close > 0:
                    qty = int(buy_amount / close)
                    if qty > 0:
                        cost = qty * close
                        ch = calculate_charges("NSE", cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                        sl = cost * SLIPPAGE
                        if cost + ch + sl <= cash:
                            positions.append((close, qty, cost))
                            cash -= cost + ch + sl
                            last_buy_idx = i

                            # Track for BacktestResult
                            if first_buy_epoch_idx is None:
                                first_buy_epoch_idx = i
                            accum_buy_charges += ch
                            accum_buy_slippage += sl
            pending_buy = False

        # ── UPDATE TRAILING SL PEAK ──
        if positions:
            pos_val = sum(p[1] * close for p in positions)
            position_peak_value = max(position_peak_value, pos_val)

        # ── CHECK EXIT: target profit OR trailing SL ──
        if positions and not pending_sell:
            should_sell = False

            # Target profit
            if target_profit > 0:
                avg_entry = sum(p[2] for p in positions) / sum(p[1] for p in positions)
                profit_pct = (close - avg_entry) / avg_entry * 100
                if profit_pct >= target_profit:
                    should_sell = True

            # Trailing stop-loss
            if trailing_sl > 0 and position_peak_value > 0:
                pos_val = sum(p[1] * close for p in positions)
                drawdown_from_peak = (position_peak_value - pos_val) / position_peak_value * 100
                if drawdown_from_peak >= trailing_sl:
                    should_sell = True

            if should_sell:
                pending_sell = True

        # ── CHECK ENTRY: dip from peak? ──
        peak_start = max(start_idx, i - peak_lookback + 1)
        rolling_peak = max(closes[peak_start:i + 1])
        dip_pct = (rolling_peak - close) / rolling_peak * 100 if rolling_peak > 0 else 0

        days_since = i - last_buy_idx
        min_cash = CAPITAL * 0.05

        # Determine tier
        tier = 0
        if dip_pct >= dip_threshold:
            tier = 1
        if n_tiers >= 2 and dip_pct >= dip_threshold * 1.5:
            tier = 2
        if n_tiers >= 3 and dip_pct >= dip_threshold * 2.0:
            tier = 3

        if tier > 0 and days_since >= min_days_between and cash > min_cash:
            pending_buy = True
            pending_buy_tier = tier

        # Portfolio value
        pos_val = sum(p[1] * close for p in positions)
        result.add_equity_point(epochs[i], cash + pos_val)

    if bm_epochs and bm_values:
        result.set_benchmark_values(bm_epochs, bm_values)

    return result


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()
    print("Fetching NIFTYBEES...")
    data, start_idx = fetch_niftybees(cr, start_epoch, end_epoch)
    if not data:
        print("No data")
        return
    n_trading = len(data) - start_idx
    print(f"  {n_trading} trading days, start_idx={start_idx}")

    epochs = [d["epoch"] for d in data]
    closes = [d["close"] for d in data]

    # ══════════════════════════════════════════════════════════════════════════
    #  Buy-and-hold benchmark
    # ══════════════════════════════════════════════════════════════════════════
    bm_epochs = epochs[start_idx:]
    bm_values = [closes[i] / closes[start_idx] * CAPITAL
                 for i in range(start_idx, len(data))]

    bh_result = BacktestResult("buy_hold", {}, "NIFTYBEES", "NSE", CAPITAL)
    for j, i in enumerate(range(start_idx, len(data))):
        bh_result.add_equity_point(epochs[i], bm_values[j])
    bh_result.compute()
    bh_s = bh_result.to_dict()["summary"]
    print(f"\n  Buy & Hold NIFTYBEES: CAGR={bh_s['cagr']*100:.1f}%, "
          f"MDD={bh_s['max_drawdown']*100:.1f}%, "
          f"Calmar={bh_s['calmar_ratio']:.2f}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Sweep: dip_threshold × target_profit × trailing_sl × buy_fraction × tiers
    # ══════════════════════════════════════════════════════════════════════════
    dip_thresholds = [5, 7, 10, 14]
    target_profits = [0, 16]              # 0 = never sell
    trailing_sls = [0, 5, 10, 15, 20]    # 0 = no TSL
    buy_fractions = [0.5, 0.7]
    peak_lookbacks = [50]
    tier_options = [1, 3]

    print("\n" + "=" * 140)
    print("  MOC EXECUTION (signal yesterday -> buy today's close)")
    print("  Real NSE charges (STT 0.1% + brokerage + GST) + 5bps slippage")
    print("=" * 140)

    sweep = SweepResult(STRATEGY_NAME, "NIFTYBEES", "NSE", CAPITAL,
                        slippage_bps=int(SLIPPAGE * 10000), description=DESCRIPTION)

    total_configs = (len(dip_thresholds) * len(target_profits) * len(trailing_sls)
                     * len(buy_fractions) * len(peak_lookbacks) * len(tier_options))
    print(f"  Sweeping {total_configs} configs...")

    for dip in dip_thresholds:
        for tp in target_profits:
            for tsl in trailing_sls:
                for bf in buy_fractions:
                    for plb in peak_lookbacks:
                        for tiers in tier_options:
                            r = simulate(
                                data, start_idx,
                                bm_epochs=bm_epochs, bm_values=bm_values,
                                dip_threshold=dip, target_profit=tp,
                                trailing_sl=tsl,
                                buy_fraction=bf, peak_lookback=plb,
                                n_tiers=tiers)
                            params = {
                                "dip_threshold": dip, "target_profit": tp,
                                "trailing_sl": tsl, "buy_fraction": bf,
                                "peak_lookback": plb, "n_tiers": tiers,
                            }
                            sweep.add_config(params, r)

    # ── Leaderboard ──
    sweep.print_leaderboard(top_n=30, sort_by="calmar_ratio")

    # ── Save ──
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    # ── Best configs: detailed summary ──
    if sweep.configs:
        sorted_by_calmar = sweep._sorted("calmar_ratio")
        sorted_by_cagr = sweep._sorted("cagr")

        print("\n  BEST by Calmar:")
        _, best_calmar = sorted_by_calmar[0]
        best_calmar.print_summary()

        # Only print CAGR best if it's a different config
        _, best_cagr = sorted_by_cagr[0]
        if best_cagr is not best_calmar:
            print("\n  BEST by CAGR:")
            best_cagr.print_summary()


if __name__ == "__main__":
    main()

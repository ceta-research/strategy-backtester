#!/usr/bin/env python3
"""Corrected dip-buy strategy on NIFTYBEES.

Entry: Price dropped X% from N-day rolling peak → buy at NEXT day's close (MOC)
Exit:  Position gained Y% from entry price → sell at NEXT day's close (MOC)

MOC execution: signal from day i → execute at day i+1 close.
Real NSE delivery charges + 5bps slippage.
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
SLIPPAGE = 0.0005  # 5 bps


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

def simulate(data, start_idx, *,
             dip_threshold,          # buy when price drops X% from peak
             peak_lookback=50,       # N-day rolling peak window
             target_profit,          # sell when position gains Y% (0 = no TP)
             trailing_sl=0,          # trailing stop-loss % (0 = no TSL)
             buy_fraction=0.5,       # fraction of available cash to deploy
             min_days_between=5,     # minimum days between buys
             n_tiers=1,              # 1=single buy, 2-3=average down at 2x/3x threshold
             execution="moc",        # "moc" (next-day) or "same_bar" (for comparison)
             ):
    """Run dip-buy with target-profit exit and trailing stop-loss.

    MOC model: signal computed from day i's close, execution at day i+1's close.

    Trailing SL: tracks the highest portfolio value since entry. If portfolio
    drops X% from that peak, sell everything.
    """
    closes = [d["close"] for d in data]
    epochs = [d["epoch"] for d in data]
    n = len(closes)

    cash = CAPITAL
    positions = []  # list of (entry_price, qty, cost)
    last_buy_idx = -999
    total_buys = 0
    total_sells = 0
    total_charges = 0.0
    total_slippage = 0.0
    values = []

    # Trailing SL state
    position_peak_value = 0.0  # highest position value since entry

    # Pending signals (for MOC)
    pending_buy = False
    pending_buy_tier = 0
    pending_sell = False

    for i in range(start_idx, n):
        close = closes[i]

        # ── EXECUTE pending signals from yesterday (MOC) ──
        if execution == "moc":
            if pending_sell and positions:
                total_qty = sum(p[1] for p in positions)
                sell_val = total_qty * close
                ch = calculate_charges("NSE", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
                sl = sell_val * SLIPPAGE
                cash += sell_val - ch - sl
                total_charges += ch
                total_slippage += sl
                total_sells += len(positions)
                positions = []
                position_peak_value = 0.0
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
                                total_charges += ch
                                total_slippage += sl
                                total_buys += 1
                                last_buy_idx = i
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
                if execution == "moc":
                    pending_sell = True
                else:
                    total_qty = sum(p[1] for p in positions)
                    sell_val = total_qty * close
                    ch = calculate_charges("NSE", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
                    slp = sell_val * SLIPPAGE if execution != "same_bar_no_slip" else 0
                    cash += sell_val - ch - slp
                    total_charges += ch
                    total_slippage += slp
                    total_sells += len(positions)
                    positions = []
                    position_peak_value = 0.0

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
            if execution == "moc":
                pending_buy = True
                pending_buy_tier = tier
            else:
                # Same-bar entry
                frac = buy_fraction * min(tier, n_tiers)
                frac = min(frac, 1.0)
                buy_amount = (cash - min_cash) * frac
                if buy_amount > 0 and close > 0:
                    qty = int(buy_amount / close) if execution != "same_bar_no_slip" else buy_amount / close
                    if execution == "same_bar_no_slip":
                        # Original fractional, no charges
                        positions.append((close, qty, buy_amount))
                        cash -= buy_amount
                        total_buys += 1
                        last_buy_idx = i
                    elif qty > 0:
                        cost = qty * close
                        ch = calculate_charges("NSE", cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                        sl = cost * SLIPPAGE
                        if cost + ch + sl <= cash:
                            positions.append((close, qty, cost))
                            cash -= cost + ch + sl
                            total_charges += ch
                            total_slippage += sl
                            total_buys += 1
                            last_buy_idx = i

        # Portfolio value
        pos_val = sum(p[1] * close for p in positions)
        values.append(cash + pos_val)

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
    print("Fetching NIFTYBEES...")
    data, start_idx = fetch_niftybees(cr, start_epoch, end_epoch)
    if not data:
        print("No data")
        return
    n_trading = len(data) - start_idx
    print(f"  {n_trading} trading days, start_idx={start_idx}")

    epochs = [d["epoch"] for d in data]

    # ══════════════════════════════════════════════════════════════════════════
    #  Buy-and-hold baseline
    # ══════════════════════════════════════════════════════════════════════════
    closes = [d["close"] for d in data]
    bh_vals = [closes[i] / closes[start_idx] * CAPITAL for i in range(start_idx, len(data))]
    bh_ep = epochs[start_idx:]
    bh_s = compute_stats(bh_vals, bh_ep)
    print(f"\n  Buy & Hold NIFTYBEES: CAGR={bh_s['cagr']:.1f}%, MDD={bh_s['mdd']:.1f}%, "
          f"Calmar={bh_s['calmar']:.2f}")

    # ══════════════════════════════════════════════════════════════════════════
    #  Sweep: dip_threshold × target_profit × buy_fraction × peak_lookback
    # ══════════════════════════════════════════════════════════════════════════
    dip_thresholds = [5, 7, 10, 14]
    target_profits = [0, 16]              # 0 = never sell
    trailing_sls = [0, 5, 10, 15, 20]    # 0 = no TSL
    buy_fractions = [0.5, 0.7]
    peak_lookbacks = [50]
    tier_options = [1, 3]

    # ── Part 1: MOC execution (realistic) ──
    print("\n" + "=" * 140)
    print("  PART 1: MOC EXECUTION (signal yesterday → buy today's close)")
    print("  Real NSE charges (STT 0.1% + brokerage + GST) + 5bps slippage")
    print("=" * 140)

    results_moc = []
    total_configs = len(dip_thresholds) * len(target_profits) * len(trailing_sls) * len(buy_fractions) * len(peak_lookbacks) * len(tier_options)
    print(f"  Sweeping {total_configs} configs...")

    for dip in dip_thresholds:
        for tp in target_profits:
            for tsl in trailing_sls:
                for bf in buy_fractions:
                    for plb in peak_lookbacks:
                        for tiers in tier_options:
                            vals, buys, sells, ch, sl = simulate(
                                data, start_idx,
                                dip_threshold=dip, target_profit=tp,
                                trailing_sl=tsl,
                                buy_fraction=bf, peak_lookback=plb,
                                n_tiers=tiers, execution="moc")
                            ep = epochs[start_idx:]
                            s = compute_stats(vals, ep)
                            if s:
                                results_moc.append({
                                    "dip": dip, "tp": tp, "tsl": tsl,
                                    "bf": bf, "plb": plb,
                                    "tiers": tiers, "buys": buys, "sells": sells,
                                    "charges": ch, "slippage": sl, **s})

    results_moc.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n  TOP 30 by Calmar:")
    print(f"  {'#':<3} {'Dip%':>5} {'TP%':>4} {'TSL%':>5} {'BuyF':>5} {'Tier':>4} "
          f"{'CAGR':>7} {'MDD':>7} {'Cal':>5} {'Shrp':>5} {'Sort':>5} "
          f"{'Grwth':>6} {'Buy':>4} {'Sel':>4} {'Cost%':>6}")
    print(f"  {'-'*105}")

    for i, r in enumerate(results_moc[:30]):
        cost_pct = (r["charges"] + r["slippage"]) / CAPITAL * 100
        print(f"  {i+1:<3} {r['dip']:>5} {r['tp']:>4} {r['tsl']:>5} {r['bf']:>5.1f} {r['tiers']:>4} "
              f"{r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>5.02f} "
              f"{r.get('sharpe') or 0:>5.2f} {r.get('sortino') or 0:>5.02f} "
              f"{r['tr']:>5.1f}x {r['buys']:>4} {r['sells']:>4} {cost_pct:>5.1f}%")

    # Top by CAGR
    by_cagr = sorted(results_moc, key=lambda r: r["cagr"], reverse=True)
    print(f"\n  TOP 10 by CAGR:")
    print(f"  {'#':<3} {'Dip%':>5} {'TP%':>4} {'TSL%':>5} {'BuyF':>5} {'Tier':>4} "
          f"{'CAGR':>7} {'MDD':>7} {'Cal':>5} {'Buy':>4} {'Sel':>4}")
    print(f"  {'-'*75}")
    for i, r in enumerate(by_cagr[:10]):
        print(f"  {i+1:<3} {r['dip']:>5} {r['tp']:>4} {r['tsl']:>5} {r['bf']:>5.1f} {r['tiers']:>4} "
              f"{r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>5.02f} "
              f"{r['buys']:>4} {r['sells']:>4}")

    # Year-wise for best Calmar
    for label, result_list in [("BEST by Calmar", results_moc[:1]),
                                ("BEST by CAGR", by_cagr[:1])]:
        if not result_list:
            continue
        best = result_list[0]
        cost_pct = (best["charges"] + best["slippage"]) / CAPITAL * 100
        print(f"\n  {label}: dip={best['dip']}%, tp={best['tp']}%, tsl={best['tsl']}%, "
              f"frac={best['bf']}, tiers={best['tiers']}")
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

    return  # Skip biased comparison (already validated)

    # ── Part 2: Same-bar with NO charges/slippage (shows bias impact) ──
    print("\n\n" + "=" * 140)
    print("  PART 2: SAME-BAR, NO CHARGES (shows bias impact — NOT realistic)")
    print("=" * 140)

    results_biased = []
    for dip in dip_thresholds:
        for tp in target_profits:
            for bf in [0.5]:
                for plb in [50]:
                    vals, buys, sells, ch, sl = simulate(
                        data, start_idx,
                        dip_threshold=dip, target_profit=tp,
                        buy_fraction=bf, peak_lookback=plb,
                        n_tiers=1, execution="same_bar_no_slip")
                    ep = epochs[start_idx:]
                    s = compute_stats(vals, ep)
                    if s:
                        results_biased.append({
                            "dip": dip, "tp": tp, "buys": buys, "sells": sells, **s})

    results_biased.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n  {'#':<3} {'Dip%':>5} {'TP%':>4} {'CAGR':>7} {'MDD':>7} {'Cal':>5} "
          f"{'Buy':>4} {'Sel':>4}")
    print(f"  {'-'*50}")
    for i, r in enumerate(results_biased[:20]):
        print(f"  {i+1:<3} {r['dip']:>5} {r['tp']:>4} "
              f"{r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>5.2f} "
              f"{r['buys']:>4} {r['sells']:>4}")

    # ── Part 3: Comparison table ──
    print("\n\n" + "=" * 100)
    print("  PART 3: BIAS IMPACT — Same config, different execution")
    print("=" * 100)

    print(f"\n  {'Dip%':>5} {'TP%':>4} | {'Biased CAGR':>12} {'Biased MDD':>11} | "
          f"{'MOC CAGR':>10} {'MOC MDD':>9} | {'CAGR Lost':>10}")
    print(f"  {'-'*80}")

    for dip in dip_thresholds:
        for tp in target_profits:
            biased = next((r for r in results_biased
                          if r["dip"] == dip and r["tp"] == tp), None)
            moc = next((r for r in results_moc
                       if r["dip"] == dip and r["tp"] == tp
                       and r["bf"] == 0.5 and r["plb"] == 50 and r["tiers"] == 1), None)
            if biased and moc:
                lost = biased["cagr"] - moc["cagr"]
                print(f"  {dip:>5} {tp:>4} | {biased['cagr']:>+10.1f}% {biased['mdd']:>9.1f}% | "
                      f"{moc['cagr']:>+8.1f}% {moc['mdd']:>7.1f}% | {lost:>+8.1f}pp")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""MOC (Market-on-Close) execution model.

Signal computed from PRIOR close (day i-1). Execute at CURRENT close (day i).
This is realistic: you observe yesterday's close overnight, decide in the morning,
submit a MOC order, and get today's close.

This is between same-bar (unrealistic) and next-day (too conservative):
  - Same-bar:  signal(close_i) → execute at close_i    (look-ahead)
  - MOC:       signal(close_{i-1}) → execute at close_i  (REALISTIC)
  - Next-day:  signal(close_i) → execute at close_{i+1}  (too conservative)

The z-score uses ratios up to day i-1. On day i, we already know yesterday's
ratio, compute the z-score overnight, and submit a MOC order for today.
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


# ── Fixed config ─────────────────────────────────────────────────────────────

Z_LOOKBACK = 20
Z_ENTRY = 1.0
Z_EXIT = -0.5
SLIPPAGE_PCT = 0.0005  # 5 bps
CAPITAL = 10_000_000


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_close(cr, symbol, source, start_epoch, end_epoch):
    warmup = start_epoch - 500 * 86400
    if source == "nse":
        sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}' AND date_epoch >= {warmup}
                    AND date_epoch <= {end_epoch} ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
                  WHERE symbol = '{symbol}' AND dateEpoch >= {warmup}
                    AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""
    for attempt in range(3):
        try:
            return {int(r["date_epoch"]): float(r["close"])
                    for r in cr.query(sql, timeout=180, limit=10000000,
                                      memory_mb=8192, threads=4)
                    if float(r.get("close") or 0) > 0}
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                return {}


def align(datasets, start_epoch):
    common = sorted(set.intersection(*[set(d.keys()) for d in datasets]))
    return [e for e in common if e >= start_epoch]


# ── Indicators ───────────────────────────────────────────────────────────────

def compute_z(values, lookback):
    z = [0.0] * len(values)
    for i in range(lookback, len(values)):
        w = values[i - lookback:i]
        m = sum(w) / len(w)
        v = sum((x - m) ** 2 for x in w) / len(w)
        s = math.sqrt(v) if v > 0 else 1e-9
        z[i] = (values[i] - m) / s
    return z


# ── Simulation ───────────────────────────────────────────────────────────────

def sim_moc(epochs, closes_a, closes_b, z_lookback=Z_LOOKBACK,
            z_entry=Z_ENTRY, z_exit=Z_EXIT, capital=CAPITAL,
            slippage=SLIPPAGE_PCT):
    """MOC execution: signal from day i-1 ratio, execute at day i close.

    Z-score on bar i uses ratio[i-lookback:i] (past only).
    But we use z[i-1] (yesterday's signal) to decide today's trade.
    Execution at closes_a[i] / closes_b[i] (today's close via MOC order).
    """
    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    trades = 0
    total_charges = 0.0
    total_slippage = 0.0
    values = []

    for i in range(z_lookback + 1, n):
        # Signal from YESTERDAY's z-score (known before today's open)
        z_signal = z_scores[i - 1]

        # ── EXIT: check yesterday's signal, execute at today's close ──
        if position:
            side, qty, ep, ei = position
            should_exit = False
            if side == "a" and z_signal >= z_exit:
                should_exit = True
            elif side == "b" and z_signal <= -z_exit:
                should_exit = True

            if should_exit:
                exit_price = closes_a[i] if side == "a" else closes_b[i]
                sell_val = qty * exit_price
                ch = calculate_charges("US", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
                slip = sell_val * slippage
                cash += sell_val - ch - slip
                total_charges += ch
                total_slippage += slip
                position = None

        # ── ENTRY: check yesterday's signal, execute at today's close ──
        if position is None:
            buy_side = None
            if z_signal < -z_entry:
                buy_side = "a"
            elif z_signal > z_entry:
                buy_side = "b"

            if buy_side:
                buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
                invest = cash * 0.95
                if buy_price > 0 and invest > 0:
                    qty = int(invest / buy_price)
                    if qty > 0:
                        cost = qty * buy_price
                        ch = calculate_charges("US", cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                        slip = cost * slippage
                        if cost + ch + slip <= cash:
                            position = (buy_side, qty, buy_price, i)
                            cash -= cost + ch + slip
                            total_charges += ch
                            total_slippage += slip
                            trades += 1

        # Portfolio value at today's close
        if position:
            side, qty, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * cp)
        else:
            values.append(cash)

    return values, trades, total_charges, total_slippage


def sim_nextday(epochs, closes_a, closes_b, z_lookback=Z_LOOKBACK,
                z_entry=Z_ENTRY, z_exit=Z_EXIT, capital=CAPITAL,
                slippage=SLIPPAGE_PCT):
    """Next-day execution: signal from day i, execute at day i+1 close.
    (Same as alpha_corrected.py for comparison.)
    """
    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None
    trades = 0
    total_charges = 0.0
    total_slippage = 0.0
    values = []
    pending_entry = None
    pending_exit = False

    for i in range(z_lookback, n):
        z = z_scores[i]

        # Execute pending from yesterday
        if pending_exit and position:
            side, qty, ep, ei = position
            exit_price = closes_a[i] if side == "a" else closes_b[i]
            sell_val = qty * exit_price
            ch = calculate_charges("US", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
            slip = sell_val * slippage
            cash += sell_val - ch - slip
            total_charges += ch
            total_slippage += slip
            position = None
            pending_exit = False

        if pending_entry and position is None:
            buy_side = pending_entry
            buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
            invest = cash * 0.95
            if buy_price > 0 and invest > 0:
                qty = int(invest / buy_price)
                if qty > 0:
                    cost = qty * buy_price
                    ch = calculate_charges("US", cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                    slip = cost * slippage
                    if cost + ch + slip <= cash:
                        position = (buy_side, qty, buy_price, i)
                        cash -= cost + ch + slip
                        total_charges += ch
                        total_slippage += slip
                        trades += 1
            pending_entry = None

        # Generate signals for tomorrow
        if position:
            side, qty, ep, ei = position
            if side == "a" and z >= z_exit:
                pending_exit = True
            elif side == "b" and z <= -z_exit:
                pending_exit = True

        if position is None and not pending_exit:
            if z < -z_entry:
                pending_entry = "a"
            elif z > z_entry:
                pending_entry = "b"

        if position:
            side, qty, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * cp)
        else:
            values.append(cash)

    return values, trades, total_charges, total_slippage


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


def fmt(s):
    if not s:
        return "NO DATA"
    sh = f", Sharpe={s['sharpe']:.2f}" if s.get('sharpe') else ""
    so = f", Sortino={s['sortino']:.2f}" if s.get('sortino') else ""
    return f"CAGR={s['cagr']:>+6.1f}%, MDD={s['mdd']:>6.1f}%, Calmar={s['calmar']:.2f}{sh}{so}"


def print_yearwise(s, label, trades=0, charges=0, slippage=0):
    if not s:
        return
    print(f"\n  {label}")
    print(f"  {fmt(s)}")
    print(f"  Growth={s['tr']:.1f}x, {trades} trades, "
          f"costs={charges+slippage:,.0f} ({(charges+slippage)/CAPITAL*100:.1f}% of capital)")
    print(f"\n  {'Year':<6} {'Return':>9} {'MaxDD':>9} {'EndValue':>14}")
    print(f"  {'-'*42}")
    for yr in sorted(s["yearly"].keys()):
        y = s["yearly"][yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
        print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}% {y['last']:>14,.0f}")


# ── Main ─────────────────────────────────────────────────────────────────────

ETF_PAIRS = [
    ("SPY", "EWJ", "SPY vs EWJ (Japan)"),
    ("SPY", "INDA", "SPY vs INDA (India)"),
    ("SPY", "FXI", "SPY vs FXI (China/HK)"),
    ("SPY", "EWU", "SPY vs EWU (UK)"),
    ("SPY", "EWG", "SPY vs EWG (Germany)"),
    ("SPY", "EWZ", "SPY vs EWZ (Brazil)"),
]

# Also test z-score parameters to see sensitivity (not a full sweep — just 3 configs)
CONFIGS = [
    (20, 1.0, -0.5, "default (z20, entry=1.0, exit=-0.5)"),
    (20, 0.75, -0.5, "aggressive (z20, entry=0.75, exit=-0.5)"),
    (30, 1.0, 0.0, "conservative (z30, entry=1.0, exit=0.0)"),
]


def main():
    start_epoch = 1104537600
    end_epoch = 1773878400
    split_epoch = 1451606400  # 2016-01-01

    cr = CetaResearch()

    # Fetch all ETFs
    all_data = {}
    needed = set()
    for sa, sb, _ in ETF_PAIRS:
        needed.add(sa)
        needed.add(sb)
    for sym in sorted(needed):
        print(f"  Fetching {sym}...")
        data = fetch_close(cr, sym, "fmp", start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            epochs = sorted(data.keys())
            print(f"    {len(data)} days, {datetime.fromtimestamp(epochs[0], tz=timezone.utc).date()} "
                  f"to {datetime.fromtimestamp(epochs[-1], tz=timezone.utc).date()}")

    # ── Compare 3 execution models per pair ──
    print("\n" + "=" * 100)
    print("  EXECUTION MODEL COMPARISON: Same-bar vs MOC vs Next-day")
    print("  All use real US-listed ETFs, 5bps slippage, US charges")
    print("=" * 100)

    for sa, sb, label in ETF_PAIRS:
        if sa not in all_data or sb not in all_data:
            continue
        common = align([all_data[sa], all_data[sb]], start_epoch)
        if len(common) < 200:
            continue
        ca = [all_data[sa][e] for e in common]
        cb = [all_data[sb][e] for e in common]
        start_date = datetime.fromtimestamp(common[0], tz=timezone.utc).date()

        # Buy-and-hold benchmarks
        bh_a = [ca[i] / ca[0] * CAPITAL for i in range(len(ca))]
        bh_b = [cb[i] / cb[0] * CAPITAL for i in range(len(cb))]
        s_bh_a = compute_stats(bh_a, common)
        s_bh_b = compute_stats(bh_b, common)

        print(f"\n{'━'*100}")
        print(f"  {label} ({len(common)} days from {start_date})")
        print(f"{'━'*100}")
        if s_bh_a:
            print(f"  B&H {sa}: {fmt(s_bh_a)}")
        if s_bh_b:
            print(f"  B&H {sb}: {fmt(s_bh_b)}")

        # MOC model (the realistic one)
        vals_moc, tr_moc, ch_moc, sl_moc = sim_moc(common, ca, cb)
        ep_moc = common[Z_LOOKBACK + 1:]
        s_moc = compute_stats(vals_moc, ep_moc)
        print_yearwise(s_moc, f"MOC (signal yesterday → execute today's close)", tr_moc, ch_moc, sl_moc)

        # Next-day model (too conservative)
        vals_nd, tr_nd, ch_nd, sl_nd = sim_nextday(common, ca, cb)
        ep_nd = common[Z_LOOKBACK:]
        s_nd = compute_stats(vals_nd, ep_nd)

        print(f"\n  Comparison:")
        print(f"    MOC (realistic): {fmt(s_moc)}, {tr_moc} trades")
        print(f"    Next-day (cons): {fmt(s_nd)}, {tr_nd} trades")

        # Train/test on MOC
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        if train_end > Z_LOOKBACK + 50 and train_end < len(common) - 50:
            vals_test, tr_te, ch_te, sl_te = sim_moc(
                common[train_end:], ca[train_end:], cb[train_end:])
            s_test = compute_stats(vals_test, common[train_end + Z_LOOKBACK + 1:])
            if s_test:
                print(f"    MOC OOS (2016+):  {fmt(s_test)}, {tr_te} trades")

    # ── Multi-pair MOC portfolio ──
    print("\n\n" + "=" * 100)
    print("  MULTI-PAIR MOC PORTFOLIO (all ETFs, equal weight)")
    print("=" * 100)

    valid_pairs = [(sa, sb, label) for sa, sb, label in ETF_PAIRS
                   if sa in all_data and sb in all_data]
    if len(valid_pairs) >= 2:
        all_syms = set()
        for sa, sb, _ in valid_pairs:
            all_syms.add(sa)
            all_syms.add(sb)
        common = align([all_data[s] for s in all_syms], start_epoch)

        if len(common) >= 200:
            n_pairs = len(valid_pairs)
            per_pair = CAPITAL / n_pairs
            start_date = datetime.fromtimestamp(common[0], tz=timezone.utc).date()
            print(f"\n  {n_pairs} pairs, {len(common)} common days from {start_date}")

            all_vals = []
            total_tr = total_ch = total_sl = 0
            for sa, sb, label in valid_pairs:
                ca = [all_data[sa][e] for e in common]
                cb = [all_data[sb][e] for e in common]
                vals, tr, ch, sl = sim_moc(common, ca, cb, capital=per_pair)
                all_vals.append(vals)
                total_tr += tr
                total_ch += ch
                total_sl += sl
                print(f"    {label}: {tr} trades")

            min_len = min(len(v) for v in all_vals)
            combined = [sum(v[i] for v in all_vals) for i in range(min_len)]
            ep = common[Z_LOOKBACK + 1:]
            s = compute_stats(combined, ep)
            print_yearwise(s, f"COMBINED {n_pairs}-PAIR MOC PORTFOLIO",
                           total_tr, total_ch, total_sl)

            # SPY benchmark
            spy_bh = [all_data["SPY"][e] / all_data["SPY"][common[0]] * CAPITAL for e in common]
            s_spy = compute_stats(spy_bh, common)
            if s_spy:
                print(f"\n  B&H SPY: {fmt(s_spy)}")

    # ── Sensitivity: try 3 configs on best pair ──
    print("\n\n" + "=" * 100)
    print("  CONFIG SENSITIVITY (SPY vs EWJ — best pair)")
    print("=" * 100)

    sa, sb = "SPY", "EWJ"
    if sa in all_data and sb in all_data:
        common = align([all_data[sa], all_data[sb]], start_epoch)
        ca = [all_data[sa][e] for e in common]
        cb = [all_data[sb][e] for e in common]

        print(f"\n  {'Config':<45} {'CAGR':>7} {'MDD':>7} {'Calm':>6} {'Shrp':>5} {'Tr':>5}")
        print(f"  {'-'*80}")

        for zlb, ze, zx, desc in CONFIGS:
            vals, tr, ch, sl = sim_moc(common, ca, cb, z_lookback=zlb,
                                        z_entry=ze, z_exit=zx)
            ep = common[zlb + 1:]
            s = compute_stats(vals, ep)
            if s:
                print(f"  {desc:<45} {s['cagr']:>+6.1f}% {s['mdd']:>6.1f}% "
                      f"{s['calmar']:>6.2f} {s.get('sharpe') or 0:>5.2f} {tr:>5}")

            # OOS
            train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
            if train_end > zlb + 50 and train_end < len(common) - 50:
                vals_oos, tr_oos, _, _ = sim_moc(
                    common[train_end:], ca[train_end:], cb[train_end:],
                    z_lookback=zlb, z_entry=ze, z_exit=zx)
                s_oos = compute_stats(vals_oos, common[train_end + zlb + 1:])
                if s_oos:
                    print(f"    OOS (2016+): {fmt(s_oos)}, {tr_oos} trades")


if __name__ == "__main__":
    main()

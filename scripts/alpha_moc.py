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
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult
from lib.indicators import compute_z
from lib.data_fetchers import fetch_close, align


# ── Fixed config ─────────────────────────────────────────────────────────────

Z_LOOKBACK = 20
Z_ENTRY = 1.0
Z_EXIT = -0.5
SLIPPAGE_PCT = 0.0005  # 5 bps
CAPITAL = 10_000_000


# ── Simulation ───────────────────────────────────────────────────────────────

def sim_moc(epochs, closes_a, closes_b, z_lookback=Z_LOOKBACK,
            z_entry=Z_ENTRY, z_exit=Z_EXIT, capital=CAPITAL,
            slippage=SLIPPAGE_PCT, instrument="SPY vs EWJ",
            exchange="US", params_dict=None):
    """MOC execution: signal from day i-1 ratio, execute at day i close.

    Z-score on bar i uses ratio[i-lookback:i] (past only).
    But we use z[i-1] (yesterday's signal) to decide today's trade.
    Execution at closes_a[i] / closes_b[i] (today's close via MOC order).
    """
    if params_dict is None:
        params_dict = {"z_lookback": z_lookback, "z_entry": z_entry, "z_exit": z_exit}

    result = BacktestResult(
        "pairs_moc", params_dict, instrument, exchange, capital,
        slippage_bps=int(slippage * 10000),
    )

    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    buy_ch = 0.0
    buy_slip = 0.0

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
                result.add_trade(
                    entry_epoch=epochs[ei], exit_epoch=epochs[i],
                    entry_price=ep, exit_price=exit_price,
                    quantity=qty, side="LONG",
                    charges=buy_ch + ch, slippage=buy_slip + slip,
                )
                position = None
                buy_ch = 0.0
                buy_slip = 0.0

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
                            buy_ch = ch
                            buy_slip = slip

        # Portfolio value at today's close
        if position:
            side, qty, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            result.add_equity_point(epochs[i], cash + qty * cp)
        else:
            result.add_equity_point(epochs[i], cash)

    return result


def sim_nextday(epochs, closes_a, closes_b, z_lookback=Z_LOOKBACK,
                z_entry=Z_ENTRY, z_exit=Z_EXIT, capital=CAPITAL,
                slippage=SLIPPAGE_PCT, instrument="SPY vs EWJ",
                exchange="US", params_dict=None):
    """Next-day execution: signal from day i, execute at day i+1 close.
    (Same as alpha_corrected.py for comparison.)
    """
    if params_dict is None:
        params_dict = {"z_lookback": z_lookback, "z_entry": z_entry, "z_exit": z_exit}

    result = BacktestResult(
        "pairs_nextday", params_dict, instrument, exchange, capital,
        slippage_bps=int(slippage * 10000),
    )

    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None
    buy_ch = 0.0
    buy_slip = 0.0
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
            result.add_trade(
                entry_epoch=epochs[ei], exit_epoch=epochs[i],
                entry_price=ep, exit_price=exit_price,
                quantity=qty, side="LONG",
                charges=buy_ch + ch, slippage=buy_slip + slip,
            )
            position = None
            buy_ch = 0.0
            buy_slip = 0.0
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
                        buy_ch = ch
                        buy_slip = slip
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
            result.add_equity_point(epochs[i], cash + qty * cp)
        else:
            result.add_equity_point(epochs[i], cash)

    return result


# ── Formatting helpers ───────────────────────────────────────────────────────

def _fmt_summary(s):
    """Format a BacktestResult summary dict into a one-line string."""
    if not s:
        return "NO DATA"
    cagr = (s.get("cagr") or 0) * 100
    mdd = (s.get("max_drawdown") or 0) * 100
    calmar = s.get("calmar_ratio") or 0
    parts = [f"CAGR={cagr:>+6.1f}%", f"MDD={mdd:>6.1f}%", f"Calmar={calmar:.2f}"]
    sh = s.get("sharpe_ratio")
    if sh is not None:
        parts.append(f"Sharpe={sh:.2f}")
    so = s.get("sortino_ratio")
    if so is not None:
        parts.append(f"Sortino={so:.2f}")
    return ", ".join(parts)


def _fmt_bh(values, epochs):
    """Compute buy-and-hold stats and return a formatted summary string."""
    if len(values) < 2:
        return "NO DATA", None
    sv, ev = values[0], values[-1]
    yrs = (epochs[-1] - epochs[0]) / (365.25 * 86400)
    if yrs <= 0 or ev <= 0 or sv <= 0:
        return "NO DATA", None
    cagr = ((ev / sv) ** (1 / yrs) - 1) * 100
    peak = sv
    mdd = 0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    return f"CAGR={cagr:>+6.1f}%, MDD={mdd:>6.1f}%, Calmar={calmar:.2f}", {
        "cagr": cagr, "mdd": mdd, "calmar": calmar, "yrs": yrs, "tr": ev / sv
    }


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

    # ── Compare execution models per pair ──
    print("\n" + "=" * 100)
    print("  EXECUTION MODEL COMPARISON: MOC vs Next-day")
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
        instrument = f"{sa} vs {sb}"

        # Buy-and-hold benchmarks
        bh_a = [ca[i] / ca[0] * CAPITAL for i in range(len(ca))]
        bh_b = [cb[i] / cb[0] * CAPITAL for i in range(len(cb))]
        bh_a_str, _ = _fmt_bh(bh_a, common)
        bh_b_str, _ = _fmt_bh(bh_b, common)

        print(f"\n{'━'*100}")
        print(f"  {label} ({len(common)} days from {start_date})")
        print(f"{'━'*100}")
        print(f"  B&H {sa}: {bh_a_str}")
        print(f"  B&H {sb}: {bh_b_str}")

        # MOC model (the realistic one)
        result_moc = sim_moc(common, ca, cb, instrument=instrument)
        # Benchmark must match equity curve length (starts at z_lookback+1)
        bm_start = Z_LOOKBACK + 1
        result_moc.set_benchmark_values(common[bm_start:], bh_a[bm_start:])
        result_moc.compute()
        s_moc = result_moc.to_dict()["summary"]
        c_moc = result_moc.to_dict()["costs"]
        tr_moc = s_moc.get("total_trades", 0)

        result_moc.print_summary()

        # Next-day model (too conservative)
        result_nd = sim_nextday(common, ca, cb, instrument=instrument)
        result_nd.compute()
        s_nd = result_nd.to_dict()["summary"]
        tr_nd = s_nd.get("total_trades", 0)

        print(f"\n  Comparison:")
        print(f"    MOC (realistic): {_fmt_summary(s_moc)}, {tr_moc} trades")
        print(f"    Next-day (cons): {_fmt_summary(s_nd)}, {tr_nd} trades")

        # Train/test on MOC
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        if train_end > Z_LOOKBACK + 50 and train_end < len(common) - 50:
            result_oos = sim_moc(
                common[train_end:], ca[train_end:], cb[train_end:],
                instrument=instrument)
            result_oos.compute()
            s_oos = result_oos.to_dict()["summary"]
            tr_oos = s_oos.get("total_trades", 0)
            if s_oos.get("cagr") is not None:
                print(f"    MOC OOS (2016+):  {_fmt_summary(s_oos)}, {tr_oos} trades")

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

            all_results = []
            for sa, sb, label in valid_pairs:
                ca = [all_data[sa][e] for e in common]
                cb = [all_data[sb][e] for e in common]
                instrument = f"{sa} vs {sb}"
                r = sim_moc(common, ca, cb, capital=per_pair, instrument=instrument)
                r.compute()
                tr = r.to_dict()["summary"].get("total_trades", 0)
                all_results.append(r)
                print(f"    {label}: {tr} trades")

            # Build combined equity curve from individual results
            # All results start at z_lookback+1, so they should have the same length
            combined_epochs = common[Z_LOOKBACK + 1:]
            all_equity = []
            for r in all_results:
                eq = r.equity_curve
                all_equity.append(eq)

            min_len = min(len(eq) for eq in all_equity)
            combined_values = [sum(eq[i][1] for eq in all_equity) for i in range(min_len)]
            combined_ep = [all_equity[0][i][0] for i in range(min_len)]

            # Create a combined BacktestResult
            combined_result = BacktestResult(
                "pairs_moc", {"pairs": n_pairs, "z_lookback": Z_LOOKBACK,
                              "z_entry": Z_ENTRY, "z_exit": Z_EXIT},
                f"Multi({n_pairs} pairs)", "US", CAPITAL,
                slippage_bps=int(SLIPPAGE_PCT * 10000),
            )
            for i in range(min_len):
                combined_result.add_equity_point(combined_ep[i], combined_values[i])
            # Copy trades from all individual results
            for r in all_results:
                for t in r.trades:
                    combined_result.trades.append(t)
                    combined_result.costs["total_charges"] += t["charges"]
                    combined_result.costs["total_slippage"] += t["slippage"]

            # SPY benchmark
            spy_bh = [all_data["SPY"][e] / all_data["SPY"][common[0]] * CAPITAL for e in common]
            spy_bh_epochs = common
            combined_result.set_benchmark_values(
                spy_bh_epochs[Z_LOOKBACK + 1:Z_LOOKBACK + 1 + min_len],
                spy_bh[Z_LOOKBACK + 1:Z_LOOKBACK + 1 + min_len],
            )
            combined_result.compute()
            combined_result.print_summary()

            spy_bh_str, _ = _fmt_bh(spy_bh, common)
            print(f"\n  B&H SPY: {spy_bh_str}")

    # ── Sensitivity: try 3 configs on best pair ──
    print("\n\n" + "=" * 100)
    print("  CONFIG SENSITIVITY (SPY vs EWJ — best pair)")
    print("=" * 100)

    sa, sb = "SPY", "EWJ"
    if sa in all_data and sb in all_data:
        common = align([all_data[sa], all_data[sb]], start_epoch)
        ca = [all_data[sa][e] for e in common]
        cb = [all_data[sb][e] for e in common]

        sweep = SweepResult("pairs_moc", "SPY vs EWJ", "US", CAPITAL,
                            slippage_bps=int(SLIPPAGE_PCT * 10000))

        for zlb, ze, zx, desc in CONFIGS:
            params = {"z_lookback": zlb, "z_entry": ze, "z_exit": zx, "desc": desc}
            r = sim_moc(common, ca, cb, z_lookback=zlb, z_entry=ze, z_exit=zx,
                        instrument="SPY vs EWJ", params_dict=params)
            r.compute()
            sweep.add_config(params, r)

            # OOS
            train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
            if train_end > zlb + 50 and train_end < len(common) - 50:
                r_oos = sim_moc(
                    common[train_end:], ca[train_end:], cb[train_end:],
                    z_lookback=zlb, z_entry=ze, z_exit=zx,
                    instrument="SPY vs EWJ",
                    params_dict={**params, "period": "OOS_2016+"})
                r_oos.compute()
                s_oos = r_oos.to_dict()["summary"]
                tr_oos = s_oos.get("total_trades", 0)
                if s_oos.get("cagr") is not None:
                    print(f"    {desc} OOS (2016+): {_fmt_summary(s_oos)}, {tr_oos} trades")

        sweep.print_leaderboard(top_n=10)
        sweep.save("result.json", top_n=10, sort_by="calmar_ratio")


if __name__ == "__main__":
    main()

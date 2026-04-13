#!/usr/bin/env python3
"""Explore variations on the MOC pairs strategy (SPY vs EWJ as base).

Variations tested:
  1. Z-score parameters: lookback (10-60), entry threshold (0.5-2.0), exit (-1.0 to +0.5)
  2. Trend filter: only trade in direction of 60/120/200-day momentum
  3. Volatility regime: reduce exposure when realized vol is elevated
  4. Asymmetric sizing: deploy more capital at deeper z-scores
  5. Hold timer: minimum hold days to avoid whipsaws
  6. Dual timeframe: short z for entry timing, long z for direction
  7. Multi-pair with only the best pairs (EWJ + EWU + INDA)
  8. Adaptive z-score: use expanding window instead of fixed

All use MOC execution model (signal from yesterday, execute at today's close).
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
from lib.backtest_result import BacktestResult, SweepResult, MultiSweepResult
from lib.indicators import compute_z, compute_sma, compute_realized_vol
from lib.data_fetchers import fetch_close, align

CAPITAL = 10_000_000
SLIPPAGE = 0.0005


def us_charges(value, side):
    return calculate_charges("US", value, "EQUITY", "DELIVERY", side)


def _fmt_summary(s):
    """Format BacktestResult summary dict into one line."""
    if not s:
        return "NO DATA"
    cagr = (s.get("cagr") or 0) * 100
    mdd = (s.get("max_drawdown") or 0) * 100
    calmar = s.get("calmar_ratio") or 0
    parts = [f"CAGR={cagr:>+6.1f}%", f"MDD={mdd:>6.1f}%", f"Cal={calmar:.2f}"]
    sh = s.get("sharpe_ratio")
    if sh is not None:
        parts.append(f"Sh={sh:.2f}")
    so = s.get("sortino_ratio")
    if so is not None:
        parts.append(f"So={so:.2f}")
    return " ".join(parts)


# ── Unified Simulator ────────────────────────────────────────────────────────

def sim_moc(epochs, closes_a, closes_b, *,
            z_lookback=20, z_entry=1.0, z_exit=-0.5,
            # Trend filter
            trend_filter=False, trend_period=120,
            # Vol regime
            vol_filter=False, vol_window=20, vol_avg_window=60, vol_threshold=2.0,
            # Conviction sizing
            conviction=False,  # deeper z -> more capital
            # Min hold
            min_hold_days=0,
            # Dual timeframe
            dual_z=False, z_long_lookback=60,
            capital=CAPITAL, slippage=SLIPPAGE,
            instrument="SPY vs EWJ", exchange="US", params_dict=None):
    """MOC pairs sim with all optional layers. Returns (BacktestResult, warmup)."""
    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)
    z_long = compute_z(ratios, z_long_lookback) if dual_z else None

    # Optional: trend (SMA on each side)
    sma_a = compute_sma(closes_a, trend_period) if trend_filter else None
    sma_b = compute_sma(closes_b, trend_period) if trend_filter else None

    # Optional: vol
    vol = compute_realized_vol(closes_a, vol_window) if vol_filter else None
    vol_avg = compute_sma(vol, vol_avg_window) if vol_filter and vol else None

    warmup = max(z_lookback + 1,
                 z_long_lookback + 1 if dual_z else 0,
                 trend_period if trend_filter else 0,
                 vol_avg_window if vol_filter else 0)

    result = BacktestResult(
        "alpha_variations", params_dict or {}, instrument, exchange, capital,
        slippage_bps=int(slippage * 10000),
    )

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx, entry_epoch)
    trades = 0
    total_costs = 0.0
    buy_ch = 0.0
    buy_sl = 0.0

    for i in range(warmup, n):
        z_sig = z_scores[i - 1]  # yesterday's signal
        zl_sig = z_long[i - 1] if dual_z else z_sig

        # ── EXIT ──
        if position:
            side, qty, ep, ei, ee = position
            should_exit = False

            if side == "a" and z_sig >= z_exit:
                should_exit = True
            elif side == "b" and z_sig <= -z_exit:
                should_exit = True

            # Min hold check
            if min_hold_days > 0 and should_exit:
                days_held = (epochs[i] - ee) / 86400
                if days_held < min_hold_days:
                    should_exit = False

            if should_exit:
                exit_price = closes_a[i] if side == "a" else closes_b[i]
                sell_val = qty * exit_price
                sell_ch = us_charges(sell_val, "SELL_SIDE")
                sell_sl = sell_val * slippage
                cash += sell_val - sell_ch - sell_sl
                total_costs += sell_ch + sell_sl
                result.add_trade(
                    entry_epoch=ee, exit_epoch=epochs[i],
                    entry_price=ep, exit_price=exit_price,
                    quantity=qty, side="LONG",
                    charges=buy_ch + sell_ch,
                    slippage=buy_sl + sell_sl,
                )
                position = None
                buy_ch = 0.0
                buy_sl = 0.0

        # ── ENTRY ──
        if position is None:
            buy_side = None
            if z_sig < -z_entry:
                buy_side = "a"
            elif z_sig > z_entry:
                buy_side = "b"

            # Dual z confirmation
            if buy_side and dual_z:
                if buy_side == "a" and zl_sig > -z_entry * 0.3:
                    buy_side = None
                elif buy_side == "b" and zl_sig < z_entry * 0.3:
                    buy_side = None

            # Trend filter: only buy the side that's above its trend
            if buy_side and trend_filter:
                if buy_side == "a" and closes_a[i - 1] < sma_a[i - 1]:
                    buy_side = None  # A is below trend, don't buy
                elif buy_side == "b" and closes_b[i - 1] < sma_b[i - 1]:
                    buy_side = None

            # Vol filter: reduce size if vol is elevated
            vol_mult = 1.0
            if buy_side and vol_filter and vol_avg and vol_avg[i - 1] > 0:
                ratio = vol[i - 1] / vol_avg[i - 1]
                if ratio > vol_threshold:
                    vol_mult = 0.5

            # Conviction: deeper z -> more capital
            size_pct = 0.95  # base
            if buy_side and conviction:
                abs_z = abs(z_sig)
                if abs_z >= 2.5:
                    size_pct = 0.95
                elif abs_z >= 2.0:
                    size_pct = 0.80
                elif abs_z >= 1.5:
                    size_pct = 0.60
                else:
                    size_pct = 0.40

            if buy_side:
                buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
                invest = cash * size_pct * vol_mult
                if buy_price > 0 and invest > 0:
                    qty = int(invest / buy_price)
                    if qty > 0:
                        cost = qty * buy_price
                        ch = us_charges(cost, "BUY_SIDE")
                        sl = cost * slippage
                        if cost + ch + sl <= cash:
                            position = (buy_side, qty, buy_price, i, epochs[i])
                            cash -= cost + ch + sl
                            total_costs += ch + sl
                            buy_ch = ch
                            buy_sl = sl
                            trades += 1

        if position:
            side, qty, _, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            result.add_equity_point(epochs[i], cash + qty * cp)
        else:
            result.add_equity_point(epochs[i], cash)

    return result, warmup


# ── Sweep Runner ─────────────────────────────────────────────────────────────

def run_sweep(epochs, ca, cb, configs, label, split_epoch=None,
              instrument="SPY vs EWJ", exchange="US"):
    """Run all configs and print results."""
    sweep = SweepResult("alpha_variations", instrument, exchange, CAPITAL,
                        slippage_bps=int(SLIPPAGE * 10000))

    oos_data = []  # [(desc, oos_result)]

    for i, (desc, kwargs) in enumerate(configs):
        params_dict = dict(kwargs)
        params_dict["desc"] = desc
        result, wu = sim_moc(epochs, ca, cb, instrument=instrument,
                             exchange=exchange, params_dict=params_dict, **kwargs)
        sweep.add_config(params_dict, result)

        # OOS if split_epoch provided
        if split_epoch:
            te = next((j for j, e in enumerate(epochs) if e >= split_epoch), len(epochs))
            if te > wu + 50 and te < len(epochs) - 50:
                oos_result, wu_oos = sim_moc(
                    epochs[te:], ca[te:], cb[te:],
                    instrument=instrument, exchange=exchange,
                    params_dict=params_dict, **kwargs)
                oos_result.compute()
                oos_data.append((desc, oos_result))

    # Print leaderboard with OOS info
    sorted_configs = sweep._sorted("calmar_ratio")
    oos_lookup = {d: r for d, r in oos_data}

    print(f"\n{'='*130}")
    print(f"  {label} -- {len(sorted_configs)} configs, sorted by Calmar")
    print(f"{'='*130}")
    print(f"  {'#':<3} {'Description':<42} {'CAGR':>7} {'MDD':>7} {'Cal':>5} "
          f"{'Shrp':>5} {'Sort':>5} {'Tr':>4} "
          f"{'OOS CAGR':>9} {'OOS Cal':>8}")
    print(f"  {'-'*110}")

    for i, (params, result) in enumerate(sorted_configs[:25]):
        s = result.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        cal = s.get("calmar_ratio") or 0
        sh = s.get("sharpe_ratio") or 0
        so = s.get("sortino_ratio") or 0
        tr = s.get("total_trades") or 0
        desc = params.get("desc", str(params))

        oos_r = oos_lookup.get(desc)
        if oos_r:
            oos_s = oos_r.to_dict()["summary"]
            oos_cagr = f"{(oos_s.get('cagr') or 0)*100:>+7.1f}%"
            oos_cal = f"{oos_s.get('calmar_ratio') or 0:>7.2f}"
        else:
            oos_cagr = "     N/A"
            oos_cal = "     N/A"

        print(f"  {i+1:<3} {desc:<42} "
              f"{cagr:>+6.1f}% {mdd:>6.1f}% {cal:>5.2f} "
              f"{sh:>5.2f} {so:>5.2f} {tr:>4} "
              f"{oos_cagr} {oos_cal}")

    # Year-wise for best
    if sorted_configs:
        best_params, best_result = sorted_configs[0]
        print(f"\n  BEST: {best_params.get('desc', str(best_params))}")
        best_result.print_summary()

    return sweep


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600
    end_epoch = 1773878400
    split_epoch = 1451606400  # 2016-01-01

    cr = CetaResearch()

    # Fetch ETFs
    etfs = ["SPY", "EWJ", "INDA", "FXI", "EWU", "EWG", "EWZ"]
    all_data = {}
    for sym in etfs:
        print(f"  Fetching {sym}...")
        data = fetch_close(cr, sym, start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            print(f"    {len(data)} days")

    multi = MultiSweepResult("alpha_variations", "Variation sweeps on SPY vs EWJ")

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 1: Z-Score Parameters (SPY vs EWJ)
    # ══════════════════════════════════════════════════════════════════════════
    sa, sb = "SPY", "EWJ"
    common = align([all_data[sa], all_data[sb]], start_epoch)
    ca = [all_data[sa][e] for e in common]
    cb = [all_data[sb][e] for e in common]
    print(f"\n  Base pair: {sa} vs {sb}, {len(common)} days")

    configs_zscore = []
    for zlb in [10, 15, 20, 30, 45, 60]:
        for ze in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
            for zx in [-1.0, -0.5, 0.0, 0.5]:
                desc = f"z{zlb} in={ze:.2f} out={zx:.1f}"
                configs_zscore.append((desc, dict(
                    z_lookback=zlb, z_entry=ze, z_exit=zx)))

    sweep1 = run_sweep(common, ca, cb, configs_zscore,
                       "VAR 1: Z-Score Parameters", split_epoch)
    multi.add_sweep("var1_zscore_params", sweep1)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 2: Trend Filter
    # ══════════════════════════════════════════════════════════════════════════
    configs_trend = []
    for tp in [60, 120, 200]:
        for ze in [0.75, 1.0]:
            for zx in [-0.5, 0.0]:
                # With trend
                desc = f"z20 in={ze} out={zx} trend={tp}d"
                configs_trend.append((desc, dict(
                    z_lookback=20, z_entry=ze, z_exit=zx,
                    trend_filter=True, trend_period=tp)))
                # Without trend (baseline)
                desc = f"z20 in={ze} out={zx} no-trend"
                configs_trend.append((desc, dict(
                    z_lookback=20, z_entry=ze, z_exit=zx,
                    trend_filter=False)))

    sweep2 = run_sweep(common, ca, cb, configs_trend,
                       "VAR 2: Trend Filter", split_epoch)
    multi.add_sweep("var2_trend_filter", sweep2)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 3: Volatility Regime Filter
    # ══════════════════════════════════════════════════════════════════════════
    configs_vol = []
    for vt in [1.5, 2.0, 2.5]:
        for ze in [0.75, 1.0]:
            desc = f"z20 in={ze} vol_thresh={vt}x"
            configs_vol.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=-0.5,
                vol_filter=True, vol_threshold=vt)))
    # Baselines
    for ze in [0.75, 1.0]:
        desc = f"z20 in={ze} no-vol-filter"
        configs_vol.append((desc, dict(
            z_lookback=20, z_entry=ze, z_exit=-0.5, vol_filter=False)))

    sweep3 = run_sweep(common, ca, cb, configs_vol,
                       "VAR 3: Volatility Regime", split_epoch)
    multi.add_sweep("var3_vol_regime", sweep3)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 4: Conviction Sizing
    # ══════════════════════════════════════════════════════════════════════════
    configs_conv = []
    for ze in [0.75, 1.0]:
        for zx in [-0.5, 0.0]:
            desc = f"z20 in={ze} out={zx} conviction=ON"
            configs_conv.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=zx, conviction=True)))
            desc = f"z20 in={ze} out={zx} conviction=OFF"
            configs_conv.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=zx, conviction=False)))

    sweep4 = run_sweep(common, ca, cb, configs_conv,
                       "VAR 4: Conviction Sizing", split_epoch)
    multi.add_sweep("var4_conviction", sweep4)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 5: Minimum Hold Days
    # ══════════════════════════════════════════════════════════════════════════
    configs_hold = []
    for mh in [0, 3, 5, 10, 20]:
        for ze in [0.75, 1.0]:
            desc = f"z20 in={ze} min_hold={mh}d"
            configs_hold.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=-0.5, min_hold_days=mh)))

    sweep5 = run_sweep(common, ca, cb, configs_hold,
                       "VAR 5: Minimum Hold Days", split_epoch)
    multi.add_sweep("var5_min_hold", sweep5)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 6: Dual Timeframe Z-Score
    # ══════════════════════════════════════════════════════════════════════════
    configs_dual = []
    for zl in [45, 60, 90, 120]:
        for ze in [0.75, 1.0]:
            desc = f"dual z_short=20 z_long={zl} in={ze}"
            configs_dual.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=-0.5,
                dual_z=True, z_long_lookback=zl)))
    # Baselines
    for ze in [0.75, 1.0]:
        desc = f"single z=20 in={ze} (baseline)"
        configs_dual.append((desc, dict(
            z_lookback=20, z_entry=ze, z_exit=-0.5, dual_z=False)))

    sweep6 = run_sweep(common, ca, cb, configs_dual,
                       "VAR 6: Dual Timeframe Z", split_epoch)
    multi.add_sweep("var6_dual_z", sweep6)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 7: Combined Best Layers
    # ══════════════════════════════════════════════════════════════════════════
    configs_combined = []
    for zlb in [15, 20]:
        for ze in [0.75, 1.0]:
            for zx in [-0.5, 0.0]:
                # Kitchen sink
                desc = f"z{zlb} in={ze} out={zx} ALL"
                configs_combined.append((desc, dict(
                    z_lookback=zlb, z_entry=ze, z_exit=zx,
                    trend_filter=True, trend_period=120,
                    vol_filter=True, vol_threshold=2.0,
                    conviction=True, min_hold_days=5,
                    dual_z=True, z_long_lookback=60)))
                # Trend + vol only
                desc = f"z{zlb} in={ze} out={zx} trend+vol"
                configs_combined.append((desc, dict(
                    z_lookback=zlb, z_entry=ze, z_exit=zx,
                    trend_filter=True, trend_period=120,
                    vol_filter=True, vol_threshold=2.0)))
                # Conviction + hold
                desc = f"z{zlb} in={ze} out={zx} conv+hold"
                configs_combined.append((desc, dict(
                    z_lookback=zlb, z_entry=ze, z_exit=zx,
                    conviction=True, min_hold_days=5)))
                # Baseline
                desc = f"z{zlb} in={ze} out={zx} naked"
                configs_combined.append((desc, dict(
                    z_lookback=zlb, z_entry=ze, z_exit=zx)))

    sweep7 = run_sweep(common, ca, cb, configs_combined,
                       "VAR 7: Combined Layers (SPY vs EWJ)", split_epoch)
    multi.add_sweep("var7_combined", sweep7)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 8: Best config on other pairs (print-only)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  VAR 8: Best configs applied to ALL pairs")
    print("=" * 130)

    best_configs = [
        ("naked z20/1.0/-0.5", dict(z_lookback=20, z_entry=1.0, z_exit=-0.5)),
        ("naked z20/0.75/-0.5", dict(z_lookback=20, z_entry=0.75, z_exit=-0.5)),
        ("naked z15/1.0/-0.5", dict(z_lookback=15, z_entry=1.0, z_exit=-0.5)),
        ("trend120 z20/1.0/-0.5", dict(z_lookback=20, z_entry=1.0, z_exit=-0.5,
                                        trend_filter=True, trend_period=120)),
    ]

    pairs = [
        ("SPY", "EWJ", "SPY/EWJ"),
        ("SPY", "INDA", "SPY/INDA"),
        ("SPY", "EWU", "SPY/EWU"),
        ("SPY", "EWG", "SPY/EWG"),
        ("SPY", "FXI", "SPY/FXI"),
        ("SPY", "EWZ", "SPY/EWZ"),
    ]

    print(f"\n  {'Pair':<12} {'Config':<28} {'CAGR':>7} {'MDD':>7} {'Cal':>5} "
          f"{'Shrp':>5} {'Tr':>4} {'OOS CAGR':>9} {'OOS Cal':>8}")
    print(f"  {'-'*100}")

    for pa, pb, plabel in pairs:
        if pa not in all_data or pb not in all_data:
            continue
        com = align([all_data[pa], all_data[pb]], start_epoch)
        if len(com) < 200:
            continue
        c_a = [all_data[pa][e] for e in com]
        c_b = [all_data[pb][e] for e in com]

        for desc, kwargs in best_configs:
            result, wu = sim_moc(com, c_a, c_b,
                                 instrument=f"{pa} vs {pb}", exchange="US",
                                 params_dict={"desc": desc}, **kwargs)
            result.compute()
            s = result.to_dict()["summary"]

            # OOS
            te = next((j for j, e in enumerate(com) if e >= split_epoch), len(com))
            oos_s = None
            oos_tr = 0
            if te > wu + 50 and te < len(com) - 50:
                oos_result, wu_oos = sim_moc(
                    com[te:], c_a[te:], c_b[te:],
                    instrument=f"{pa} vs {pb}", exchange="US",
                    params_dict={"desc": desc}, **kwargs)
                oos_result.compute()
                oos_s = oos_result.to_dict()["summary"]

            if s:
                cagr = (s.get("cagr") or 0) * 100
                mdd = (s.get("max_drawdown") or 0) * 100
                cal = s.get("calmar_ratio") or 0
                sh = s.get("sharpe_ratio") or 0
                tr = s.get("total_trades") or 0
                if oos_s:
                    oos_cagr = f"{(oos_s.get('cagr') or 0)*100:>+7.1f}%"
                    oos_cal = f"{oos_s.get('calmar_ratio') or 0:>7.2f}"
                else:
                    oos_cagr = "     N/A"
                    oos_cal = "     N/A"
                print(f"  {plabel:<12} {desc:<28} "
                      f"{cagr:>+6.1f}% {mdd:>6.1f}% {cal:>5.2f} "
                      f"{sh:>5.2f} {tr:>4} "
                      f"{oos_cagr} {oos_cal}")

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 9: Multi-pair with best 3 pairs only (print-only)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 130)
    print("  VAR 9: Multi-pair portfolios (best pairs only)")
    print("=" * 130)

    multi_combos = [
        [("SPY", "EWJ")],
        [("SPY", "EWJ"), ("SPY", "EWU")],
        [("SPY", "EWJ"), ("SPY", "EWU"), ("SPY", "INDA")],
        [("SPY", "EWJ"), ("SPY", "EWU"), ("SPY", "EWG")],
    ]

    for combo in multi_combos:
        all_syms = set()
        for a, b in combo:
            all_syms.add(a)
            all_syms.add(b)
        if not all(s in all_data for s in all_syms):
            continue
        com = align([all_data[s] for s in all_syms], start_epoch)
        if len(com) < 200:
            continue

        label = "+".join(b for _, b in combo)
        n_pairs = len(combo)
        per_pair = CAPITAL / n_pairs

        # Build combined equity from individual results
        all_results = []
        total_tr = 0
        for pa, pb in combo:
            c_a = [all_data[pa][e] for e in com]
            c_b = [all_data[pb][e] for e in com]
            r, wu = sim_moc(com, c_a, c_b, capital=per_pair,
                            instrument=f"{pa} vs {pb}", exchange="US",
                            params_dict={"pair": f"{pa}/{pb}"})
            r.compute()
            all_results.append(r)
            total_tr += r.to_dict()["summary"].get("total_trades") or 0

        # Combine equity curves
        all_eq = [r.equity_curve for r in all_results]
        min_len = min(len(eq) for eq in all_eq)
        combined_result = BacktestResult(
            "alpha_variations", {"combo": label, "n_pairs": n_pairs},
            f"SPY vs {label}", "US", CAPITAL,
            slippage_bps=int(SLIPPAGE * 10000),
        )
        for j in range(min_len):
            epoch = all_eq[0][j][0]
            combined_val = sum(eq[j][1] for eq in all_eq)
            combined_result.add_equity_point(epoch, combined_val)
        combined_result.compute()
        cs = combined_result.to_dict()["summary"]

        # OOS
        te = next((j for j, e in enumerate(com) if e >= split_epoch), len(com))
        oos_s = None
        max_wu = 21
        if te > max_wu + 50 and te < len(com) - 50:
            oos_results = []
            for pa, pb in combo:
                c_a = [all_data[pa][e] for e in com[te:]]
                c_b = [all_data[pb][e] for e in com[te:]]
                r_o, wu_o = sim_moc(com[te:], c_a, c_b, capital=per_pair,
                                    instrument=f"{pa} vs {pb}", exchange="US",
                                    params_dict={"pair": f"{pa}/{pb}"})
                r_o.compute()
                oos_results.append(r_o)
            oos_eq = [r.equity_curve for r in oos_results]
            oos_min_len = min(len(eq) for eq in oos_eq)
            oos_combined = BacktestResult(
                "alpha_variations", {"combo": label, "n_pairs": n_pairs, "oos": True},
                f"SPY vs {label}", "US", CAPITAL,
                slippage_bps=int(SLIPPAGE * 10000),
            )
            for j in range(oos_min_len):
                epoch = oos_eq[0][j][0]
                combined_val = sum(eq[j][1] for eq in oos_eq)
                oos_combined.add_equity_point(epoch, combined_val)
            oos_combined.compute()
            oos_s = oos_combined.to_dict()["summary"]

        if cs:
            oos_str = f", OOS: {_fmt_summary(oos_s)}" if oos_s else ""
            print(f"\n  {label} ({n_pairs} pairs): {_fmt_summary(cs)}, {total_tr} trades{oos_str}")

            yearly = combined_result.to_dict().get("yearly_returns", [])
            if yearly:
                print(f"  {'Year':<6} {'Return':>9} {'MaxDD':>9}")
                print(f"  {'-'*28}")
                for y in yearly:
                    print(f"  {y['year']:<6} {y['return']*100:>+8.1f}% {y['mdd']*100:>8.1f}%")

    # SPY benchmark
    if "SPY" in all_data:
        spy_bh_result = BacktestResult(
            "spy_benchmark", {}, "SPY", "US", CAPITAL,
            slippage_bps=0,
        )
        for e in common:
            spy_bh_result.add_equity_point(e, all_data["SPY"][e] / all_data["SPY"][common[0]] * CAPITAL)
        spy_bh_result.compute()
        spy_s = spy_bh_result.to_dict()["summary"]
        if spy_s:
            print(f"\n  B&H SPY: {_fmt_summary(spy_s)}")

    # Save multi-sweep results
    multi.save("result.json", top_n=20)
    multi.print_leaderboard(top_n=10)


if __name__ == "__main__":
    main()

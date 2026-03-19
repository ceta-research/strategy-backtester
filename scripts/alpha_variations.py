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

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.metrics import compute_metrics as compute_full_metrics

CAPITAL = 10_000_000
SLIPPAGE = 0.0005


# ── Data + Helpers ───────────────────────────────────────────────────────────

def fetch_close(cr, symbol, start_epoch, end_epoch):
    warmup = start_epoch - 500 * 86400
    sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
              WHERE symbol = '{symbol}' AND dateEpoch >= {warmup}
                AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""
    for attempt in range(3):
        try:
            return {int(r["date_epoch"]): float(r["close"])
                    for r in cr.query(sql, timeout=180, limit=10000000,
                                      memory_mb=8192, threads=4)
                    if float(r.get("close") or 0) > 0}
        except:
            if attempt < 2:
                time.sleep(5)
            else:
                return {}


def align(datasets, start_epoch):
    common = sorted(set.intersection(*[set(d.keys()) for d in datasets]))
    return [e for e in common if e >= start_epoch]


def compute_z(values, lookback):
    z = [0.0] * len(values)
    for i in range(lookback, len(values)):
        w = values[i - lookback:i]
        m = sum(w) / len(w)
        v = sum((x - m) ** 2 for x in w) / len(w)
        s = math.sqrt(v) if v > 0 else 1e-9
        z[i] = (values[i] - m) / s
    return z


def compute_sma(values, period):
    sma = [0.0] * len(values)
    r = 0.0
    for i in range(len(values)):
        r += values[i]
        if i >= period:
            r -= values[i - period]
        sma[i] = r / min(i + 1, period)
    return sma


def compute_realized_vol(closes, window):
    vol = [0.0] * len(closes)
    for i in range(1, len(closes)):
        start = max(1, i - window + 1)
        rets = [math.log(closes[j] / closes[j - 1])
                for j in range(start, i + 1) if closes[j - 1] > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol[i] = math.sqrt(var) * math.sqrt(252)
    return vol


def us_charges(value, side):
    return calculate_charges("US", value, "EQUITY", "DELIVERY", side)


def stats(values, epochs_sub):
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
    sh = f" Sh={s['sharpe']:.2f}" if s.get('sharpe') else ""
    so = f" So={s['sortino']:.2f}" if s.get('sortino') else ""
    return f"CAGR={s['cagr']:>+6.1f}% MDD={s['mdd']:>6.1f}% Cal={s['calmar']:.2f}{sh}{so}"


# ── Unified Simulator ────────────────────────────────────────────────────────

def sim_moc(epochs, closes_a, closes_b, *,
            z_lookback=20, z_entry=1.0, z_exit=-0.5,
            # Trend filter
            trend_filter=False, trend_period=120,
            # Vol regime
            vol_filter=False, vol_window=20, vol_avg_window=60, vol_threshold=2.0,
            # Conviction sizing
            conviction=False,  # deeper z → more capital
            # Min hold
            min_hold_days=0,
            # Dual timeframe
            dual_z=False, z_long_lookback=60,
            capital=CAPITAL, slippage=SLIPPAGE):
    """MOC pairs sim with all optional layers."""
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

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx, entry_epoch)
    trades = 0
    total_costs = 0.0
    values = []

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
                ch = us_charges(sell_val, "SELL_SIDE")
                sl = sell_val * slippage
                cash += sell_val - ch - sl
                total_costs += ch + sl
                position = None

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

            # Conviction: deeper z → more capital
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
                            trades += 1

        if position:
            side, qty, _, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * cp)
        else:
            values.append(cash)

    return values, trades, total_costs, warmup


# ── Sweep Runner ─────────────────────────────────────────────────────────────

def run_sweep(epochs, ca, cb, configs, label, split_epoch=None):
    """Run all configs and print results."""
    results = []
    for i, (desc, kwargs) in enumerate(configs):
        vals, tr, costs, wu = sim_moc(epochs, ca, cb, **kwargs)
        ep = epochs[wu:]
        if len(vals) > len(ep):
            vals = vals[:len(ep)]
        s = stats(vals, ep)
        if s:
            results.append({"desc": desc, "trades": tr, "costs": costs,
                            "warmup": wu, **s, "kwargs": kwargs})

            # OOS if split_epoch provided
            if split_epoch:
                te = next((j for j, e in enumerate(epochs) if e >= split_epoch), len(epochs))
                if te > wu + 50 and te < len(epochs) - 50:
                    vals_oos, tr_oos, _, wu_oos = sim_moc(
                        epochs[te:], ca[te:], cb[te:], **kwargs)
                    ep_oos = epochs[te + wu_oos:]
                    s_oos = stats(vals_oos, ep_oos)
                    results[-1]["oos"] = s_oos
                    results[-1]["oos_trades"] = tr_oos

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*130}")
    print(f"  {label} — {len(results)} configs, sorted by Calmar")
    print(f"{'='*130}")
    print(f"  {'#':<3} {'Description':<42} {'CAGR':>7} {'MDD':>7} {'Cal':>5} "
          f"{'Shrp':>5} {'Sort':>5} {'Tr':>4} "
          f"{'OOS CAGR':>9} {'OOS Cal':>8}")
    print(f"  {'-'*110}")

    for i, r in enumerate(results[:25]):
        oos = r.get("oos")
        oos_cagr = f"{oos['cagr']:>+7.1f}%" if oos else "     N/A"
        oos_cal = f"{oos['calmar']:>7.2f}" if oos else "     N/A"
        print(f"  {i+1:<3} {r['desc']:<42} "
              f"{r['cagr']:>+6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>5.2f} "
              f"{r.get('sharpe') or 0:>5.2f} {r.get('sortino') or 0:>5.2f} {r['trades']:>4} "
              f"{oos_cagr} {oos_cal}")

    # Year-wise for best
    if results:
        best = results[0]
        print(f"\n  BEST: {best['desc']}")
        print(f"  {fmt(best)}, {best['trades']} trades, costs={best['costs']:,.0f}")
        yearly = best["yearly"]
        print(f"\n  {'Year':<6} {'Return':>9} {'MaxDD':>9}")
        print(f"  {'-'*28}")
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    return results


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

    run_sweep(common, ca, cb, configs_zscore,
              "VAR 1: Z-Score Parameters", split_epoch)

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

    run_sweep(common, ca, cb, configs_trend,
              "VAR 2: Trend Filter", split_epoch)

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

    run_sweep(common, ca, cb, configs_vol,
              "VAR 3: Volatility Regime", split_epoch)

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

    run_sweep(common, ca, cb, configs_conv,
              "VAR 4: Conviction Sizing", split_epoch)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 5: Minimum Hold Days
    # ══════════════════════════════════════════════════════════════════════════
    configs_hold = []
    for mh in [0, 3, 5, 10, 20]:
        for ze in [0.75, 1.0]:
            desc = f"z20 in={ze} min_hold={mh}d"
            configs_hold.append((desc, dict(
                z_lookback=20, z_entry=ze, z_exit=-0.5, min_hold_days=mh)))

    run_sweep(common, ca, cb, configs_hold,
              "VAR 5: Minimum Hold Days", split_epoch)

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

    run_sweep(common, ca, cb, configs_dual,
              "VAR 6: Dual Timeframe Z", split_epoch)

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

    run_sweep(common, ca, cb, configs_combined,
              "VAR 7: Combined Layers (SPY vs EWJ)", split_epoch)

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 8: Best config on other pairs
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
            vals, tr, costs, wu = sim_moc(com, c_a, c_b, **kwargs)
            ep = com[wu:]
            s = stats(vals, ep)
            # OOS
            te = next((j for j, e in enumerate(com) if e >= split_epoch), len(com))
            oos_s = None
            oos_tr = 0
            if te > wu + 50 and te < len(com) - 50:
                vals_oos, oos_tr, _, wu_oos = sim_moc(
                    com[te:], c_a[te:], c_b[te:], **kwargs)
                oos_s = stats(vals_oos, com[te + wu_oos:])

            if s:
                oos_cagr = f"{oos_s['cagr']:>+7.1f}%" if oos_s else "     N/A"
                oos_cal = f"{oos_s['calmar']:>7.2f}" if oos_s else "     N/A"
                print(f"  {plabel:<12} {desc:<28} "
                      f"{s['cagr']:>+6.1f}% {s['mdd']:>6.1f}% {s['calmar']:>5.2f} "
                      f"{s.get('sharpe') or 0:>5.2f} {tr:>4} "
                      f"{oos_cagr} {oos_cal}")

    # ══════════════════════════════════════════════════════════════════════════
    #  VARIATION 9: Multi-pair with best 3 pairs only
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

        all_vals = []
        total_tr = total_costs = 0
        for pa, pb in combo:
            c_a = [all_data[pa][e] for e in com]
            c_b = [all_data[pb][e] for e in com]
            vals, tr, costs, wu = sim_moc(com, c_a, c_b, capital=per_pair)
            all_vals.append(vals)
            total_tr += tr
            total_costs += costs

        min_len = min(len(v) for v in all_vals)
        combined = [sum(v[i] for v in all_vals) for i in range(min_len)]
        # Use max warmup
        max_wu = max(21, 21)  # z_lookback + 1
        ep = com[max_wu:]
        if len(combined) > len(ep):
            combined = combined[:len(ep)]
        s = stats(combined, ep)

        # OOS
        te = next((j for j, e in enumerate(com) if e >= split_epoch), len(com))
        oos_s = None
        if te > max_wu + 50 and te < len(com) - 50:
            oos_vals = []
            oos_tr = 0
            for pa, pb in combo:
                c_a = [all_data[pa][e] for e in com[te:]]
                c_b = [all_data[pb][e] for e in com[te:]]
                vals_o, tr_o, _, wu_o = sim_moc(com[te:], c_a, c_b, capital=per_pair)
                oos_vals.append(vals_o)
                oos_tr += tr_o
            min_len_o = min(len(v) for v in oos_vals)
            combined_o = [sum(v[i] for v in oos_vals) for i in range(min_len_o)]
            ep_o = com[te + max_wu:]
            if len(combined_o) > len(ep_o):
                combined_o = combined_o[:len(ep_o)]
            oos_s = stats(combined_o, ep_o)

        if s:
            oos_str = f", OOS: {fmt(oos_s)}" if oos_s else ""
            print(f"\n  {label} ({n_pairs} pairs): {fmt(s)}, {total_tr} trades{oos_str}")

            yearly = s["yearly"]
            print(f"  {'Year':<6} {'Return':>9} {'MaxDD':>9}")
            print(f"  {'-'*28}")
            for yr in sorted(yearly.keys()):
                y = yearly[yr]
                ret = (y["last"] - y["first"]) / y["first"] * 100
                dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
                print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    # SPY benchmark
    if "SPY" in all_data:
        spy_bh = [all_data["SPY"][e] / all_data["SPY"][common[0]] * CAPITAL
                  for e in common]
        s_spy = stats(spy_bh, common)
        if s_spy:
            print(f"\n  B&H SPY: {fmt(s_spy)}")


if __name__ == "__main__":
    main()

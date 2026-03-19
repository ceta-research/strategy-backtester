#!/usr/bin/env python3
"""Combined index alpha: pairs + momentum + dip-timing + multi-pair portfolio.

Goal: Push past 20% CAGR with controlled drawdown.

Strategies combined:
  1. Multi-pair portfolio (US vs India, US vs UK, US vs HK) — run 2-3 pairs
     simultaneously with capital allocation
  2. Momentum overlay — only take pair trades in direction of 12-month momentum
  3. Dip-timing — don't buy immediately on z-score breach; wait for RSI < threshold
  4. Crash safety — speed-of-decline pause
  5. Leveraged rotation — top-1 momentum with partial leverage via pair overlaps

Also tests finer parameter grids on the best pair (US vs India).
"""

import sys
import os
import math
import time
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.metrics import compute_metrics as compute_full_metrics


# ── Exchange mapping (symbol → exchange for charge calculation) ───────────────

SYMBOL_EXCHANGE = {
    "^GSPC": "US", "^NSEI": "NSE", "^BSESN": "BSE", "^FTSE": "LSE",
    "^HSI": "HKSE", "^GDAXI": "XETRA", "^N225": "JPX", "^BVSP": "BVMF",
    "SPY": "US", "QQQ": "US",
    "NIFTYBEES": "NSE", "BANKBEES": "NSE",
}


def get_exchange(symbol):
    return SYMBOL_EXCHANGE.get(symbol, "US")


def trade_charges(exchange, order_value, side="BUY_SIDE"):
    """Calculate realistic trade charges using the pipeline's charge model."""
    if order_value <= 0:
        return 0.0
    return calculate_charges(exchange, order_value, segment="EQUITY",
                             trade_type="DELIVERY", which_side=side)


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_data(cr, symbol, source, start_epoch, end_epoch):
    warmup_epoch = start_epoch - 400 * 86400
    if source == "nse":
        sql = f"""SELECT date_epoch, open, high, low, close, volume
                  FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}'
                    AND date_epoch >= {warmup_epoch} AND date_epoch <= {end_epoch}
                  ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, open, high, low, adjClose as close, volume
                  FROM fmp.stock_eod
                  WHERE symbol = '{symbol}'
                    AND dateEpoch >= {warmup_epoch} AND dateEpoch <= {end_epoch}
                  ORDER BY dateEpoch"""

    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            print(f"  Attempt {attempt+1} for {symbol}: {e}")
            if attempt < 2:
                time.sleep(5)
            else:
                return {}

    if not results:
        return {}

    data = {}
    for r in results:
        c = float(r.get("close") or 0)
        if c > 0:
            epoch = int(r["date_epoch"])
            data[epoch] = {
                "close": c,
                "open": float(r.get("open") or c),
                "high": float(r.get("high") or c),
                "low": float(r.get("low") or c),
                "volume": float(r.get("volume") or 0),
            }
    return data


def align_datasets(datasets, start_epoch):
    """Align multiple datasets by epoch. Return common epochs + close arrays."""
    epoch_sets = [set(d.keys()) for d in datasets]
    common = sorted(epoch_sets[0].intersection(*epoch_sets[1:]))
    common = [e for e in common if e >= start_epoch]
    return common


# ── Indicators ───────────────────────────────────────────────────────────────

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


def compute_momentum(closes, period):
    """N-day return percentage."""
    mom = [0.0] * len(closes)
    for i in range(period, len(closes)):
        if closes[i - period] > 0:
            mom[i] = (closes[i] - closes[i - period]) / closes[i - period] * 100
    return mom


def compute_z_scores(ratios, lookback):
    z = [0.0] * len(ratios)
    for i in range(lookback, len(ratios)):
        window = ratios[i - lookback:i]
        mean = sum(window) / len(window)
        var = sum((r - mean) ** 2 for r in window) / len(window)
        std = math.sqrt(var) if var > 0 else 1e-9
        z[i] = (ratios[i] - mean) / std
    return z


def compute_cum_return(closes, window):
    cr = [0.0] * len(closes)
    for i in range(window, len(closes)):
        if closes[i - window] > 0:
            cr[i] = (closes[i] - closes[i - window]) / closes[i - window] * 100
    return cr


# ── Part 1: Fine-tuned Single Pair ──────────────────────────────────────────

@dataclass
class PairConfig:
    lookback: int = 30
    z_entry: float = 1.0
    z_exit: float = -0.5
    max_hold_days: int = 0
    # Momentum overlay
    use_momentum: bool = False
    momentum_period: int = 252
    # Dip timing
    use_dip_timing: bool = False
    rsi_period: int = 14
    rsi_threshold: float = 40.0  # only buy when RSI < this
    # Crash safety
    use_crash_safety: bool = False
    crash_daily_pct: float = 3.0
    crash_pause_days: int = 5
    # Position sizing
    invest_pct: float = 1.0      # fraction of capital to invest (< 1 = partial)


def simulate_pair(epochs, data_a_list, data_b_list, cfg: PairConfig, capital=10_000_000,
                   sym_a="^GSPC", sym_b="^NSEI"):
    """Enhanced pair simulation with charges, integer quantities, and full tracking.

    Now includes:
    - Exchange-specific transaction charges (STT, brokerage, SEC fees, etc.)
    - Integer share quantities (no fractional shares)
    - Daily return tracking for Sharpe/Sortino computation
    """
    n = len(epochs)
    closes_a = [d["close"] for d in data_a_list]
    closes_b = [d["close"] for d in data_b_list]

    exchange_a = get_exchange(sym_a)
    exchange_b = get_exchange(sym_b)

    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z_scores(ratios, cfg.lookback)

    # Optional indicators
    rsi_a = compute_rsi(closes_a, cfg.rsi_period) if cfg.use_dip_timing else None
    rsi_b = compute_rsi(closes_b, cfg.rsi_period) if cfg.use_dip_timing else None
    mom_a = compute_momentum(closes_a, cfg.momentum_period) if cfg.use_momentum else None
    mom_b = compute_momentum(closes_b, cfg.momentum_period) if cfg.use_momentum else None
    cum_1d_a = compute_cum_return(closes_a, 1) if cfg.use_crash_safety else None
    cum_1d_b = compute_cum_return(closes_b, 1) if cfg.use_crash_safety else None

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    trades_a = trades_b = 0
    total_charges = 0.0
    crash_pause_until = -1
    values = []

    start = max(cfg.lookback, cfg.momentum_period if cfg.use_momentum else 0)

    for i in range(start, n):
        z = z_scores[i]

        # Crash safety: pause on big daily drops
        if cfg.use_crash_safety:
            if cum_1d_a[i] < -cfg.crash_daily_pct or cum_1d_b[i] < -cfg.crash_daily_pct:
                crash_pause_until = max(crash_pause_until, i + cfg.crash_pause_days)

        # Exit logic
        if position:
            side, qty, entry_price, entry_idx = position
            curr_price = closes_a[i] if side == "a" else closes_b[i]

            should_exit = False
            if side == "a" and z >= cfg.z_exit:
                should_exit = True
            elif side == "b" and z <= -cfg.z_exit:
                should_exit = True
            if cfg.max_hold_days > 0:
                hold = (epochs[i] - epochs[entry_idx]) / 86400
                if hold >= cfg.max_hold_days:
                    should_exit = True

            if should_exit:
                sell_value = qty * curr_price
                exch = exchange_a if side == "a" else exchange_b
                sell_charges = trade_charges(exch, sell_value, "SELL_SIDE")
                cash += sell_value - sell_charges
                total_charges += sell_charges
                position = None

        # Entry logic
        if position is None and i > crash_pause_until:
            buy_side = None
            if z < -cfg.z_entry:
                buy_side = "a"  # A is underperforming
            elif z > cfg.z_entry:
                buy_side = "b"  # B is underperforming

            if buy_side:
                # Momentum filter: only buy if 12-month momentum is positive
                if cfg.use_momentum:
                    mom = mom_a[i] if buy_side == "a" else mom_b[i]
                    if mom < 0:
                        buy_side = None

                # Dip timing: only buy if RSI is low (oversold)
                if buy_side and cfg.use_dip_timing:
                    rsi = rsi_a[i] if buy_side == "a" else rsi_b[i]
                    if rsi > cfg.rsi_threshold:
                        buy_side = None

            if buy_side:
                buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
                invest = cash * cfg.invest_pct
                exch = exchange_a if buy_side == "a" else exchange_b
                if buy_price > 0 and invest > 0:
                    # Integer quantity (realistic)
                    qty = int(invest / buy_price)
                    if qty <= 0:
                        qty = 1  # minimum 1 share for index ETFs
                    actual_cost = qty * buy_price
                    buy_charges = trade_charges(exch, actual_cost, "BUY_SIDE")

                    if actual_cost + buy_charges <= cash:
                        position = (buy_side, qty, buy_price, i)
                        cash -= actual_cost + buy_charges
                        total_charges += buy_charges
                        if buy_side == "a":
                            trades_a += 1
                        else:
                            trades_b += 1

        # Portfolio value
        if position:
            side, qty, _, _ = position
            curr = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * curr)
        else:
            values.append(cash)

    return values, trades_a, trades_b, total_charges


def compute_stats(values, epochs_subset):
    """Compute full metrics: CAGR, MaxDD, Calmar, Sharpe, Sortino, VaR 95%."""
    if len(values) < 2 or len(epochs_subset) < 2:
        return None
    sv, ev = values[0], values[-1]
    years = (epochs_subset[-1] - epochs_subset[0]) / (365.25 * 86400)
    if years <= 0 or ev <= 0 or sv <= 0:
        return None
    tr = ev / sv
    cagr = (tr ** (1 / years) - 1) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    calmar = cagr / abs(max_dd) if max_dd != 0 else 0

    # Daily returns for Sharpe/Sortino/VaR
    daily_returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            daily_returns.append((values[i] - values[i - 1]) / values[i - 1])
        else:
            daily_returns.append(0.0)

    sharpe = sortino = var_95 = vol = None
    if len(daily_returns) >= 20:
        benchmark_returns = [0.0] * len(daily_returns)
        full = compute_full_metrics(daily_returns, benchmark_returns, periods_per_year=252)
        port = full["portfolio"]
        sharpe = port.get("sharpe_ratio")
        sortino = port.get("sortino_ratio")
        var_95 = port.get("var_95")
        vol = port.get("annualized_volatility")

    yearly = {}
    for j, v in enumerate(values):
        yr = datetime.fromtimestamp(epochs_subset[j], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v, "peak": v, "trough": v}
        yearly[yr]["last"] = v
        yearly[yr]["peak"] = max(yearly[yr]["peak"], v)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], v)

    return {"cagr": cagr, "max_dd": max_dd, "calmar": calmar, "total_return": tr,
            "years": years, "yearly": yearly,
            "sharpe": sharpe, "sortino": sortino, "var_95": var_95, "vol": vol}


def run_fine_tuned_pair(epochs, data_a_list, data_b_list, label, sym_a="^GSPC", sym_b="^NSEI"):
    """Fine-grained sweep on a single pair with realistic charges."""
    configs = []

    for lb in [20, 30, 45, 60, 90]:
        for z_in in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
            for z_out in [-1.0, -0.5, 0.0, 0.5]:
                for max_hold in [0, 40, 60, 90]:
                    for use_mom in [False, True]:
                        for use_dip in [False, True]:
                            for use_crash in [False, True]:
                                for invest in [0.8, 1.0]:
                                    configs.append(PairConfig(
                                        lookback=lb, z_entry=z_in, z_exit=z_out,
                                        max_hold_days=max_hold,
                                        use_momentum=use_mom,
                                        use_dip_timing=use_dip,
                                        rsi_threshold=40.0,
                                        use_crash_safety=use_crash,
                                        invest_pct=invest,
                                    ))

    print(f"\n  {label}: Sweeping {len(configs)} configs (with charges)...")
    results = []

    for i, cfg in enumerate(configs):
        start = max(cfg.lookback, cfg.momentum_period if cfg.use_momentum else 0)
        vals, ta, tb, charges = simulate_pair(epochs, data_a_list, data_b_list, cfg,
                                               sym_a=sym_a, sym_b=sym_b)
        ep_sub = epochs[start:]
        stats = compute_stats(vals, ep_sub)
        if stats and abs(stats["max_dd"]) > 0.01:
            results.append({"cfg": cfg, "ta": ta, "tb": tb, "charges": charges, **stats})

        if (i + 1) % 5000 == 0:
            print(f"    {i+1}/{len(configs)} done...")

    results.sort(key=lambda r: r["calmar"], reverse=True)

    # Print top 20
    print(f"\n{'='*150}")
    print(f"  {label} — TOP 20 by Calmar")
    print(f"{'='*150}")
    print(f"  {'#':<3} {'LB':>3} {'Z_in':>5} {'Z_out':>6} {'MaxH':>5} "
          f"{'Mom':>3} {'Dip':>3} {'Crsh':>4} {'Inv':>4} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Tr':>5}")
    print(f"  {'-'*100}")

    for i, r in enumerate(results[:20]):
        c = r["cfg"]
        print(f"  {i+1:<3} {c.lookback:>3} {c.z_entry:>5.2f} {c.z_exit:>6.1f} "
              f"{c.max_hold_days:>5} "
              f"{'Y' if c.use_momentum else 'N':>3} "
              f"{'Y' if c.use_dip_timing else 'N':>3} "
              f"{'Y' if c.use_crash_safety else 'N':>4} "
              f"{c.invest_pct:>4.1f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x {r['ta']+r['tb']:>5}")

    # Print top 10 by CAGR (separate list)
    by_cagr = sorted(results, key=lambda r: r["cagr"], reverse=True)
    print(f"\n  TOP 10 by CAGR (absolute returns):")
    print(f"  {'#':<3} {'LB':>3} {'Z_in':>5} {'Z_out':>6} {'MaxH':>5} "
          f"{'Mom':>3} {'Dip':>3} {'Crsh':>4} {'Inv':>4} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6}")
    print(f"  {'-'*85}")
    for i, r in enumerate(by_cagr[:10]):
        c = r["cfg"]
        print(f"  {i+1:<3} {c.lookback:>3} {c.z_entry:>5.2f} {c.z_exit:>6.1f} "
              f"{c.max_hold_days:>5} "
              f"{'Y' if c.use_momentum else 'N':>3} "
              f"{'Y' if c.use_dip_timing else 'N':>3} "
              f"{'Y' if c.use_crash_safety else 'N':>4} "
              f"{c.invest_pct:>4.1f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x")

    # Configs > 20% CAGR
    above_20 = [r for r in results if r["cagr"] >= 20.0]
    if above_20:
        above_20.sort(key=lambda r: r["calmar"], reverse=True)
        print(f"\n  ** {len(above_20)} configs with CAGR >= 20% **")
        b = above_20[0]
        print(f"  Best by Calmar: CAGR={b['cagr']:.1f}%, DD={b['max_dd']:.1f}%, "
              f"Calmar={b['calmar']:.2f}, "
              f"Sharpe={b.get('sharpe', 0) or 0:.2f}, Sortino={b.get('sortino', 0) or 0:.2f}")
        c = b["cfg"]
        print(f"  Config: lb={c.lookback}, z_in={c.z_entry}, z_out={c.z_exit}, "
              f"max_hold={c.max_hold_days}, mom={'Y' if c.use_momentum else 'N'}, "
              f"dip={'Y' if c.use_dip_timing else 'N'}, crash={'Y' if c.use_crash_safety else 'N'}")
        if b.get("charges"):
            print(f"  Total charges: {b['charges']:,.0f} ({b['charges'] / 10_000_000 * 100:.2f}% of capital)")

    # Year-wise for best Calmar
    if results:
        best = results[0]
        sharpe_str = f", Sharpe={best.get('sharpe', 0) or 0:.2f}" if best.get('sharpe') else ""
        sortino_str = f", Sortino={best.get('sortino', 0) or 0:.2f}" if best.get('sortino') else ""
        print(f"\n  BEST (Calmar): CAGR={best['cagr']:.1f}%, DD={best['max_dd']:.1f}%, "
              f"Calmar={best['calmar']:.2f}{sharpe_str}{sortino_str}")
        yearly = best["yearly"]
        print(f"  {'Year':<6} {'Return':>9} {'Max DD':>9}")
        print(f"  {'-'*28}")
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    return results


# ── Part 2: Multi-Pair Portfolio ─────────────────────────────────────────────

def simulate_multi_pair(pair_data, common_epochs, cfg: PairConfig, n_pairs, capital=10_000_000):
    """Run multiple pairs simultaneously with equal capital allocation.

    pair_data: list of (closes_a, closes_b, label, sym_a, sym_b) for each pair
    """
    n = len(common_epochs)
    n_active = len(pair_data)
    per_pair_capital = capital / n_active

    # Run each pair independently
    pair_values = []
    total_trades = 0
    total_charges = 0.0
    for closes_a_list, closes_b_list, label, sym_a, sym_b in pair_data:
        vals, ta, tb, charges = simulate_pair(
            common_epochs, closes_a_list, closes_b_list, cfg,
            capital=per_pair_capital, sym_a=sym_a, sym_b=sym_b
        )
        pair_values.append(vals)
        total_trades += ta + tb
        total_charges += charges

    # Combine: sum of all pair portfolio values
    start = max(cfg.lookback, cfg.momentum_period if cfg.use_momentum else 0)
    combined = []
    min_len = min(len(pv) for pv in pair_values)
    for i in range(min_len):
        combined.append(sum(pv[i] for pv in pair_values))

    return combined, total_trades


def run_multi_pair_sweep(pair_data, common_epochs, label):
    """Sweep configs across all pairs simultaneously."""
    configs = []

    for lb in [20, 30, 45, 60]:
        for z_in in [0.5, 0.75, 1.0, 1.5]:
            for z_out in [-0.5, 0.0, 0.5]:
                for max_hold in [0, 60]:
                    for use_mom in [False, True]:
                        for use_crash in [False, True]:
                            configs.append(PairConfig(
                                lookback=lb, z_entry=z_in, z_exit=z_out,
                                max_hold_days=max_hold,
                                use_momentum=use_mom,
                                use_crash_safety=use_crash,
                            ))

    print(f"\n  Multi-pair ({label}): Sweeping {len(configs)} configs...")
    results = []

    for i, cfg in enumerate(configs):
        start = max(cfg.lookback, cfg.momentum_period if cfg.use_momentum else 0)
        combined, total_trades = simulate_multi_pair(pair_data, common_epochs, cfg, len(pair_data))
        ep_sub = common_epochs[start:]
        if len(combined) > len(ep_sub):
            combined = combined[:len(ep_sub)]
        stats = compute_stats(combined, ep_sub)
        if stats and abs(stats["max_dd"]) > 0.01:
            results.append({"cfg": cfg, "trades": total_trades, **stats})

        if (i + 1) % 500 == 0:
            print(f"    {i+1}/{len(configs)} done...")

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*130}")
    print(f"  MULTI-PAIR {label} — TOP 15 by Calmar")
    print(f"{'='*130}")
    print(f"  {'#':<3} {'LB':>3} {'Z_in':>5} {'Z_out':>6} {'MaxH':>5} "
          f"{'Mom':>3} {'Crsh':>4} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Tr':>5}")
    print(f"  {'-'*75}")

    for i, r in enumerate(results[:15]):
        c = r["cfg"]
        print(f"  {i+1:<3} {c.lookback:>3} {c.z_entry:>5.2f} {c.z_exit:>6.1f} "
              f"{c.max_hold_days:>5} "
              f"{'Y' if c.use_momentum else 'N':>3} "
              f"{'Y' if c.use_crash_safety else 'N':>4} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x {r['trades']:>5}")

    above_20 = [r for r in results if r["cagr"] >= 20.0]
    if above_20:
        above_20.sort(key=lambda r: r["calmar"], reverse=True)
        print(f"\n  ** {len(above_20)} multi-pair configs with CAGR >= 20% **")
        best = above_20[0]
        c = best["cfg"]
        print(f"  Best: CAGR={best['cagr']:.1f}%, DD={best['max_dd']:.1f}%, "
              f"Calmar={best['calmar']:.2f}")
        print(f"  Config: lb={c.lookback}, z_in={c.z_entry}, z_out={c.z_exit}")

    # Year-wise for best
    if results:
        best = results[0]
        yearly = best["yearly"]
        print(f"\n  BEST: CAGR={best['cagr']:.1f}%, DD={best['max_dd']:.1f}%, "
              f"Calmar={best['calmar']:.2f}")
        print(f"  {'Year':<6} {'Return':>9} {'Max DD':>9}")
        print(f"  {'-'*28}")
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    return results


# ── Part 3: Momentum Rotation + Pair Timing ──────────────────────────────────

def simulate_rotation_with_pairs(all_close_data, symbols, labels, common_epochs,
                                  mom_lookback=252, top_k=1, rebal_days=63,
                                  use_z_timing=False, z_lookback=30, z_threshold=1.0,
                                  abs_momentum=False):
    """Enhanced rotation: rank by momentum, optionally use z-score timing for entry."""
    n = len(common_epochs)
    n_sym = len(symbols)

    # Build close matrix
    close_matrix = []
    for sym in symbols:
        close_matrix.append([all_close_data[sym][e]["close"]
                             if e in all_close_data[sym] else 0
                             for e in common_epochs])

    # Precompute z-scores between all pairs (for timing)
    z_matrix = {}
    if use_z_timing:
        for s1 in range(n_sym):
            for s2 in range(s1 + 1, n_sym):
                ratios = [close_matrix[s1][i] / close_matrix[s2][i]
                          if close_matrix[s2][i] > 0 else 1.0
                          for i in range(n)]
                z_matrix[(s1, s2)] = compute_z_scores(ratios, z_lookback)

    cash = 10_000_000
    holdings = {}
    last_rebal = -999
    values = []

    for i in range(mom_lookback, n):
        pos_val = sum(holdings.get(s, 0) * close_matrix[s][i] for s in range(n_sym))
        total_val = cash + pos_val

        if i - last_rebal >= rebal_days:
            # Compute momentum
            scores = []
            for s in range(n_sym):
                prev = close_matrix[s][i - mom_lookback]
                curr = close_matrix[s][i]
                if prev > 0 and curr > 0:
                    mom = (curr - prev) / prev * 100
                    scores.append((s, mom))

            scores.sort(key=lambda x: x[1], reverse=True)

            selected = []
            for s, mom in scores:
                if len(selected) >= top_k:
                    break
                if abs_momentum and mom <= 0:
                    continue

                # Z-score timing: check if selected symbol is relatively cheap
                if use_z_timing and len(scores) > 1:
                    # Check z-score vs the worst performer
                    worst_s = scores[-1][0]
                    pair_key = (min(s, worst_s), max(s, worst_s))
                    if pair_key in z_matrix:
                        z = z_matrix[pair_key][i]
                        # If s is the first in pair and z is very high,
                        # s is expensive relative to worst — skip
                        if s == pair_key[0] and z > z_threshold:
                            continue
                        if s == pair_key[1] and z < -z_threshold:
                            continue

                selected.append(s)

            # Rebalance
            for s, qty in holdings.items():
                cash += qty * close_matrix[s][i]
            holdings = {}

            if selected:
                per_sym = total_val / len(selected)
                for s in selected:
                    price = close_matrix[s][i]
                    if price > 0:
                        holdings[s] = per_sym / price
                        cash -= per_sym

            last_rebal = i

        pos_val = sum(holdings.get(s, 0) * close_matrix[s][i] for s in range(n_sym))
        values.append(cash + pos_val)

    return values


def run_rotation_sweep(all_close_data, symbols, labels, common_epochs):
    """Sweep enhanced rotation configs."""
    configs = []
    for mom in [126, 252]:
        for top_k in [1, 2]:
            for rebal in [21, 42, 63]:
                for abs_mom in [False, True]:
                    for use_z in [False, True]:
                        for z_thresh in [0.5, 1.0, 1.5] if use_z else [0]:
                            configs.append({
                                "mom": mom, "top_k": top_k, "rebal": rebal,
                                "abs_mom": abs_mom, "use_z": use_z,
                                "z_thresh": z_thresh,
                            })

    print(f"\n  Rotation sweep: {len(configs)} configs...")
    results = []

    for cfg in configs:
        vals = simulate_rotation_with_pairs(
            all_close_data, symbols, labels, common_epochs,
            mom_lookback=cfg["mom"], top_k=cfg["top_k"],
            rebal_days=cfg["rebal"], abs_momentum=cfg["abs_mom"],
            use_z_timing=cfg["use_z"], z_threshold=cfg["z_thresh"],
        )
        ep_sub = common_epochs[cfg["mom"]:]
        stats = compute_stats(vals, ep_sub)
        if stats and abs(stats["max_dd"]) > 0.01:
            results.append({"cfg": cfg, **stats})

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*120}")
    print(f"  ENHANCED ROTATION — TOP 15 by Calmar")
    print(f"{'='*120}")
    print(f"  {'#':<3} {'Mom':>4} {'TopK':>5} {'Reb':>4} {'Abs':>4} {'Z':>3} {'Zthr':>5} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6}")
    print(f"  {'-'*70}")

    for i, r in enumerate(results[:15]):
        c = r["cfg"]
        print(f"  {i+1:<3} {c['mom']:>4} {c['top_k']:>5} {c['rebal']:>4} "
              f"{'Y' if c['abs_mom'] else 'N':>4} "
              f"{'Y' if c['use_z'] else 'N':>3} "
              f"{c['z_thresh']:>5.1f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x")

    above_20 = [r for r in results if r["cagr"] >= 20.0]
    if above_20:
        above_20.sort(key=lambda r: r["calmar"], reverse=True)
        print(f"\n  ** {len(above_20)} rotation configs with CAGR >= 20% **")

    # Year-wise for best
    if results:
        best = results[0]
        yearly = best["yearly"]
        c = best["cfg"]
        print(f"\n  BEST: mom={c['mom']}, top_k={c['top_k']}, rebal={c['rebal']}, "
              f"abs_mom={'Y' if c['abs_mom'] else 'N'}, z={'Y' if c['use_z'] else 'N'}")
        print(f"  CAGR={best['cagr']:.1f}%, DD={best['max_dd']:.1f}%, Calmar={best['calmar']:.2f}")
        print(f"  {'Year':<6} {'Return':>9} {'Max DD':>9}")
        print(f"  {'-'*28}")
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

FETCH_LIST = [
    ("^GSPC", "S&P 500", "fmp"),
    ("^NSEI", "NIFTY 50", "fmp"),
    ("^FTSE", "FTSE 100", "fmp"),
    ("^HSI", "Hang Seng", "fmp"),
    ("^GDAXI", "DAX", "fmp"),
    ("^N225", "Nikkei", "fmp"),
    ("^BVSP", "Bovespa", "fmp"),
    ("SPY", "SPY", "fmp"),
    ("QQQ", "QQQ", "fmp"),
    ("NIFTYBEES", "NIFTYBEES", "nse"),
    ("BANKBEES", "BANKBEES", "nse"),
]


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    # Fetch all data
    all_data = {}
    for sym, label, source in FETCH_LIST:
        print(f"  Fetching {label} ({sym})...")
        data = fetch_data(cr, sym, source, start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            print(f"    {len(data)} days")
        else:
            print(f"    FAILED")

    # ── Part 1: Fine-tuned single pair — US vs India ──
    print("\n" + "=" * 80)
    print("  PART 1: FINE-TUNED SINGLE PAIR (US vs India)")
    print("=" * 80)

    for sym_a, sym_b, label in [
        ("^GSPC", "^NSEI", "US vs India"),
        ("^GSPC", "^FTSE", "US vs UK"),
        ("^GSPC", "^HSI", "US vs HK"),
    ]:
        if sym_a not in all_data or sym_b not in all_data:
            print(f"  Missing data for {label}")
            continue

        common = align_datasets([all_data[sym_a], all_data[sym_b]], start_epoch)
        if len(common) < 300:
            print(f"  Insufficient overlap for {label}")
            continue

        data_a_list = [all_data[sym_a][e] for e in common]
        data_b_list = [all_data[sym_b][e] for e in common]
        print(f"  {label}: {len(common)} common days")

        run_fine_tuned_pair(common, data_a_list, data_b_list, label, sym_a=sym_a, sym_b=sym_b)

    # ── Part 2: Multi-pair portfolio ──
    print("\n" + "=" * 80)
    print("  PART 2: MULTI-PAIR PORTFOLIO")
    print("=" * 80)

    # Best pairs from initial run
    pair_combos = [
        [("^GSPC", "^NSEI"), ("^GSPC", "^FTSE")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^FTSE"), ("^GSPC", "^HSI")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^FTSE"), ("^GSPC", "^GDAXI")],
    ]

    for pairs in pair_combos:
        # Check data availability
        all_syms = set()
        for a, b in pairs:
            all_syms.add(a)
            all_syms.add(b)
        if not all(s in all_data for s in all_syms):
            continue

        # Find common epochs across all symbols in this combo
        datasets = [all_data[s] for s in all_syms]
        common = align_datasets(datasets, start_epoch)
        if len(common) < 300:
            continue

        pair_label = " + ".join(f"{a.replace('^','')}v{b.replace('^','')}" for a, b in pairs)

        pair_data = []
        for sym_a, sym_b in pairs:
            data_a = [all_data[sym_a][e] for e in common]
            data_b = [all_data[sym_b][e] for e in common]
            pair_data.append((data_a, data_b, f"{sym_a} vs {sym_b}", sym_a, sym_b))

        run_multi_pair_sweep(pair_data, common, pair_label)

    # ── Part 3: Enhanced Rotation ──
    print("\n" + "=" * 80)
    print("  PART 3: ENHANCED ROTATION (momentum + z-score timing)")
    print("=" * 80)

    rot_symbols = ["^GSPC", "^NSEI", "^FTSE", "^HSI", "^GDAXI", "^N225", "SPY", "QQQ"]
    rot_symbols = [s for s in rot_symbols if s in all_data]
    rot_labels = {s: next(l for sy, l, _ in FETCH_LIST if sy == s) for s in rot_symbols}

    if len(rot_symbols) >= 4:
        datasets = [all_data[s] for s in rot_symbols]
        common = align_datasets(datasets, start_epoch)
        if len(common) >= 300:
            print(f"  {len(rot_symbols)} symbols, {len(common)} common days")
            run_rotation_sweep(
                all_data, rot_symbols,
                [rot_labels[s] for s in rot_symbols],
                common
            )


if __name__ == "__main__":
    main()

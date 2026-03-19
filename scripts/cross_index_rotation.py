#!/usr/bin/env python3
"""Cross-exchange index relative value / rotation strategy.

Two modes:
  1. PAIRS: Track ratio of two indices. Buy the underperformer when z-score
     deviates beyond threshold. Long-only (no shorting).
  2. ROTATION: Rank N indices by relative momentum. Hold top K.
     Rebalance at fixed intervals.

Data: FMP stock_eod (global indices + ETFs), NSE charting (India ETFs).
"""

import sys
import os
import math
import time
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch


# ── Symbols ──────────────────────────────────────────────────────────────────

PAIRS = [
    # (sym_a, sym_b, label, source_a, source_b)
    ("NIFTYBEES", "BANKBEES", "NIFTY vs BANK", "nse", "nse"),
    ("SPY", "QQQ", "SPY vs QQQ", "fmp", "fmp"),
    ("^GSPC", "^NSEI", "US vs India", "fmp", "fmp"),
    ("^GSPC", "^HSI", "US vs HK", "fmp", "fmp"),
    ("^NSEI", "^HSI", "India vs HK", "fmp", "fmp"),
    ("^GSPC", "^GDAXI", "US vs Germany", "fmp", "fmp"),
    ("^GSPC", "^FTSE", "US vs UK", "fmp", "fmp"),
    ("^NSEI", "^BSESN", "NIFTY vs Sensex", "fmp", "fmp"),
]

ROTATION_POOL = [
    # (symbol, label, source)
    ("^GSPC", "S&P 500", "fmp"),
    ("^NSEI", "NIFTY 50", "fmp"),
    ("^FTSE", "FTSE 100", "fmp"),
    ("^N225", "Nikkei", "fmp"),
    ("^HSI", "Hang Seng", "fmp"),
    ("^GDAXI", "DAX", "fmp"),
    ("^BVSP", "Bovespa", "fmp"),
    ("SPY", "SPY", "fmp"),
    ("QQQ", "QQQ", "fmp"),
]


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_data(cr, symbol, source, start_epoch, end_epoch):
    """Fetch OHLCV, return sorted list of {epoch, close}."""
    warmup_epoch = start_epoch - 400 * 86400

    if source == "nse":
        sql = f"""SELECT date_epoch, close
                  FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}'
                    AND date_epoch >= {warmup_epoch} AND date_epoch <= {end_epoch}
                  ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, adjClose as close
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
            data[int(r["date_epoch"])] = c

    return data


def align_pair(data_a, data_b, start_epoch):
    """Align two datasets by epoch. Return matched lists."""
    common_epochs = sorted(set(data_a.keys()) & set(data_b.keys()))
    common_epochs = [e for e in common_epochs if e >= start_epoch]

    if len(common_epochs) < 100:
        return [], [], []

    epochs = common_epochs
    closes_a = [data_a[e] for e in epochs]
    closes_b = [data_b[e] for e in epochs]
    return epochs, closes_a, closes_b


# ── Pairs Strategy ───────────────────────────────────────────────────────────

@dataclass
class PairsConfig:
    lookback: int = 60           # rolling window for z-score
    z_entry: float = 2.0         # buy underperformer when z < -threshold
    z_exit: float = 0.0          # exit when z returns to this level
    hold_a_or_b: str = "under"   # "under" = buy underperformer, "a" = always buy A
    max_hold_days: int = 0       # 0 = unlimited
    start_capital: float = 10_000_000


def compute_z_scores(closes_a, closes_b, lookback):
    """Compute z-score of ratio A/B vs rolling mean/std."""
    n = len(closes_a)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]

    z_scores = [0.0] * n
    for i in range(lookback, n):
        window = ratios[i - lookback:i]
        mean = sum(window) / len(window)
        var = sum((r - mean) ** 2 for r in window) / len(window)
        std = math.sqrt(var) if var > 0 else 1e-9
        z_scores[i] = (ratios[i] - mean) / std

    return ratios, z_scores


def simulate_pairs(epochs, closes_a, closes_b, cfg: PairsConfig):
    """Long-only pairs: buy the underperformer when spread diverges.

    Returns (values, trades_a, trades_b).
    """
    n = len(epochs)
    ratios, z_scores = compute_z_scores(closes_a, closes_b, cfg.lookback)

    cash = cfg.start_capital
    position = None  # ("a" or "b", qty, entry_price, entry_idx)
    trades_a = 0
    trades_b = 0
    values = []

    for i in range(cfg.lookback, n):
        z = z_scores[i]

        # Current position value
        if position:
            side, qty, entry_price, entry_idx = position
            curr_price = closes_a[i] if side == "a" else closes_b[i]
            pos_val = qty * curr_price

            # Exit conditions
            should_exit = False

            # Z-score reverted
            if side == "a" and z >= cfg.z_exit:
                should_exit = True
            elif side == "b" and z <= -cfg.z_exit:
                should_exit = True

            # Max hold
            if cfg.max_hold_days > 0:
                hold_days = (epochs[i] - epochs[entry_idx]) / 86400
                if hold_days >= cfg.max_hold_days:
                    should_exit = True

            if should_exit:
                cash += pos_val
                position = None
        else:
            pos_val = 0

        # Entry: no position, z-score diverged
        if position is None:
            if z < -cfg.z_entry:
                # A is underperforming — buy A
                buy_price = closes_a[i]
                if buy_price > 0:
                    qty = cash / buy_price
                    position = ("a", qty, buy_price, i)
                    cash = 0
                    trades_a += 1
            elif z > cfg.z_entry:
                # B is underperforming — buy B
                buy_price = closes_b[i]
                if buy_price > 0:
                    qty = cash / buy_price
                    position = ("b", qty, buy_price, i)
                    cash = 0
                    trades_b += 1

        # Portfolio value
        if position:
            side, qty, _, _ = position
            curr_price = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * curr_price)
        else:
            values.append(cash)

    return values, trades_a, trades_b


def compute_stats_simple(values, epochs_subset):
    """Compute CAGR, MaxDD, Calmar from aligned values and epochs."""
    if len(values) < 2 or len(epochs_subset) < 2:
        return None
    start_val, end_val = values[0], values[-1]
    years = (epochs_subset[-1] - epochs_subset[0]) / (365.25 * 86400)
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
        yr = datetime.fromtimestamp(epochs_subset[j], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v, "peak": v, "trough": v}
        yearly[yr]["last"] = v
        yearly[yr]["peak"] = max(yearly[yr]["peak"], v)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], v)

    return {
        "cagr": cagr, "max_dd": max_dd, "calmar": calmar,
        "total_return": total_return, "years": years, "yearly": yearly,
    }


def run_pairs_sweep(epochs, closes_a, closes_b, label):
    """Sweep pairs configs and print results."""
    results = []

    for lookback in [30, 60, 90, 120]:
        for z_entry in [1.0, 1.5, 2.0, 2.5]:
            for z_exit in [-0.5, 0.0, 0.5]:
                for max_hold in [0, 60, 120]:
                    cfg = PairsConfig(
                        lookback=lookback, z_entry=z_entry, z_exit=z_exit,
                        max_hold_days=max_hold,
                    )

                    vals, ta, tb = simulate_pairs(epochs, closes_a, closes_b, cfg)
                    epochs_subset = epochs[cfg.lookback:]
                    stats = compute_stats_simple(vals, epochs_subset)

                    if stats and abs(stats["max_dd"]) > 0.01:
                        results.append({
                            "cfg": cfg, "trades_a": ta, "trades_b": tb, **stats,
                        })

    results.sort(key=lambda r: r["calmar"], reverse=True)

    # Buy-and-hold baselines for comparison
    bh_a_vals = [closes_a[i] / closes_a[0] * 10_000_000 for i in range(len(closes_a))]
    bh_b_vals = [closes_b[i] / closes_b[0] * 10_000_000 for i in range(len(closes_b))]
    stats_bh_a = compute_stats_simple(bh_a_vals, epochs)
    stats_bh_b = compute_stats_simple(bh_b_vals, epochs)

    print(f"\n{'='*130}")
    print(f"  PAIRS: {label} — TOP 15 by Calmar")
    print(f"{'='*130}")

    if stats_bh_a:
        print(f"  BH(A): CAGR={stats_bh_a['cagr']:>+6.1f}%, MaxDD={stats_bh_a['max_dd']:>6.1f}%, "
              f"Calmar={stats_bh_a['calmar']:.2f}")
    if stats_bh_b:
        print(f"  BH(B): CAGR={stats_bh_b['cagr']:>+6.1f}%, MaxDD={stats_bh_b['max_dd']:>6.1f}%, "
              f"Calmar={stats_bh_b['calmar']:.2f}")

    print(f"\n  {'#':<3} {'LB':>4} {'Z_in':>5} {'Z_out':>6} {'MaxH':>5} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Tr_A':>5} {'Tr_B':>5}")
    print(f"  {'-'*80}")

    for i, r in enumerate(results[:15]):
        c = r["cfg"]
        print(f"  {i+1:<3} {c.lookback:>4} {c.z_entry:>5.1f} {c.z_exit:>6.1f} "
              f"{c.max_hold_days:>5} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x {r['trades_a']:>5} {r['trades_b']:>5}")

    return results


# ── Rotation Strategy ────────────────────────────────────────────────────────

@dataclass
class RotationConfig:
    momentum_lookback: int = 60      # N-day return for ranking
    top_k: int = 3                   # hold top K indices
    rebalance_days: int = 21         # rebalance every N trading days
    use_absolute_momentum: bool = True  # only hold if return > 0
    start_capital: float = 10_000_000


def simulate_rotation(all_data, common_epochs, symbols, labels, cfg: RotationConfig):
    """Rotation: rank indices by momentum, hold top K.

    all_data: dict of symbol -> {epoch: close}
    common_epochs: sorted list of epochs present in ALL symbols
    """
    n = len(common_epochs)
    if n < cfg.momentum_lookback + 10:
        return [], {}

    # Build close matrix: [symbol_idx][time_idx]
    n_sym = len(symbols)
    close_matrix = []
    for sym in symbols:
        close_matrix.append([all_data[sym].get(e, 0) for e in common_epochs])

    cash = cfg.start_capital
    holdings = {}  # symbol_idx -> qty
    last_rebal_idx = -999
    values = []
    rebalance_log = []

    for i in range(cfg.momentum_lookback, n):
        # Current portfolio value
        pos_val = sum(holdings.get(s, 0) * close_matrix[s][i] for s in range(n_sym))
        total_val = cash + pos_val

        # Rebalance?
        if i - last_rebal_idx >= cfg.rebalance_days:
            # Compute momentum for each symbol
            scores = []
            for s in range(n_sym):
                prev = close_matrix[s][i - cfg.momentum_lookback]
                curr = close_matrix[s][i]
                if prev > 0 and curr > 0:
                    mom = (curr - prev) / prev * 100
                    scores.append((s, mom))

            scores.sort(key=lambda x: x[1], reverse=True)

            # Pick top K (with optional absolute momentum filter)
            selected = []
            for s, mom in scores:
                if len(selected) >= cfg.top_k:
                    break
                if cfg.use_absolute_momentum and mom <= 0:
                    continue
                selected.append(s)

            # Liquidate current holdings
            for s, qty in holdings.items():
                cash += qty * close_matrix[s][i]
            holdings = {}

            # Equal-weight buy selected
            if selected:
                per_sym = total_val / len(selected)
                for s in selected:
                    price = close_matrix[s][i]
                    if price > 0:
                        qty = per_sym / price
                        holdings[s] = qty
                        cash -= qty * price

            last_rebal_idx = i
            rebalance_log.append({
                "epoch": common_epochs[i],
                "selected": [labels[s] for s in selected],
                "total_val": total_val,
            })

        # Recompute after potential rebalance
        pos_val = sum(holdings.get(s, 0) * close_matrix[s][i] for s in range(n_sym))
        values.append(cash + pos_val)

    return values, rebalance_log


def run_rotation_sweep(all_data, common_epochs, symbols, labels):
    """Sweep rotation configs."""
    results = []

    for mom_lb in [21, 60, 126, 252]:
        for top_k in [1, 2, 3, 5]:
            for rebal in [5, 21, 63]:
                for abs_mom in [True, False]:
                    cfg = RotationConfig(
                        momentum_lookback=mom_lb, top_k=top_k,
                        rebalance_days=rebal, use_absolute_momentum=abs_mom,
                    )
                    vals, rebal_log = simulate_rotation(
                        all_data, common_epochs, symbols, labels, cfg
                    )
                    epochs_subset = common_epochs[cfg.momentum_lookback:]
                    stats = compute_stats_simple(vals, epochs_subset)

                    if stats and abs(stats["max_dd"]) > 0.01:
                        results.append({
                            "cfg": cfg, "n_rebalances": len(rebal_log), **stats,
                        })

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print(f"\n{'='*130}")
    print(f"  ROTATION: TOP 15 by Calmar")
    print(f"{'='*130}")
    print(f"  {'#':<3} {'MomLB':>6} {'TopK':>5} {'Rebal':>6} {'AbsMom':>7} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'#Reb':>5}")
    print(f"  {'-'*75}")

    for i, r in enumerate(results[:15]):
        c = r["cfg"]
        am = "Y" if c.use_absolute_momentum else "N"
        print(f"  {i+1:<3} {c.momentum_lookback:>6} {c.top_k:>5} {c.rebalance_days:>6} "
              f"{am:>7} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x {r['n_rebalances']:>5}")

    # Print year-wise for best
    if results:
        best = results[0]
        yearly = best["yearly"]
        c = best["cfg"]
        print(f"\n  BEST: mom={c.momentum_lookback}d, top_k={c.top_k}, rebal={c.rebalance_days}d, "
              f"abs_mom={'Y' if c.use_absolute_momentum else 'N'}")
        print(f"  CAGR={best['cagr']:.1f}%, MaxDD={best['max_dd']:.1f}%, "
              f"Calmar={best['calmar']:.2f}")
        print(f"\n  {'Year':<6} {'Return':>9} {'Max DD':>9}")
        print(f"  {'-'*28}")
        for yr in sorted(yearly.keys()):
            y = yearly[yr]
            ret = (y["last"] - y["first"]) / y["first"] * 100
            dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
            print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")

    return results


# ── Equal-weight benchmark ───────────────────────────────────────────────────

def compute_equal_weight_bh(all_data, common_epochs, symbols, labels):
    """Equal-weight buy-and-hold across all rotation pool symbols."""
    n = len(common_epochs)
    n_sym = len(symbols)
    capital = 10_000_000
    per_sym = capital / n_sym

    holdings = {}
    for s in range(n_sym):
        price = all_data[symbols[s]].get(common_epochs[0], 0)
        if price > 0:
            holdings[s] = per_sym / price

    values = []
    for i in range(n):
        val = sum(holdings.get(s, 0) * all_data[symbols[s]].get(common_epochs[i], 0)
                  for s in range(n_sym))
        values.append(val)

    stats = compute_stats_simple(values, common_epochs)
    if stats:
        print(f"\n  Equal-weight B&H ({n_sym} indices): CAGR={stats['cagr']:.1f}%, "
              f"MaxDD={stats['max_dd']:.1f}%, Calmar={stats['calmar']:.2f}")
    return stats


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    # ── Part 1: Pairs Trading ──
    print("\n" + "=" * 80)
    print("  PART 1: PAIRS TRADING")
    print("=" * 80)

    for sym_a, sym_b, label, src_a, src_b in PAIRS:
        print(f"\n  Fetching {label} ({sym_a} vs {sym_b})...")
        data_a = fetch_data(cr, sym_a, src_a, start_epoch, end_epoch)
        data_b = fetch_data(cr, sym_b, src_b, start_epoch, end_epoch)

        if not data_a or not data_b:
            print(f"  Missing data, skipping")
            continue

        epochs, closes_a, closes_b = align_pair(data_a, data_b, start_epoch)
        if not epochs:
            print(f"  Insufficient overlapping data (<100 days)")
            continue

        print(f"  {len(epochs)} common trading days")
        run_pairs_sweep(epochs, closes_a, closes_b, label)

    # ── Part 2: Rotation Strategy ──
    print("\n" + "=" * 80)
    print("  PART 2: ROTATION (rank by momentum, hold top K)")
    print("=" * 80)

    all_data = {}
    symbols = []
    labels = []
    for sym, label, source in ROTATION_POOL:
        print(f"  Fetching {label} ({sym})...")
        data = fetch_data(cr, sym, source, start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            symbols.append(sym)
            labels.append(label)
        else:
            print(f"    No data for {sym}, skipping")

    if len(symbols) < 3:
        print("  Need at least 3 symbols for rotation")
        return

    # Find common epochs across ALL symbols
    epoch_sets = [set(all_data[s].keys()) for s in symbols]
    common = sorted(epoch_sets[0].intersection(*epoch_sets[1:]))
    common = [e for e in common if e >= start_epoch]
    print(f"\n  {len(symbols)} symbols, {len(common)} common trading days")

    if len(common) < 300:
        print("  Too few common days for rotation, trying pairwise overlap...")
        # Fallback: use epochs present in at least 2/3 of symbols
        from collections import Counter
        epoch_counts = Counter()
        for s in symbols:
            for e in all_data[s]:
                if e >= start_epoch:
                    epoch_counts[e] += 1
        threshold = max(2, len(symbols) * 2 // 3)
        common = sorted(e for e, c in epoch_counts.items() if c >= threshold)
        print(f"  Relaxed: {len(common)} days with >= {threshold}/{len(symbols)} symbols")

    if len(common) >= 300:
        compute_equal_weight_bh(all_data, common, symbols, labels)
        run_rotation_sweep(all_data, common, symbols, labels)
    else:
        print("  Still insufficient data for rotation strategy")


if __name__ == "__main__":
    main()

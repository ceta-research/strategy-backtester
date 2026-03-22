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
import dataclasses
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import BacktestResult, SweepResult, MultiSweepResult
from engine.charges import calculate_charges


# ── Exchange mapping (symbol → exchange for charge calculation) ───────────────

SYMBOL_EXCHANGE = {
    "^GSPC": "US", "^NSEI": "NSE", "^BSESN": "BSE", "^FTSE": "LSE",
    "^HSI": "HKSE", "^GDAXI": "XETRA", "^N225": "JPX", "^BVSP": "BVMF",
    "SPY": "US", "QQQ": "US",
    "NIFTYBEES": "NSE", "BANKBEES": "NSE",
}


IS_CLOUD = os.path.isdir("/session")


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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_summary(s):
    if not s:
        return "NO DATA"
    cagr = (s.get("cagr") or 0) * 100
    mdd = (s.get("max_drawdown") or 0) * 100
    calmar = s.get("calmar_ratio") or 0
    parts = [f"CAGR={cagr:>6.1f}%", f"MDD={mdd:>6.1f}%", f"Calmar={calmar:.2f}"]
    sh = s.get("sharpe_ratio")
    if sh is not None:
        parts.append(f"Sharpe={sh:.2f}")
    so = s.get("sortino_ratio")
    if so is not None:
        parts.append(f"Sortino={so:.2f}")
    return ", ".join(parts)


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
                   sym_a="^GSPC", sym_b="^NSEI", instrument="", params_dict=None):
    """Enhanced pair simulation with charges, integer quantities, and full tracking.

    Returns a BacktestResult with equity curve and trades.
    """
    if params_dict is None:
        params_dict = dataclasses.asdict(cfg)

    n = len(epochs)
    closes_a = [d["close"] for d in data_a_list]
    closes_b = [d["close"] for d in data_b_list]

    exchange_a = get_exchange(sym_a)
    exchange_b = get_exchange(sym_b)

    if not instrument:
        instrument = f"{sym_a} vs {sym_b}"

    result = BacktestResult("combined_pair", params_dict, instrument, "US",
                            capital, slippage_bps=0)

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
    crash_pause_until = -1
    buy_ch = 0.0

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
                result.add_trade(
                    entry_epoch=epochs[entry_idx], exit_epoch=epochs[i],
                    entry_price=entry_price, exit_price=curr_price,
                    quantity=qty, side="LONG",
                    charges=buy_ch + sell_charges, slippage=0.0,
                )
                buy_ch = 0.0
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
                        buy_ch = buy_charges

        # Portfolio value
        if position:
            side, qty, _, _ = position
            curr = closes_a[i] if side == "a" else closes_b[i]
            result.add_equity_point(epochs[i], cash + qty * curr)
        else:
            result.add_equity_point(epochs[i], cash)

    return result


def run_fine_tuned_pair(epochs, data_a_list, data_b_list, label, sym_a="^GSPC", sym_b="^NSEI"):
    """Fine-grained sweep on a single pair with realistic charges."""
    instrument_label = f"{sym_a} vs {sym_b}"
    sweep = SweepResult("combined_pair", instrument_label, "US", 10_000_000,
                        slippage_bps=0, description=f"Fine-tuned pair: {label}")

    configs = []
    if IS_CLOUD:
        # Reduced grid for cloud container (~96 configs vs 7680)
        for lb in [30, 60]:
            for z_in in [0.75, 1.0, 1.5]:
                for z_out in [-0.5, 0.0]:
                    for max_hold in [0, 60]:
                        for use_mom in [False, True]:
                            for use_crash in [False, True]:
                                configs.append(PairConfig(
                                    lookback=lb, z_entry=z_in, z_exit=z_out,
                                    max_hold_days=max_hold,
                                    use_momentum=use_mom,
                                    use_crash_safety=use_crash,
                                    invest_pct=1.0,
                                ))
    else:
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

    print(f"\n  {label}: Sweeping {len(configs)} configs (with charges)"
          f"{'  [cloud mode]' if IS_CLOUD else ''}...")

    for i, cfg in enumerate(configs):
        params_dict = dataclasses.asdict(cfg)
        br = simulate_pair(epochs, data_a_list, data_b_list, cfg,
                           sym_a=sym_a, sym_b=sym_b,
                           instrument=instrument_label, params_dict=params_dict)
        br.compute()
        s = br.to_dict().get("summary", {})
        mdd = s.get("max_drawdown")
        if mdd is not None and abs(mdd) > 0.0001:
            sweep.add_config(params_dict, br)
            br.compact()

        if (i + 1) % 5000 == 0:
            print(f"    {i+1}/{len(configs)} done...")

    # Leaderboards
    sweep.print_leaderboard(top_n=20, sort_by="calmar_ratio")

    # Configs > 20% CAGR
    sorted_configs = sweep._sorted("calmar_ratio")
    above_20 = [(p, r) for p, r in sorted_configs
                if (r.to_dict().get("summary", {}).get("cagr") or 0) >= 0.20]
    if above_20:
        print(f"\n  ** {len(above_20)} configs with CAGR >= 20% **")
        best_p, best_r = above_20[0]
        bs = best_r.to_dict()["summary"]
        print(f"  Best by Calmar: {_fmt_summary(bs)}")
        print(f"  Config: {best_p}")

    # Year-wise for best Calmar
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        print(f"\n  BEST (Calmar):")
        best_result.print_summary()

    return sweep


# ── Part 2: Multi-Pair Portfolio ─────────────────────────────────────────────

def simulate_multi_pair(pair_data, common_epochs, cfg: PairConfig, n_pairs, capital=10_000_000):
    """Run multiple pairs simultaneously with equal capital allocation.

    pair_data: list of (closes_a, closes_b, label, sym_a, sym_b) for each pair
    Returns a combined BacktestResult.
    """
    n = len(common_epochs)
    n_active = len(pair_data)
    per_pair_capital = capital / n_active

    params_dict = dataclasses.asdict(cfg)

    # Run each pair independently, collect BacktestResults
    pair_results = []
    for closes_a_list, closes_b_list, plabel, sym_a, sym_b in pair_data:
        br = simulate_pair(
            common_epochs, closes_a_list, closes_b_list, cfg,
            capital=per_pair_capital, sym_a=sym_a, sym_b=sym_b,
            instrument=f"{sym_a} vs {sym_b}", params_dict=params_dict,
        )
        pair_results.append(br)

    # Build combined equity by summing each pair's equity curve values
    start = max(cfg.lookback, cfg.momentum_period if cfg.use_momentum else 0)

    # Each pair result has equity starting from 'start' index.
    # All pairs use the same epochs and same start, so curves are aligned.
    min_len = min(len(pr.equity_curve) for pr in pair_results)

    combined_result = BacktestResult(
        "combined_multi_pair", params_dict,
        f"Multi({n_active} pairs)", "MIXED",
        capital, slippage_bps=0,
    )

    for i in range(min_len):
        epoch = pair_results[0].equity_curve[i][0]
        total_value = sum(pr.equity_curve[i][1] for pr in pair_results)
        combined_result.add_equity_point(epoch, total_value)

    # Copy trades from all sub-pairs
    for pr in pair_results:
        for trade in pr.trades:
            combined_result.trades.append(trade)
            combined_result.costs["total_charges"] += trade.get("charges", 0)
            combined_result.costs["total_slippage"] += trade.get("slippage", 0)

    return combined_result


def run_multi_pair_sweep(pair_data, common_epochs, label):
    """Sweep configs across all pairs simultaneously."""
    n_active = len(pair_data)
    sweep = SweepResult("combined_multi_pair", f"Multi({n_active} pairs): {label}",
                        "MIXED", 10_000_000, slippage_bps=0,
                        description=f"Multi-pair sweep: {label}")

    configs = []
    if IS_CLOUD:
        # Reduced grid for cloud container (~64 configs vs 384)
        for lb in [30, 60]:
            for z_in in [0.75, 1.0]:
                for z_out in [-0.5, 0.0]:
                    for max_hold in [0, 60]:
                        for use_mom in [False, True]:
                            for use_crash in [False, True]:
                                configs.append(PairConfig(
                                    lookback=lb, z_entry=z_in, z_exit=z_out,
                                    max_hold_days=max_hold,
                                    use_momentum=use_mom,
                                    use_crash_safety=use_crash,
                                ))
    else:
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

    print(f"\n  Multi-pair ({label}): Sweeping {len(configs)} configs"
          f"{'  [cloud mode]' if IS_CLOUD else ''}...")

    for i, cfg in enumerate(configs):
        params_dict = dataclasses.asdict(cfg)
        combined_result = simulate_multi_pair(pair_data, common_epochs, cfg, len(pair_data))
        combined_result.compute()
        s = combined_result.to_dict().get("summary", {})
        mdd = s.get("max_drawdown")
        if mdd is not None and abs(mdd) > 0.0001:
            sweep.add_config(params_dict, combined_result)
            combined_result.compact()

        if (i + 1) % 500 == 0:
            print(f"    {i+1}/{len(configs)} done...")

    # Leaderboard
    sweep.print_leaderboard(top_n=15, sort_by="calmar_ratio")

    # Configs > 20% CAGR
    sorted_configs = sweep._sorted("calmar_ratio")
    above_20 = [(p, r) for p, r in sorted_configs
                if (r.to_dict().get("summary", {}).get("cagr") or 0) >= 0.20]
    if above_20:
        print(f"\n  ** {len(above_20)} multi-pair configs with CAGR >= 20% **")
        best_p, best_r = above_20[0]
        bs = best_r.to_dict()["summary"]
        print(f"  Best: {_fmt_summary(bs)}")
        print(f"  Config: {best_p}")

    # Year-wise for best
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        print(f"\n  BEST:")
        best_result.print_summary()

    return sweep


# ── Part 3: Momentum Rotation + Pair Timing ──────────────────────────────────

def simulate_rotation_with_pairs(all_close_data, symbols, labels, common_epochs,
                                  mom_lookback=252, top_k=1, rebal_days=63,
                                  use_z_timing=False, z_lookback=30, z_threshold=1.0,
                                  abs_momentum=False, params_dict=None):
    """Enhanced rotation: rank by momentum, optionally use z-score timing for entry.

    Returns a BacktestResult with equity points only (no individual trades).
    """
    n = len(common_epochs)
    n_sym = len(symbols)

    if params_dict is None:
        params_dict = {
            "mom_lookback": mom_lookback, "top_k": top_k, "rebal_days": rebal_days,
            "use_z_timing": use_z_timing, "z_lookback": z_lookback,
            "z_threshold": z_threshold, "abs_momentum": abs_momentum,
        }

    result = BacktestResult(
        "rotation", params_dict,
        f"Rotation({len(symbols)} indices)", "MIXED",
        10_000_000, slippage_bps=0,
    )

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
        value = cash + pos_val
        result.add_equity_point(common_epochs[i], value)

    return result


def run_rotation_sweep(all_close_data, symbols, labels, common_epochs):
    """Sweep enhanced rotation configs."""
    sweep = SweepResult("rotation", f"Rotation({len(symbols)} indices)", "MIXED",
                        10_000_000, slippage_bps=0,
                        description=f"Momentum rotation sweep ({len(symbols)} indices)")

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

    for cfg in configs:
        params_dict = {
            "mom_lookback": cfg["mom"], "top_k": cfg["top_k"],
            "rebal_days": cfg["rebal"], "abs_momentum": cfg["abs_mom"],
            "use_z_timing": cfg["use_z"], "z_threshold": cfg["z_thresh"],
        }
        br = simulate_rotation_with_pairs(
            all_close_data, symbols, labels, common_epochs,
            mom_lookback=cfg["mom"], top_k=cfg["top_k"],
            rebal_days=cfg["rebal"], abs_momentum=cfg["abs_mom"],
            use_z_timing=cfg["use_z"], z_threshold=cfg["z_thresh"],
            params_dict=params_dict,
        )
        br.compute()
        s = br.to_dict().get("summary", {})
        mdd = s.get("max_drawdown")
        if mdd is not None and abs(mdd) > 0.0001:
            sweep.add_config(params_dict, br)

    # Leaderboard
    sweep.print_leaderboard(top_n=15, sort_by="calmar_ratio")

    # Configs > 20% CAGR
    sorted_configs = sweep._sorted("calmar_ratio")
    above_20 = [(p, r) for p, r in sorted_configs
                if (r.to_dict().get("summary", {}).get("cagr") or 0) >= 0.20]
    if above_20:
        print(f"\n  ** {len(above_20)} rotation configs with CAGR >= 20% **")

    # Year-wise for best
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        print(f"\n  BEST:")
        best_result.print_summary()

    return sweep


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

    multi = MultiSweepResult("combined_index_alpha",
                             "Fine-tuned pair + multi-pair + rotation")

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

    pair_list = [
        ("^GSPC", "^NSEI", "US vs India"),
        ("^GSPC", "^FTSE", "US vs UK"),
        ("^GSPC", "^HSI", "US vs HK"),
    ]
    if IS_CLOUD:
        pair_list = pair_list[:1]  # Just US vs India in cloud
    for sym_a, sym_b, label in pair_list:
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

        sweep = run_fine_tuned_pair(common, data_a_list, data_b_list, label,
                                    sym_a=sym_a, sym_b=sym_b)
        pair_key = label.replace(" ", "_").lower()
        multi.add_sweep(f"pair_{pair_key}", sweep)

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

    combos_to_test = pair_combos[:1] if IS_CLOUD else pair_combos
    for pairs in combos_to_test:
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

        sweep = run_multi_pair_sweep(pair_data, common, pair_label)
        multi.add_sweep(f"multi_{pair_label}", sweep)

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
            sweep = run_rotation_sweep(
                all_data, rot_symbols,
                [rot_labels[s] for s in rot_symbols],
                common
            )
            multi.add_sweep("rotation", sweep)

    # Save and print final leaderboard
    multi.save("result.json", top_n=15)
    multi.print_leaderboard(top_n=10)


if __name__ == "__main__":
    main()

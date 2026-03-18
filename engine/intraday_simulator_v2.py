"""Intraday portfolio simulator v2 (bar-level exit logic in Python).

v1 bakes exit logic into SQL CTEs. v2 receives a signal matrix (all bars
from entry onward) and resolves exits bar-by-bar in Python.

Supports: fixed target/stop (v1-equivalent), trailing stops, min hold bars,
bar hi/lo exit triggers, dynamic position sizing, margin checks, and
per-instrument limits. All features default to off/backward-compatible.
"""

import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta

from engine.intraday_simulator import _date_to_epoch, _get_charges_fn


def simulate_intraday_v2(signal_matrix: list, config: dict) -> dict:
    """Bar-level intraday simulation with Python exit logic.

    Args:
        signal_matrix: list of dicts from build_orb_signal_sql(), each row has:
            symbol, trade_date, entry_bar, entry_price,
            or_high, or_low, or_range, signal_strength,
            bar_num, bar_open, bar_high, bar_low, bar_close,
            bench_ret
        config: dict with keys:
            initial_capital, max_positions, order_value, exchange,
            target_pct, stop_pct, max_hold_bars,
            trailing_stop_pct (opt), min_hold_bars (opt), use_bar_hilo (opt),
            sizing_type (opt), sizing_pct (opt), max_order_value (opt),
            max_positions_per_instrument (opt),
            ranking_type (opt), ranking_window_days (opt),
            payout (opt): {type, value, interval_days, lockup_days}

    Returns:
        dict with: daily_returns, bench_returns, day_wise_log, trade_count,
                   win_count, trade_log  (same structure as v1)
    """
    if not signal_matrix:
        return {
            "daily_returns": [],
            "bench_returns": [],
            "day_wise_log": [],
            "trade_count": 0,
            "win_count": 0,
            "trade_log": [],
        }

    initial_capital = config["initial_capital"]
    max_positions = config["max_positions"]
    exchange = config.get("exchange", "NSE")
    charges_fn = _get_charges_fn(exchange)

    # Position sizing config
    sizing_type = config.get("sizing_type", "fixed")
    max_order_value = config.get("max_order_value")
    max_per_instrument = config.get("max_positions_per_instrument", max_positions)

    exit_config = {
        "target_pct": config["target_pct"],
        "stop_pct": config["stop_pct"],
        "trailing_stop_pct": config.get("trailing_stop_pct", 0),
        "min_hold_bars": config.get("min_hold_bars", 0),
        "use_bar_hilo": config.get("use_bar_hilo", False),
        "eod_buffer_bars": config.get("eod_buffer_bars", 30),
        "time_stop_bars": config.get("time_stop_bars", 0),
        "use_atr_stop": config.get("use_atr_stop", False),
        "atr_multiplier": config.get("atr_multiplier", 1.0),
        "exit_reentry_range": config.get("exit_reentry_range", False),
    }

    # Ranking config
    ranking_type = config.get("ranking_type", "signal_strength")
    ranking_window = config.get("ranking_window_days", 180)

    # Payout config
    payout_cfg = config.get("payout")
    total_withdrawn = 0.0
    days_elapsed = 0
    next_payout_day = None
    if payout_cfg:
        lockup = payout_cfg.get("lockup_days", 0)
        interval = payout_cfg.get("interval_days", 30)
        next_payout_day = max(lockup, interval)

    # Accept pre-built entries (dict) or raw signal matrix (list)
    if isinstance(signal_matrix, dict):
        entries_by_date = signal_matrix
    else:
        entries_by_date = _build_entry_signals(signal_matrix)

    daily_returns = []
    bench_returns = []
    day_wise_log = []
    trade_log = []
    margin = float(initial_capital)
    trade_count = 0
    win_count = 0
    symbol_pnl_history = defaultdict(list)  # symbol -> [(date_str, pnl_pct)]

    for d in sorted(entries_by_date.keys()):
        day_entries = entries_by_date[d]

        # Rank entries based on ranking mode
        symbol_scores = {}
        if ranking_type == "top_performer":
            symbol_scores = _compute_symbol_scores(symbol_pnl_history, d, ranking_window)
        ranked = _rank_entries(day_entries, ranking_type, symbol_scores)
        selected = ranked[:max_positions]

        # Compute order value for this day (all positions same size)
        ov = _compute_order_value(sizing_type, config, margin, max_positions)
        if max_order_value:
            ov = min(ov, max_order_value)
        if ov <= 0:
            # Can't size any positions, still record the day
            daily_returns.append(0.0)
            bench_returns.append(day_entries[0].get("bench_ret") or 0.0)
            day_wise_log.append({
                "log_date_epoch": _date_to_epoch(d),
                "invested_value": 0,
                "margin_available": margin,
            })
            continue

        charges = charges_fn(ov)
        margin_used = 0.0
        instrument_counts = {}

        daily_pnl = 0.0
        for entry in selected:
            entry_price = entry.get("entry_price")
            if not entry_price or entry_price <= 0:
                continue

            symbol = entry.get("symbol")

            # Per-instrument limit
            if instrument_counts.get(symbol, 0) >= max_per_instrument:
                continue

            # Margin check: can we afford this position?
            if margin - margin_used < ov:
                break  # all remaining entries need same ov, no point continuing

            margin_used += ov

            exit_result = _resolve_exit(entry, exit_config)
            exit_price = exit_result["exit_price"]

            pnl = (exit_price - entry_price) / entry_price * ov - charges
            daily_pnl += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1

            instrument_counts[symbol] = instrument_counts.get(symbol, 0) + 1

            # Record P&L for walk-forward ranking
            trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
            symbol_pnl_history[symbol].append((d, trade_pnl_pct))

            trade_log.append({
                "symbol": symbol,
                "trade_date": d,
                "entry_bar": entry.get("entry_bar"),
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "exit_type": exit_result["exit_type"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 4),
                "charges": round(charges, 2),
                "order_value": round(ov, 2),
                "signal_strength": entry.get("signal_strength"),
            })

        daily_ret = daily_pnl / margin if margin > 0 else 0.0
        margin += daily_pnl
        days_elapsed += 1

        # Process payout
        if payout_cfg and next_payout_day and days_elapsed >= next_payout_day:
            withdrawal = _compute_payout(payout_cfg, margin)
            margin -= withdrawal
            total_withdrawn += withdrawal
            next_payout_day += payout_cfg.get("interval_days", 30)

        daily_returns.append(daily_ret)
        bench_returns.append(day_entries[0].get("bench_ret") or 0.0)

        day_wise_log.append({
            "log_date_epoch": _date_to_epoch(d),
            "invested_value": 0,
            "margin_available": margin,
        })

    return {
        "daily_returns": daily_returns,
        "bench_returns": bench_returns,
        "day_wise_log": day_wise_log,
        "trade_count": trade_count,
        "win_count": win_count,
        "trade_log": trade_log,
        "total_withdrawn": total_withdrawn,
    }


def _compute_order_value(sizing_type: str, config: dict,
                         margin: float, max_positions: int) -> float:
    """Compute order value for a single position.

    Modes:
        fixed: use config["order_value"]
        equal_weight: margin / max_positions
        pct_equity: margin * config["sizing_pct"] / 100
    """
    if sizing_type == "equal_weight":
        return margin / max_positions if max_positions > 0 else 0
    elif sizing_type == "pct_equity":
        return margin * config.get("sizing_pct", 10) / 100
    else:  # "fixed" or unknown
        return config.get("order_value", 50000)


def _compute_payout(payout_cfg: dict, margin: float) -> float:
    """Compute withdrawal amount, capped at available margin."""
    ptype = payout_cfg.get("type", "fixed")
    value = payout_cfg.get("value", 0)
    if ptype == "percentage":
        amount = margin * value / 100
    else:  # "fixed"
        amount = value
    return min(amount, max(margin, 0))


def _rank_entries(entries: list, ranking_type: str, symbol_scores: dict) -> list:
    """Rank entry signals for position selection.

    Modes:
        signal_strength: sort by signal_strength descending (default, v1 behavior)
        top_performer: positive-P&L symbols first, then by trailing score desc,
                       tiebreak by signal_strength
    """
    if ranking_type == "top_performer":
        return sorted(entries, key=lambda e: (
            0 if symbol_scores.get(e.get("symbol"), 0) > 0 else 1,
            -symbol_scores.get(e.get("symbol"), 0),
            -(e.get("signal_strength") or 0),
        ))
    if ranking_type == "rvol":
        return sorted(entries, key=lambda e: -(e.get("rvol") or 0))
    # Default: signal_strength
    return sorted(entries, key=lambda e: -(e.get("signal_strength") or 0))


def _compute_symbol_scores(symbol_pnl_history: dict, current_date: str,
                           window_days: int) -> dict:
    """Compute trailing P&L score per symbol for walk-forward ranking."""
    current = datetime.strptime(current_date[:10], "%Y-%m-%d")
    cutoff_str = (current - timedelta(days=window_days)).strftime("%Y-%m-%d")

    scores = {}
    for symbol, history in symbol_pnl_history.items():
        recent = sum(pnl for d, pnl in history if d > cutoff_str)
        scores[symbol] = recent
    return scores


def _build_entry_signals(signal_matrix: list) -> dict:
    """Group flat signal matrix rows into structured entry signals by date.

    Returns: dict keyed by trade_date string, each value is a list of entry dicts:
        {
            "symbol": str, "entry_bar": int, "entry_price": float,
            "or_high": float, "or_low": float, "signal_strength": float,
            "bench_ret": float,
            "bars": [{"bar_num": int, "open": float, "high": float,
                       "low": float, "close": float}, ...]
        }
    """
    # Group by (trade_date, symbol) to collect bars per entry signal
    groups = defaultdict(list)
    for row in signal_matrix:
        key = (str(row["trade_date"]), row["symbol"])
        groups[key].append(row)

    by_date = defaultdict(list)
    for (trade_date, symbol), rows in groups.items():
        # Sort bars by bar_num
        rows.sort(key=lambda r: r["bar_num"])
        first = rows[0]
        entry = {
            "symbol": symbol,
            "entry_bar": first["entry_bar"],
            "entry_price": first["entry_price"],
            "or_high": first["or_high"],
            "or_low": first["or_low"],
            "or_range": first.get("or_range"),
            "signal_strength": first.get("signal_strength"),
            "bench_ret": first.get("bench_ret"),
            "rvol": first.get("rvol"),
            "atr_14": first.get("atr_14"),
            "bars": [
                {
                    "bar_num": r["bar_num"],
                    "open": r["bar_open"],
                    "high": r["bar_high"],
                    "low": r["bar_low"],
                    "close": r["bar_close"],
                }
                for r in rows
            ],
        }
        by_date[trade_date].append(entry)

    return dict(by_date)


def _resolve_exit(entry: dict, config: dict) -> dict:
    """Determine exit point from a bar sequence.

    Checks target/stop on EVERY bar from entry until eod_buffer_bars before
    session close. If no signal exit fires, force-exits at the cutoff bar.

    Supports:
    - Fixed target/stop (stop_pct based, OR low no longer used as floor)
    - ATR-based stop: entry_price - atr_multiplier * atr_14
    - Trailing stop: stop ratchets up as price makes new highs
    - Min hold: skip exit checks for first N bars after entry
    - Bar hi/lo: use bar high for target check, bar low for stop check
    - EOD buffer: force-exit N bars before session close (default 30)
    - Time stop: force-exit after N bars from entry
    - Re-entry range: exit if close drops back below or_high
    - Trail-only: target_pct=0 disables target (infinite target)

    Args:
        entry: dict with keys: bars, entry_price, or_low, or_high,
               and optionally atr_14, rvol
        config: {target_pct, stop_pct,
                 trailing_stop_pct (opt, default 0),
                 min_hold_bars (opt, default 0),
                 use_bar_hilo (opt, default False),
                 eod_buffer_bars (opt, default 30),
                 time_stop_bars (opt, default 0),
                 use_atr_stop (opt, default False),
                 atr_multiplier (opt, default 1.0),
                 exit_reentry_range (opt, default False)}

    Returns:
        {"exit_bar": int, "exit_price": float, "exit_type": str}
    """
    bars = entry["bars"]
    entry_price = entry["entry_price"]

    # Target: 0 means disabled (trail-only mode)
    target_pct = config["target_pct"]
    target_price = entry_price * (1 + target_pct) if target_pct > 0 else float("inf")

    # Stop: ATR-based or fixed percentage (OR low no longer used as floor)
    if config.get("use_atr_stop") and entry.get("atr_14"):
        fixed_stop = entry_price - config.get("atr_multiplier", 1.0) * entry["atr_14"]
    else:
        fixed_stop = entry_price * (1 - config["stop_pct"])

    trailing_pct = config.get("trailing_stop_pct", 0)
    min_hold = config.get("min_hold_bars", 0)
    use_hilo = config.get("use_bar_hilo", False)
    eod_buffer = config.get("eod_buffer_bars", 30)
    time_stop_bars = config.get("time_stop_bars", 0)
    exit_reentry = config.get("exit_reentry_range", False)
    or_high = entry.get("or_high", float("inf"))

    entry_bar = bars[0]["bar_num"]
    last_bar_num = bars[-1]["bar_num"]
    cutoff_bar = last_bar_num - eod_buffer
    highest = entry_price
    last_checked = bars[0]

    # Check bars AFTER entry (skip entry bar itself)
    for bar in bars[1:]:
        # Past the cutoff: force exit at the cutoff bar
        if bar["bar_num"] > cutoff_bar:
            break

        last_checked = bar

        # Time stop: exit after N bars from entry
        if time_stop_bars > 0 and (bar["bar_num"] - entry_bar) >= time_stop_bars:
            return {"exit_bar": bar["bar_num"], "exit_price": bar["close"], "exit_type": "time_stop"}

        # Price references: bar high/low when use_hilo, else bar close
        price_high = bar["high"] if use_hilo else bar["close"]
        price_low = bar["low"] if use_hilo else bar["close"]

        # Update highest seen price (for trailing stop tracking)
        if price_high > highest:
            highest = price_high

        # Compute current stop price (fixed or trailing, whichever is tighter)
        if trailing_pct > 0:
            trail_stop = highest * (1 - trailing_pct)
            stop_price = max(fixed_stop, trail_stop)
        else:
            stop_price = fixed_stop

        # Skip exit checks during min hold period (tracking continues above)
        if bar["bar_num"] - entry_bar <= min_hold:
            continue

        # Check exit conditions
        target_hit = price_high >= target_price
        stop_hit = price_low <= stop_price

        if target_hit or stop_hit:
            if use_hilo:
                # Fill at limit/stop price; if both triggered, assume stop first (conservative)
                exit_price = stop_price if stop_hit else target_price
            else:
                exit_price = bar["close"]
            return {"exit_bar": bar["bar_num"], "exit_price": exit_price, "exit_type": "signal"}

        # Re-entry into range: close drops back below OR high
        if exit_reentry and bar["close"] < or_high:
            return {"exit_bar": bar["bar_num"], "exit_price": bar["close"], "exit_type": "reentry"}

    # Force exit at cutoff bar (eod_buffer bars before session close)
    return {"exit_bar": last_checked["bar_num"], "exit_price": last_checked["close"], "exit_type": "eod"}


# ------------------------------------------------------------------ #
# Parallel sweep
# ------------------------------------------------------------------ #

def _run_single_config(args):
    """Worker function for parallel sweep. Takes (signal_matrix, config) tuple."""
    signal_matrix, config = args
    return simulate_intraday_v2(signal_matrix, config)


def run_parallel_sweep(signal_matrix: list, configs: list,
                       max_workers: int = None) -> list:
    """Run multiple sim configs in parallel on the same signal matrix.

    Args:
        signal_matrix: shared signal matrix (from SQL query)
        configs: list of config dicts (each is a full sim config)
        max_workers: max parallel processes (default: cpu_count - 1, capped at 8)

    Returns:
        list of result dicts, one per config
    """
    if not configs:
        return []

    if max_workers is None:
        max_workers = min(multiprocessing.cpu_count() - 1, 8)
    max_workers = max(max_workers, 1)

    # For small batches, run sequentially (avoid process overhead)
    if len(configs) <= 2 or max_workers <= 1:
        return [simulate_intraday_v2(signal_matrix, cfg) for cfg in configs]

    ctx = multiprocessing.get_context("spawn")
    work = [(signal_matrix, cfg) for cfg in configs]
    with ProcessPoolExecutor(max_workers=max_workers, mp_context=ctx) as pool:
        results = list(pool.map(_run_single_config, work))
    return results

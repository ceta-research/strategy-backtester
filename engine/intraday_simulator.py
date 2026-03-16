"""Intraday portfolio simulator.

All positions open and close within the same trading day.
Produces day_wise_log and trade_log matching the artifact structure
of ATO_Simulator / EOD pipeline for consistent analysis.
"""

from collections import defaultdict
from datetime import datetime, timezone

from engine.charges import nse_intraday_charges, us_intraday_charges


def _get_charges_fn(exchange: str):
    """Return the charge calculator for the given exchange."""
    if exchange in ("NASDAQ", "NYSE", "AMEX"):
        return us_intraday_charges
    return nse_intraday_charges


def simulate_intraday(trades: list, config: dict) -> dict:
    """Portfolio simulation for intraday trades.

    Args:
        trades: list of dicts with keys: symbol, trade_date, entry_price,
                exit_price, exit_type, signal_strength, bench_ret, entry_bar
        config: dict with keys: initial_capital, max_positions, order_value
                optional: exchange (default "NSE")

    Returns:
        dict with: daily_returns, bench_returns, day_wise_log, trade_count,
                   win_count, trade_log
    """
    initial_capital = config["initial_capital"]
    max_positions = config["max_positions"]
    order_value = config["order_value"]
    exchange = config.get("exchange", "NSE")
    charges_fn = _get_charges_fn(exchange)
    charges = charges_fn(order_value)

    # Group trades by date
    by_date = defaultdict(list)
    bench_by_date = {}
    for t in trades:
        d = str(t["trade_date"])
        by_date[d].append(t)
        bench_by_date[d] = t.get("bench_ret") or 0.0

    daily_returns = []
    bench_returns = []
    day_wise_log = []
    trade_log = []
    margin = float(initial_capital)
    trade_count = 0
    win_count = 0

    for d in sorted(by_date.keys()):
        day_trades = by_date[d]
        # Sort by signal_strength descending, take top max_positions
        day_trades.sort(key=lambda t: t.get("signal_strength") or 0, reverse=True)
        selected = day_trades[:max_positions]

        daily_pnl = 0.0
        for t in selected:
            entry = t.get("entry_price")
            exit_ = t.get("exit_price")
            if not entry or not exit_ or entry <= 0:
                continue

            pnl = (exit_ - entry) / entry * order_value - charges
            daily_pnl += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1

            trade_log.append({
                "symbol": t.get("symbol"),
                "trade_date": d,
                "entry_bar": t.get("entry_bar"),
                "entry_price": round(entry, 4),
                "exit_price": round(exit_, 4),
                "exit_type": t.get("exit_type"),
                "pnl": round(pnl, 2),
                "pnl_pct": round((exit_ - entry) / entry * 100, 4),
                "charges": round(charges, 2),
                "signal_strength": t.get("signal_strength"),
            })

        # Daily return = pnl / current capital BEFORE applying pnl
        # (matches EOD pipeline: (today - yesterday) / yesterday)
        daily_ret = daily_pnl / margin if margin > 0 else 0.0
        margin += daily_pnl

        daily_returns.append(daily_ret)
        bench_returns.append(bench_by_date[d])

        # day_wise_log: invested_value=0 (intraday positions close same day)
        # account_value = invested_value + margin_available (matches ATO_Simulator)
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
    }


def _date_to_epoch(date_str: str) -> int:
    """Convert YYYY-MM-DD string to Unix epoch (midnight UTC)."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

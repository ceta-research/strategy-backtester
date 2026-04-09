#!/usr/bin/env python3
"""Trace tool: Side-by-side comparison of engine vs standalone simulator.

Uses IDENTICAL data and cascade signals to isolate simulator differences.
Compares trade-by-trade decisions, exit timing, equity curves.

Config: f=42d, s=126d, accel>2%, mom>20%, sma=50, 10pos, tsl=13%, mpi=1
"""

import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

import polars as pl

from lib.backtest_result import BacktestResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_regime_epochs,
    CetaResearch, SLIPPAGE,
)
from scripts.momentum_breakout_v3 import compute_cascade_entries
from scripts.momentum_pyramid import simulate_pyramid_portfolio
from engine import simulator as engine_sim
from engine.charges import calculate_charges

# ── Config C (safe champion) ──
FAST_LB = 42
SLOW_LB = 126
ACCEL_THRESHOLD = 0.02
MIN_MOMENTUM = 0.20
REGIME_SMA = 50
MAX_POSITIONS = 10
MAX_PER_INSTRUMENT = 1
TSL_PCT = 13
MAX_HOLD_DAYS = 504
CAPITAL = 10_000_000
EXCHANGE = "NSE"
START_EPOCH = 1262304000   # 2010-01-01
END_EPOCH = 1773878400     # 2026-03-30


def epoch_to_date(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%d")


def precompute_exits(entries, price_data, tsl_pct, max_hold_days):
    """Walk-forward TSL exit computation (replicates engine signal generator logic).

    For each entry, walks forward through closes to find TSL or max_hold exit.
    Returns entries with exit_epoch and exit_price added.
    """
    orders = []
    for e in entries:
        sym = e["symbol"]
        if sym not in price_data:
            continue
        bars = price_data[sym]
        epochs = [b["epoch"] for b in bars]
        closes = [b["close"] for b in bars]

        entry_epoch = e["entry_epoch"]
        entry_price = e["entry_price"]
        peak_price = e["peak_price"]

        # Find start index (entry day)
        try:
            start_idx = epochs.index(entry_epoch)
        except ValueError:
            continue

        trail_high = entry_price
        reached_peak = False
        exit_epoch = None
        exit_price = None

        for j in range(start_idx, len(epochs)):
            c = closes[j]
            if c > trail_high:
                trail_high = c

            hold_days = (epochs[j] - entry_epoch) / 86400
            if max_hold_days > 0 and hold_days >= max_hold_days:
                exit_epoch = epochs[j]
                exit_price = c
                break

            if c >= peak_price:
                reached_peak = True
            if reached_peak and tsl_pct > 0 and c <= trail_high * (1 - tsl_pct / 100.0):
                exit_epoch = epochs[j]
                exit_price = c
                break

        if exit_epoch is None:
            # No exit found: close at end of data
            exit_epoch = epochs[-1]
            exit_price = closes[-1]

        orders.append({
            **e,
            "exit_epoch": exit_epoch,
            "exit_price": exit_price,
        })

    return orders


def build_engine_orders_df(orders_with_exits):
    """Convert standalone entries+exits to Polars DataFrame for engine simulator."""
    rows = []
    for o in orders_with_exits:
        rows.append({
            "instrument": f"NSE:{o['symbol']}",
            "entry_epoch": o["entry_epoch"],
            "exit_epoch": o["exit_epoch"],
            "entry_price": o["entry_price"],
            "exit_price": o["exit_price"],
            "entry_volume": 1_000_000,
            "exit_volume": 1_000_000,
            "scanner_config_ids": "0",
            "entry_config_ids": "0",
            "exit_config_ids": "0",
        })
    return pl.DataFrame(rows)


def build_epoch_wise_stats(price_data, start_epoch):
    """Build epoch_wise_instrument_stats from standalone price_data dict.

    Matches engine format: {epoch: {"NSE:SYMBOL": {"close": float, "avg_txn": float}}}
    """
    stats = {}
    for sym, bars in price_data.items():
        inst = f"NSE:{sym}"
        for b in bars:
            ep = b["epoch"]
            if ep < start_epoch - 86400 * 30:  # small buffer
                continue
            if ep not in stats:
                stats[ep] = {}
            vol = b.get("volume", 0)
            close = b["close"]
            stats[ep][inst] = {
                "close": close,
                "avg_txn": vol * close,
            }
    return stats


def main():
    cr = CetaResearch()

    print("=" * 80)
    print("  ENGINE vs STANDALONE: TRACE COMPARISON")
    print("  Config C: f=42d, accel>2%, mom>20%, sma=50, 10pos, tsl=13%")
    print("=" * 80)

    # ── 1. FETCH DATA (shared) ──
    print("\n--- 1. Fetching Data (bhavcopy, shared for both) ---")
    t0 = time.time()
    price_data = fetch_universe(cr, EXCHANGE, START_EPOCH, END_EPOCH,
                                source="bhavcopy", turnover_threshold=70_000_000)
    print(f"  {len(price_data)} symbols in {time.time()-t0:.0f}s")

    benchmark = fetch_benchmark(cr, "NIFTYBEES", EXCHANGE, START_EPOCH, END_EPOCH,
                                warmup_days=250, source="bhavcopy")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)
    print(f"  Regime epochs (SMA {REGIME_SMA}): {len(regime_epochs)} days")

    # ── 2. GENERATE CASCADE ENTRIES (shared) ──
    print("\n--- 2. Generating Cascade Entries ---")
    all_entries = compute_cascade_entries(price_data, FAST_LB, SLOW_LB,
                                         ACCEL_THRESHOLD, MIN_MOMENTUM,
                                         start_epoch=START_EPOCH)

    # Pre-filter by regime (so both simulators see identical entries)
    regime_entries = [e for e in all_entries if e["epoch"] in regime_epochs]
    print(f"  Raw: {len(all_entries)} → Regime-filtered: {len(regime_entries)}")

    # ── 3. PRE-COMPUTE EXITS (for engine) ──
    print("\n--- 3. Pre-computing Exits (engine format) ---")
    orders_with_exits = precompute_exits(regime_entries, price_data, TSL_PCT, MAX_HOLD_DAYS)
    print(f"  {len(orders_with_exits)} orders with pre-computed exits")

    # Show a few examples
    print(f"\n  Sample orders (first 5):")
    print(f"  {'Symbol':<15} {'Signal Date':<12} {'Entry Date':<12} {'Exit Date':<12} "
          f"{'Entry Px':>10} {'Exit Px':>10} {'Return':>8}")
    for o in orders_with_exits[:5]:
        ret = (o["exit_price"] / o["entry_price"] - 1) * 100
        print(f"  {o['symbol']:<15} {epoch_to_date(o['epoch']):<12} "
              f"{epoch_to_date(o['entry_epoch']):<12} {epoch_to_date(o['exit_epoch']):<12} "
              f"{o['entry_price']:>10.2f} {o['exit_price']:>10.2f} {ret:>+7.1f}%")

    # ── 4. RUN STANDALONE SIMULATOR ──
    print(f"\n--- 4. Running Standalone Simulator ---")
    t0 = time.time()
    standalone_result, standalone_log = simulate_pyramid_portfolio(
        regime_entries, price_data, benchmark,
        capital=CAPITAL, max_positions=MAX_POSITIONS, max_per_instrument=MAX_PER_INSTRUMENT,
        tsl_pct=TSL_PCT, max_hold_days=MAX_HOLD_DAYS, exchange=EXCHANGE,
        regime_epochs=None,  # already filtered
        start_epoch=START_EPOCH,
        strategy_name="standalone_trace", params={},
    )
    standalone_time = time.time() - t0

    s_dict = standalone_result.to_dict()
    s_summary = s_dict.get("summary", {})
    s_trades = s_dict.get("trades", [])
    s_equity = s_dict.get("equity_curve", {})

    print(f"  Time: {standalone_time:.1f}s")
    print(f"  CAGR: {(s_summary.get('cagr') or 0) * 100:.1f}%")
    print(f"  MDD:  {(s_summary.get('max_drawdown') or 0) * 100:.1f}%")
    print(f"  Calmar: {s_summary.get('calmar_ratio', 0):.2f}")
    print(f"  Trades: {s_summary.get('total_trades', 0)}")

    # ── 5. BUILD ENGINE INPUTS ──
    print(f"\n--- 5. Building Engine Inputs ---")
    df_orders = build_engine_orders_df(orders_with_exits)
    epoch_stats = build_epoch_wise_stats(price_data, START_EPOCH)
    print(f"  {len(df_orders)} orders, {len(epoch_stats)} epoch stats")

    # Sort by entry_epoch (engine expects chronological order)
    df_orders = df_orders.sort("entry_epoch")

    last_entry_epoch = df_orders["entry_epoch"].max()
    last_exit_epoch = df_orders["exit_epoch"].max()
    print(f"  Last entry epoch: {epoch_to_date(last_entry_epoch)}")
    print(f"  Last exit epoch:  {epoch_to_date(last_exit_epoch)}")
    print(f"  Engine will STOP at: {epoch_to_date(last_entry_epoch)} (line 86: >= end_epoch)")
    print(f"  → Truncates {(last_exit_epoch - last_entry_epoch) / 86400:.0f} days of exits")

    context = {
        "start_margin": CAPITAL,
        "start_epoch": START_EPOCH,
        "end_epoch": END_EPOCH,
    }

    sim_config = {
        "id": "SIM0",
        "max_positions": MAX_POSITIONS,
        "max_positions_per_instrument": MAX_PER_INSTRUMENT,
        "order_value_multiplier": 1.0,
        "max_order_value": {"type": "fixed", "value": 1_000_000_000},  # unlimited
    }

    # ── 6. RUN ENGINE SIMULATOR ──
    print(f"\n--- 6. Running Engine Simulator ---")
    t0 = time.time()
    day_wise_log, config_order_ids, snapshot, day_wise_positions, trade_log = engine_sim.process(
        context, df_orders, epoch_stats, {}, sim_config, "TRACE"
    )
    engine_time = time.time() - t0

    # Build BacktestResult from engine output
    engine_br = BacktestResult("engine_trace", {}, "PORTFOLIO", EXCHANGE, CAPITAL)
    for day in day_wise_log:
        engine_br.add_equity_point(
            day["log_date_epoch"],
            day["invested_value"] + day["margin_available"],
        )
    for t in trade_log:
        total_charges = t.get("entry_charges", 0) + t.get("sell_charges", 0)
        engine_br.add_trade(
            t["entry_epoch"], t["exit_epoch"],
            t["entry_price"], t["exit_price"],
            t["quantity"], charges=total_charges,
            symbol=t.get("instrument", ""),
        )

    e_dict = engine_br.to_dict()
    e_summary = e_dict.get("summary", {})
    e_trades = e_dict.get("trades", [])
    e_equity = e_dict.get("equity_curve", {})

    print(f"  Time: {engine_time:.1f}s")
    print(f"  CAGR: {(e_summary.get('cagr') or 0) * 100:.1f}%")
    print(f"  MDD:  {(e_summary.get('max_drawdown') or 0) * 100:.1f}%")
    print(f"  Calmar: {e_summary.get('calmar_ratio', 0):.2f}")
    print(f"  Trades: {e_summary.get('total_trades', 0)}")

    # ═══════════════════════════════════════════════════════════════════
    #  COMPARISON
    # ═══════════════════════════════════════════════════════════════════

    print(f"\n{'='*80}")
    print("  SIDE-BY-SIDE COMPARISON")
    print(f"{'='*80}")

    s_cagr = (s_summary.get('cagr') or 0) * 100
    e_cagr = (e_summary.get('cagr') or 0) * 100
    s_mdd = (s_summary.get('max_drawdown') or 0) * 100
    e_mdd = (e_summary.get('max_drawdown') or 0) * 100
    s_calmar = s_summary.get('calmar_ratio', 0)
    e_calmar = e_summary.get('calmar_ratio', 0)
    s_ntrades = s_summary.get('total_trades', 0)
    e_ntrades = e_summary.get('total_trades', 0)
    s_win_rate = (s_summary.get('win_rate') or 0) * 100
    e_win_rate = (e_summary.get('win_rate') or 0) * 100

    print(f"\n  {'Metric':<20} {'Standalone':>12} {'Engine':>12} {'Delta':>12}")
    print(f"  {'-'*56}")
    print(f"  {'CAGR':<20} {s_cagr:>11.1f}% {e_cagr:>11.1f}% {e_cagr-s_cagr:>+11.1f}%")
    print(f"  {'Max Drawdown':<20} {s_mdd:>11.1f}% {e_mdd:>11.1f}% {e_mdd-s_mdd:>+11.1f}%")
    print(f"  {'Calmar':<20} {s_calmar:>12.2f} {e_calmar:>12.2f} {e_calmar-s_calmar:>+12.2f}")
    print(f"  {'Total Trades':<20} {s_ntrades:>12} {e_ntrades:>12} {e_ntrades-s_ntrades:>+12}")
    print(f"  {'Win Rate':<20} {s_win_rate:>11.1f}% {e_win_rate:>11.1f}% {e_win_rate-s_win_rate:>+11.1f}%")

    # ── TRADE-LEVEL ANALYSIS ──
    print(f"\n{'='*80}")
    print("  TRADE-LEVEL ANALYSIS")
    print(f"{'='*80}")

    # Group trades by (symbol, entry_epoch)
    s_trade_map = {}
    for t in s_trades:
        sym = t.get("symbol", "")
        key = (sym, t.get("entry_epoch", 0))
        s_trade_map[key] = t

    e_trade_map = {}
    for t in e_trades:
        sym = t.get("symbol", "").replace("NSE:", "")
        key = (sym, t.get("entry_epoch", 0))
        e_trade_map[key] = t

    s_keys = set(s_trade_map.keys())
    e_keys = set(e_trade_map.keys())

    shared = s_keys & e_keys
    standalone_only = s_keys - e_keys
    engine_only = e_keys - s_keys

    print(f"\n  Shared trades:     {len(shared)}")
    print(f"  Standalone only:   {len(standalone_only)} (engine missed these)")
    print(f"  Engine only:       {len(engine_only)} (standalone missed these)")

    # ── SHARED TRADES: EXIT ANALYSIS ──
    if shared:
        exit_diffs = []
        for key in sorted(shared):
            st = s_trade_map[key]
            et = e_trade_map[key]
            s_exit = st.get("exit_epoch", 0)
            e_exit = et.get("exit_epoch", 0)
            s_entry_px = st.get("entry_price", 1)
            e_entry_px = et.get("entry_price", 1)
            s_exit_px = st.get("exit_price", 0)
            e_exit_px = et.get("exit_price", 0)
            s_ret = (s_exit_px / s_entry_px - 1) * 100 if s_entry_px else 0
            e_ret = (e_exit_px / e_entry_px - 1) * 100 if e_entry_px else 0

            exit_diffs.append({
                "symbol": key[0],
                "entry_epoch": key[1],
                "s_exit_epoch": s_exit,
                "e_exit_epoch": e_exit,
                "exit_diff_days": (e_exit - s_exit) / 86400,
                "s_entry_price": s_entry_px,
                "e_entry_price": e_entry_px,
                "s_exit_price": s_exit_px,
                "e_exit_price": e_exit_px,
                "s_return": s_ret,
                "e_return": e_ret,
                "return_diff": e_ret - s_ret,
            })

        same_exit = sum(1 for d in exit_diffs if abs(d["exit_diff_days"]) < 1.5)
        diff_exit = len(exit_diffs) - same_exit
        avg_return_diff = sum(d["return_diff"] for d in exit_diffs) / len(exit_diffs)
        avg_s_return = sum(d["s_return"] for d in exit_diffs) / len(exit_diffs)
        avg_e_return = sum(d["e_return"] for d in exit_diffs) / len(exit_diffs)

        print(f"\n  Shared trade exit analysis:")
        print(f"    Same exit date (±1 day): {same_exit}")
        print(f"    Different exit date:     {diff_exit}")
        print(f"    Avg standalone return:   {avg_s_return:+.2f}%")
        print(f"    Avg engine return:       {avg_e_return:+.2f}%")
        print(f"    Avg return diff (E-S):   {avg_return_diff:+.2f}%")

        # Entry price differences (should be identical if same data)
        entry_px_diffs = [(d["s_entry_price"], d["e_entry_price"]) for d in exit_diffs]
        entry_px_match = sum(1 for s, e in entry_px_diffs if abs(s - e) < 0.01)
        print(f"    Entry price matches:     {entry_px_match}/{len(entry_px_diffs)}")

        # Show biggest return divergences
        exit_diffs.sort(key=lambda d: abs(d["return_diff"]), reverse=True)
        print(f"\n  Top 10 divergent shared trades (by |return diff|):")
        print(f"    {'Symbol':<14} {'Entry':<11} {'S_Exit':<11} {'E_Exit':<11} "
              f"{'S_Ret':>8} {'E_Ret':>8} {'Diff':>8} {'ExDays':>7}")
        for d in exit_diffs[:10]:
            print(f"    {d['symbol']:<14} {epoch_to_date(d['entry_epoch']):<11} "
                  f"{epoch_to_date(d['s_exit_epoch']):<11} {epoch_to_date(d['e_exit_epoch']):<11} "
                  f"{d['s_return']:>+7.1f}% {d['e_return']:>+7.1f}% "
                  f"{d['return_diff']:>+7.1f}% {d['exit_diff_days']:>+6.0f}d")

    # ── STANDALONE-ONLY TRADES ──
    if standalone_only:
        s_only_returns = []
        s_only_list = []
        for key in sorted(standalone_only, key=lambda k: k[1]):
            t = s_trade_map[key]
            ep = t.get("entry_price", 1)
            xp = t.get("exit_price", 0)
            ret = (xp / ep - 1) * 100 if ep else 0
            s_only_returns.append(ret)
            s_only_list.append((key[0], key[1], t.get("exit_epoch", 0), ret))

        avg_missed = sum(s_only_returns) / len(s_only_returns) if s_only_returns else 0
        pos_missed = sum(1 for r in s_only_returns if r > 0)
        total_missed_pnl = sum(s_only_returns)

        print(f"\n  Standalone-only trades (engine MISSED these):")
        print(f"    Count:      {len(standalone_only)}")
        print(f"    Avg return: {avg_missed:+.1f}%")
        print(f"    Positive:   {pos_missed}/{len(standalone_only)}")
        print(f"    Sum return: {total_missed_pnl:+.1f}%")

        # Show first 15
        print(f"\n    First 15 missed trades:")
        print(f"    {'Symbol':<14} {'Entry':<11} {'Exit':<11} {'Return':>8}")
        for sym, entry_ep, exit_ep, ret in s_only_list[:15]:
            print(f"    {sym:<14} {epoch_to_date(entry_ep):<11} "
                  f"{epoch_to_date(exit_ep):<11} {ret:>+7.1f}%")

    # ── ENGINE-ONLY TRADES ──
    if engine_only:
        e_only_returns = []
        for key in engine_only:
            t = e_trade_map[key]
            ep = t.get("entry_price", 1)
            xp = t.get("exit_price", 0)
            ret = (xp / ep - 1) * 100 if ep else 0
            e_only_returns.append(ret)

        avg_extra = sum(e_only_returns) / len(e_only_returns) if e_only_returns else 0
        print(f"\n  Engine-only trades (standalone missed these):")
        print(f"    Count:      {len(engine_only)}")
        print(f"    Avg return: {avg_extra:+.1f}%")

    # ── EQUITY CURVE SNAPSHOTS ──
    print(f"\n{'='*80}")
    print("  EQUITY CURVE (yearly snapshots)")
    print(f"{'='*80}")

    # equity_curve is a list of {epoch, date, value} dicts
    s_eq_map = {pt["epoch"]: pt["value"] for pt in s_equity} if isinstance(s_equity, list) else {}
    e_eq_map = {pt["epoch"]: pt["value"] for pt in e_equity} if isinstance(e_equity, list) else {}

    # Sample at Jan 1 each year
    print(f"\n  {'Year':<6} {'Standalone':>14} {'Engine':>14} {'E/S Ratio':>10}")
    print(f"  {'-'*44}")
    for year in range(2011, 2027):
        target = int(datetime(year, 1, 1).timestamp())
        # Find closest epoch in each
        s_closest = min(s_eq_map.keys(), key=lambda x: abs(x - target)) if s_eq_map else None
        e_closest = min(e_eq_map.keys(), key=lambda x: abs(x - target)) if e_eq_map else None

        if s_closest and e_closest and abs(s_closest - target) < 90 * 86400:
            s_val = s_eq_map[s_closest]
            e_val = e_eq_map.get(e_closest, 0)
            ratio = e_val / s_val if s_val else 0
            print(f"  {year:<6} {s_val/1e6:>12.2f}M {e_val/1e6:>12.2f}M {ratio:>9.2f}x")

    # ── ENGINE SIMULATION STATE ──
    print(f"\n{'='*80}")
    print("  ENGINE SIMULATOR STATE")
    print(f"{'='*80}")

    if day_wise_log:
        first_day = day_wise_log[0]
        last_day = day_wise_log[-1]
        print(f"  First day: {epoch_to_date(first_day['log_date_epoch'])}")
        print(f"  Last day:  {epoch_to_date(last_day['log_date_epoch'])}")
        print(f"  Days processed: {len(day_wise_log)}")
        print(f"  Final margin: {last_day['margin_available']/1e6:.2f}M")
        print(f"  Final invested: {last_day['invested_value']/1e6:.2f}M")

        # How many positions still open at end?
        open_positions = snapshot.get("current_positions", {})
        open_count = sum(len(v) for v in open_positions.values())
        print(f"  Positions still open: {open_count}")
        if open_positions:
            print(f"  Open positions (unrealized, never closed):")
            for inst, positions in open_positions.items():
                for oid, pos in positions.items():
                    entry_px = pos.get("entry_price", 0)
                    last_px = pos.get("last_close_price", entry_px)
                    ret = (last_px / entry_px - 1) * 100 if entry_px else 0
                    print(f"    {inst:<20} entry={epoch_to_date(pos['entry_epoch'])} "
                          f"scheduled_exit={epoch_to_date(pos['exit_epoch'])} "
                          f"px={entry_px:.2f}→{last_px:.2f} ({ret:+.1f}%)")

    # ── STANDALONE LOG ──
    if standalone_log:
        print(f"\n  Standalone simulator:")
        print(f"  Days processed: {len(standalone_log)}")
        last_sl = standalone_log[-1]
        print(f"  Last day: {epoch_to_date(last_sl['epoch'])}")
        print(f"  Final margin: {last_sl['margin_available']/1e6:.2f}M")
        print(f"  Final invested: {last_sl['invested_value']/1e6:.2f}M")

    # ── DIAGNOSIS ──
    print(f"\n{'='*80}")
    print("  DIAGNOSIS")
    print(f"{'='*80}")

    print(f"\n  Entry signals available: {len(regime_entries)}")
    print(f"  Standalone trades: {s_ntrades}")
    print(f"  Engine trades:     {e_ntrades}")
    if s_ntrades > 0:
        print(f"  Trade ratio:       {e_ntrades/s_ntrades:.2f}x")
    if s_cagr != 0:
        print(f"  CAGR ratio:        {e_cagr/s_cagr:.2f}x")

    # Check for same-day entry+exit collisions in engine
    entry_epochs_set = set(df_orders["entry_epoch"].to_list())
    exit_epochs_set = set(df_orders["exit_epoch"].to_list())
    collision_days = entry_epochs_set & exit_epochs_set
    print(f"\n  Same-day entry+exit epochs: {len(collision_days)}")

    # Count how many of those collision days actually had both processed
    collision_entries = df_orders.filter(pl.col("entry_epoch").is_in(list(collision_days)))
    collision_exits = df_orders.filter(pl.col("exit_epoch").is_in(list(collision_days)))
    print(f"  Entries on collision days:  {len(collision_entries)}")
    print(f"  Exits on collision days:    {len(collision_exits)}")

    # Truncation analysis
    standalone_last_epoch = standalone_log[-1]["epoch"] if standalone_log else 0
    engine_last_epoch = day_wise_log[-1]["log_date_epoch"] if day_wise_log else 0
    if standalone_last_epoch and engine_last_epoch:
        truncation_days = (standalone_last_epoch - engine_last_epoch) / 86400
        print(f"\n  Standalone ends: {epoch_to_date(standalone_last_epoch)}")
        print(f"  Engine ends:     {epoch_to_date(engine_last_epoch)}")
        print(f"  Truncation:      {truncation_days:.0f} days")

        if truncation_days > 30:
            # What was equity at engine end date in standalone?
            closest_s = min(s_eq_map.keys(), key=lambda x: abs(x - engine_last_epoch)) if s_eq_map else 0
            s_val_at_engine_end = s_eq_map.get(closest_s, 0)
            s_val_at_end = s_eq_map.get(max(s_eq_map.keys()), 0) if s_eq_map else 0
            if s_val_at_engine_end:
                tail_return = (s_val_at_end / s_val_at_engine_end - 1) * 100
                print(f"  Standalone equity at engine end: {s_val_at_engine_end/1e6:.2f}M")
                print(f"  Standalone equity at data end:   {s_val_at_end/1e6:.2f}M")
                print(f"  Truncated tail return:           {tail_return:+.1f}%")

    print(f"\n{'='*80}")
    print("  KEY FINDINGS")
    print(f"{'='*80}")

    findings = []
    if e_ntrades != s_ntrades:
        findings.append(f"Trade count differs: standalone={s_ntrades}, engine={e_ntrades} "
                        f"(delta={e_ntrades-s_ntrades:+d})")
    if shared and diff_exit > 0:
        findings.append(f"Exit dates differ for {diff_exit}/{len(shared)} shared trades "
                        f"(avg return diff: {avg_return_diff:+.2f}%)")
    if standalone_only:
        findings.append(f"Engine missed {len(standalone_only)} trades that standalone took "
                        f"(avg return: {avg_missed:+.1f}%)")
    if engine_only:
        findings.append(f"Standalone missed {len(engine_only)} trades that engine took")
    if open_count > 0:
        findings.append(f"Engine has {open_count} positions still open (exits truncated)")
    if abs(s_cagr - e_cagr) > 2:
        findings.append(f"CAGR gap: {abs(s_cagr-e_cagr):.1f}pp")

    for i, f in enumerate(findings, 1):
        print(f"  {i}. {f}")

    if not findings:
        print("  No significant differences found!")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Debug pipeline: trace every step of the intraday v2 simulation.

Runs a SINGLE config (1 SQL config x 1 SIM config) with full tracing.
Each step prints output matching the numbered flow from the architecture doc.

Usage:
    # Full run, trace first 5 days in detail
    python scripts/debug_pipeline.py --strategy orb_us

    # Stop after config parsing (no API calls)
    python scripts/debug_pipeline.py --strategy orb_us --stop-after config

    # Stop after SQL execution (inspect entries, no simulation)
    python scripts/debug_pipeline.py --strategy orb_us --stop-after entries

    # Narrow date range (1 month), trace 10 days
    python scripts/debug_pipeline.py --strategy orb_us --start-date 2024-01-02 --end-date 2024-02-01 --trace-days 10

    # Pick specific combo indices (default: 0, 0 = simplest config)
    python scripts/debug_pipeline.py --strategy orb_us --sql-idx 2 --sim-idx 10

    # Re-run from cached entries (skip SQL), excluding symbols
    python scripts/debug_pipeline.py --strategy orb_us --load-entries debug_output/entries_cache.pkl --exclude-symbols FISV
"""

import argparse
import json
import os
import pickle
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from itertools import product

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.intraday_sql_builder import build_orb_signal_sql
from engine.intraday_simulator import _date_to_epoch, _get_charges_fn
from engine.intraday_simulator_v2 import (
    _build_entry_signals, _resolve_exit, _rank_entries,
    _compute_order_value, _compute_symbol_scores, _compute_payout,
)
from engine.intraday_pipeline import (
    SQL_KEYS_V2, SIM_KEYS_V2, EXCHANGE_SQL_DEFAULTS,
    _cartesian, _date_chunks,
)
from lib.cr_client import CetaResearch
from lib.metrics import compute_metrics


STOP_POINTS = ["config", "sql", "entries", "sim", "metrics", "all"]

DIV = "=" * 80
SUBDIV = "-" * 60


def hdr(num, title):
    print(f"\n{DIV}")
    print(f"  STEP {num}: {title}")
    print(DIV)


def subhdr(label):
    print(f"\n  {SUBDIV}")
    print(f"  {label}")
    print(f"  {SUBDIV}")


def dump_json(dump_dir, filename, data):
    path = os.path.join(dump_dir, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


def main():
    parser = argparse.ArgumentParser(description="Debug intraday v2 pipeline step by step")
    parser.add_argument("--strategy", required=True, help="Strategy name (e.g. orb_us)")
    parser.add_argument("--config", help="Custom config YAML path")
    parser.add_argument("--stop-after", default="all", choices=STOP_POINTS,
                        help="Stop after this phase")
    parser.add_argument("--trace-days", type=int, default=5,
                        help="Number of days to trace bar-by-bar (default: 5)")
    parser.add_argument("--sql-idx", type=int, default=0, help="SQL config index")
    parser.add_argument("--sim-idx", type=int, default=0, help="SIM config index")
    parser.add_argument("--start-date", help="Override start_date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Override end_date (YYYY-MM-DD)")
    parser.add_argument("--dump-dir", default=None, help="Output directory for JSON dumps")
    parser.add_argument("--load-entries", default=None,
                        help="Load entries from pickle cache (skip SQL)")
    parser.add_argument("--exclude-symbols", default=None,
                        help="Comma-separated symbols to exclude (e.g. FISV,TQQQ)")
    parser.add_argument("--max-signal-strength", type=float, default=None,
                        help="Cap: discard entries with signal_strength above this (e.g. 1.0)")
    args = parser.parse_args()

    stop_at = STOP_POINTS.index(args.stop_after)

    dump_dir = args.dump_dir or os.path.join(ROOT, "debug_output")
    os.makedirs(dump_dir, exist_ok=True)
    print(f"Debug output: {dump_dir}/")

    # ==================================================================
    # PHASE 1: CONFIG (steps 1-8)
    # ==================================================================

    hdr("1-2", "Load Config")

    config_path = args.config or os.path.join(ROOT, "strategies", args.strategy, "config.yaml")
    print(f"  Path: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    print(f"  Sections: {list(raw.keys())}")

    # -- steps 3-4 --
    hdr("3-4", "Static Values")
    static = raw["static"]
    for k, v in static.items():
        print(f"  {k}: {v}")

    if args.start_date:
        static["start_date"] = args.start_date
        print(f"  [OVERRIDE] start_date -> {args.start_date}")
    if args.end_date:
        static["end_date"] = args.end_date
        print(f"  [OVERRIDE] end_date -> {args.end_date}")

    exchange = static.get("exchange", "NSE")
    initial_capital = static.get("initial_capital", 500_000)
    risk_free_rate = static.get("risk_free_rate", 0.02)

    # -- step 5 --
    hdr(5, "Pipeline Version")
    version = static.get("pipeline_version", "v2")
    print(f"  Version: {version}")
    print(f"  SQL_KEYS: {sorted(SQL_KEYS_V2)}")
    print(f"  SIM_KEYS: {sorted(SIM_KEYS_V2)}")

    # -- step 6 --
    hdr(6, "Flatten All Params")
    all_params = {}
    for section in ("scanner", "entry", "exit", "simulation"):
        if section in raw:
            for key, val in raw[section].items():
                all_params[key] = val if isinstance(val, list) else [val]
                print(f"  [{section:>10}] {key}: {all_params[key]}")

    # -- step 7 --
    hdr(7, "Partition SQL vs SIM")
    sql_params = {}
    sim_params = {}
    for key, vals in all_params.items():
        if key in SIM_KEYS_V2:
            sim_params[key] = vals
            print(f"  SIM  {key}: {vals}")
        elif key in SQL_KEYS_V2:
            sql_params[key] = vals
            print(f"  SQL  {key}: {vals}")
        else:
            # Not in either set -- defaults to SQL
            sql_params[key] = vals
            print(f"  SQL* {key}: {vals}  (not in SIM_KEYS, defaulting to SQL)")

    # -- step 8 --
    hdr(8, "Cartesian Product")
    sql_combos = list(_cartesian(sql_params))
    sim_combos = list(_cartesian(sim_params))
    total = len(sql_combos) * len(sim_combos)

    print(f"  SQL combos: {len(sql_combos)}")
    for i, c in enumerate(sql_combos):
        marker = " <-- SELECTED" if i == args.sql_idx else ""
        print(f"    [{i}] {c}{marker}")

    print(f"  SIM combos: {len(sim_combos)}")
    for i, c in enumerate(sim_combos):
        marker = " <-- SELECTED" if i == args.sim_idx else ""
        print(f"    [{i}] {c}{marker}")

    print(f"  Total configs: {total}")

    sql_cfg = sql_combos[args.sql_idx]
    sim_cfg = sim_combos[args.sim_idx]

    dump_json(dump_dir, "step_08_combos.json", {
        "sql_combos": sql_combos,
        "sim_combos": sim_combos,
        "selected_sql": {"idx": args.sql_idx, "cfg": sql_cfg},
        "selected_sim": {"idx": args.sim_idx, "cfg": sim_cfg},
    })

    if stop_at <= 0:
        print(f"\n--- Stopped after: config ---")
        return

    # ==================================================================
    # PHASE 2: SQL EXECUTION (steps 9-13)
    # ==================================================================

    # Parse exclusion filters
    exclude_symbols = set()
    if args.exclude_symbols:
        exclude_symbols = set(s.strip() for s in args.exclude_symbols.split(","))
    max_ss = args.max_signal_strength

    if args.load_entries:
        # -- CACHE LOAD: skip SQL entirely --
        hdr("9-13", f"Load entries from cache: {args.load_entries}")
        with open(args.load_entries, "rb") as f:
            all_entries = pickle.load(f)
        print(f"  Loaded {sum(len(v) for v in all_entries.values()):,} entries "
              f"across {len(all_entries)} days")
    else:
        hdr(9, "Create API Client")
        client = CetaResearch()
        print(f"  Base URL: {client.base_url}")
        print(f"  API key: {client.api_key[:8]}...{client.api_key[-4:]}")

        # -- step 10 --
        hdr(10, "Build full_sql_cfg")
        exchange_defaults = EXCHANGE_SQL_DEFAULTS.get(exchange, EXCHANGE_SQL_DEFAULTS["NSE"])
        full_sql_cfg = {
            "start_date": static["start_date"],
            "end_date": static["end_date"],
            **exchange_defaults,
            **sql_cfg,
        }
        for k, v in full_sql_cfg.items():
            print(f"  {k}: {v}")

        # -- step 11 --
        hdr(11, "Date Chunking")
        months = 3 if exchange in ("NASDAQ", "NYSE", "AMEX") else 12
        chunks = _date_chunks(static["start_date"], static["end_date"], months)
        print(f"  Chunk months: {months}")
        print(f"  Chunks: {len(chunks)}")
        for i, (cs, ce) in enumerate(chunks):
            print(f"    [{i}] {cs} -> {ce}")

        # -- step 12 --
        hdr(12, "SQL Query (sample: chunk 0)")
        sample_cfg = {**full_sql_cfg, "start_date": chunks[0][0], "end_date": chunks[0][1]}
        sample_sql = build_orb_signal_sql(sample_cfg)
        print()
        for line in sample_sql.strip().split("\n"):
            print(f"  {line}")
        dump_json(dump_dir, "step_12_sample_sql.txt", sample_sql)

        # -- step 13 --
        hdr(13, "Execute SQL (all chunks)")
        all_entries = {}
        total_rows = 0
        raw_sample = []

        for i, (cs, ce) in enumerate(chunks):
            chunk_cfg = {**full_sql_cfg, "start_date": cs, "end_date": ce}
            sql = build_orb_signal_sql(chunk_cfg)
            print(f"  chunk {i+1}/{len(chunks)}: {cs} -> {ce}", end="", flush=True)
            try:
                t0 = time.time()
                rows = client.query(sql, memory_mb=16384, threads=6, timeout=600)
                elapsed = round(time.time() - t0, 1)
                total_rows += len(rows)
                print(f" => {len(rows)} rows ({elapsed}s)")

                if i == 0 and rows:
                    raw_sample = rows[:30]

                chunk_entries = _build_entry_signals(rows)
                for date, entries in chunk_entries.items():
                    all_entries.setdefault(date, []).extend(entries)
                del rows, chunk_entries
            except Exception as e:
                print(f" => FAILED: {e}")

        print(f"\n  Total rows: {total_rows:,}")
        print(f"  Trading days: {len(all_entries)}")
        print(f"  Entry signals: {sum(len(v) for v in all_entries.values()):,}")

        subhdr("Raw row sample (first 3 rows)")
        if raw_sample:
            print(f"  Columns: {list(raw_sample[0].keys())}")
            for j, row in enumerate(raw_sample[:3]):
                print(f"\n  Row {j}:")
                for k, v in row.items():
                    print(f"    {k}: {v}")

        dump_json(dump_dir, "step_13_raw_sample.json", raw_sample[:30])

        # Save entries cache for fast re-runs
        cache_path = os.path.join(dump_dir, "entries_cache.pkl")
        with open(cache_path, "wb") as f:
            pickle.dump(all_entries, f)
        print(f"\n  Entries cached to: {cache_path}")

    # -- Apply exclusion filters --
    if exclude_symbols or max_ss:
        before = sum(len(v) for v in all_entries.values())
        for d in list(all_entries.keys()):
            filtered = []
            for e in all_entries[d]:
                if e.get("symbol") in exclude_symbols:
                    continue
                if max_ss and (e.get("signal_strength") or 0) > max_ss:
                    continue
                filtered.append(e)
            if filtered:
                all_entries[d] = filtered
            else:
                del all_entries[d]
        after = sum(len(v) for v in all_entries.values())
        removed = before - after
        filters = []
        if exclude_symbols:
            filters.append(f"symbols={exclude_symbols}")
        if max_ss:
            filters.append(f"max_ss={max_ss}")
        print(f"\n  Filters applied ({', '.join(filters)}): "
              f"{before:,} -> {after:,} entries ({removed:,} removed)")

    if stop_at <= 1:
        print(f"\n--- Stopped after: sql ---")
        return

    # ==================================================================
    # PHASE 3: ENTRY STRUCTURING (steps 14-17)
    # ==================================================================

    hdr("14-17", "Entry Signal Analysis")

    entries_per_day = {d: len(v) for d, v in all_entries.items()}
    counts = sorted(entries_per_day.values())

    subhdr("A. Entries-per-day distribution")
    if counts:
        print(f"  Days: {len(counts)}")
        print(f"  Min:    {counts[0]}")
        print(f"  P25:    {counts[len(counts)//4]}")
        print(f"  Median: {counts[len(counts)//2]}")
        print(f"  P75:    {counts[3*len(counts)//4]}")
        print(f"  Max:    {counts[-1]}")
        print(f"  Mean:   {sum(counts)/len(counts):.1f}")

        buckets = Counter()
        for c in counts:
            if c <= 1:    buckets["    1"] += 1
            elif c <= 3:  buckets["  2-3"] += 1
            elif c <= 5:  buckets["  4-5"] += 1
            elif c <= 10: buckets[" 6-10"] += 1
            elif c <= 20: buckets["11-20"] += 1
            else:         buckets["  21+"] += 1
        print(f"\n  Histogram:")
        for label in ["    1", "  2-3", "  4-5", " 6-10", "11-20", "  21+"]:
            n = buckets.get(label, 0)
            bar = "#" * min(n // max(1, len(counts) // 50), 50) if n else ""
            pct = n / len(counts) * 100
            print(f"    {label} entries/day: {n:>5} days ({pct:5.1f}%)  {bar}")

        # Critical insight for max_positions analysis
        below_max = sum(1 for c in counts if c <= 5)
        print(f"\n  Days with <= 5 entries (max_positions=5 not binding): "
              f"{below_max}/{len(counts)} ({below_max/len(counts)*100:.1f}%)")
        above_max = sum(1 for c in counts if c > 5)
        print(f"  Days with > 5 entries (max_positions=5 IS binding):    "
              f"{above_max}/{len(counts)} ({above_max/len(counts)*100:.1f}%)")

    subhdr("B. Symbol frequency (top 30)")
    symbol_counts = Counter()
    for entries in all_entries.values():
        for e in entries:
            symbol_counts[e["symbol"]] += 1
    for sym, cnt in symbol_counts.most_common(30):
        pct = cnt / sum(symbol_counts.values()) * 100
        print(f"    {sym:>10}: {cnt:>5} entries ({pct:.1f}%)")
    print(f"    {'TOTAL':>10}: {sum(symbol_counts.values()):>5} across {len(symbol_counts)} unique symbols")

    subhdr("C. Signal strength distribution")
    all_ss = []
    for entries in all_entries.values():
        for e in entries:
            ss = e.get("signal_strength")
            if ss is not None:
                all_ss.append(ss)
    if all_ss:
        ss_sorted = sorted(all_ss)
        n = len(ss_sorted)
        print(f"  Count:  {n}")
        print(f"  Min:    {ss_sorted[0]:.6f}")
        print(f"  P05:    {ss_sorted[n//20]:.6f}")
        print(f"  P25:    {ss_sorted[n//4]:.6f}")
        print(f"  Median: {ss_sorted[n//2]:.6f}")
        print(f"  P75:    {ss_sorted[3*n//4]:.6f}")
        print(f"  P95:    {ss_sorted[int(n*0.95)]:.6f}")
        print(f"  Max:    {ss_sorted[-1]:.6f}")

        outliers = [(d, e["symbol"], e.get("signal_strength"))
                    for d, entries in all_entries.items()
                    for e in entries if (e.get("signal_strength") or 0) > 1.0]
        if outliers:
            print(f"\n  WARNING: {len(outliers)} entries with signal_strength > 1.0:")
            for d, sym, ss in outliers[:10]:
                print(f"    {d} {sym} ss={ss:.4f}")
            if len(outliers) > 10:
                print(f"    ... and {len(outliers) - 10} more")

    subhdr("D. Bars per entry distribution")
    bar_counts = []
    for entries in all_entries.values():
        for e in entries:
            bar_counts.append(len(e["bars"]))
    if bar_counts:
        bc = sorted(bar_counts)
        n = len(bc)
        print(f"  Min:    {bc[0]}")
        print(f"  P25:    {bc[n//4]}")
        print(f"  Median: {bc[n//2]}")
        print(f"  P75:    {bc[3*n//4]}")
        print(f"  Max:    {bc[-1]}")
        few = sum(1 for b in bc if b <= 5)
        if few:
            print(f"  Entries with <= 5 bars: {few} (late-day entries, almost no room for exit)")

    subhdr("E. Entry price vs OR high (sanity check)")
    ratios = []
    for entries in all_entries.values():
        for e in entries:
            if e.get("or_high") and e["or_high"] > 0:
                ratios.append(e["entry_price"] / e["or_high"])
    if ratios:
        r_sorted = sorted(ratios)
        n = len(r_sorted)
        print(f"  entry_price / or_high:")
        print(f"  Min:    {r_sorted[0]:.6f}")
        print(f"  Median: {r_sorted[n//2]:.6f}")
        print(f"  Max:    {r_sorted[-1]:.6f}")
        print(f"  (should all be > 1.0 since entry requires close > or_high)")
        below_one = sum(1 for r in ratios if r <= 1.0)
        if below_one:
            print(f"  WARNING: {below_one} entries with entry_price <= or_high")

    subhdr("F. Sample entries (first day)")
    first_day = sorted(all_entries.keys())[0] if all_entries else None
    if first_day:
        print(f"  Date: {first_day}")
        day_entries = all_entries[first_day]
        for i, e in enumerate(day_entries[:8]):
            print(f"\n  Entry {i}: {e['symbol']}")
            print(f"    entry_bar={e['entry_bar']}  entry_price=${e['entry_price']:.4f}")
            print(f"    or_high=${e.get('or_high', 0):.4f}  or_low=${e.get('or_low', 0):.4f}  "
                  f"or_range=${e.get('or_range', 0):.4f}")
            print(f"    signal_strength={e.get('signal_strength', 0):.6f}  "
                  f"bench_ret={e.get('bench_ret', 0):.6f}")
            print(f"    bars: {len(e['bars'])} "
                  f"(bar {e['bars'][0]['bar_num']} to {e['bars'][-1]['bar_num']})")
        if len(day_entries) > 8:
            print(f"  ... and {len(day_entries) - 8} more entries")

    dump_json(dump_dir, "step_17_entry_stats.json", {
        "total_days": len(all_entries),
        "total_entries": sum(len(v) for v in all_entries.values()),
        "entries_per_day_histogram": dict(Counter(entries_per_day.values()).most_common()),
        "symbol_frequency_top50": dict(symbol_counts.most_common(50)),
        "signal_strength_percentiles": {
            "p05": ss_sorted[len(ss_sorted)//20],
            "p25": ss_sorted[len(ss_sorted)//4],
            "p50": ss_sorted[len(ss_sorted)//2],
            "p75": ss_sorted[3*len(ss_sorted)//4],
            "p95": ss_sorted[int(len(ss_sorted)*0.95)],
        } if all_ss else {},
    })

    if stop_at <= 2:
        print(f"\n--- Stopped after: entries ---")
        return

    # ==================================================================
    # PHASE 4: SIMULATION (steps 18-21)
    # ==================================================================

    hdr("18-19", "Initialize Simulation")

    full_sim_cfg = {
        "initial_capital": initial_capital,
        "exchange": exchange,
        **sim_cfg,
    }
    print(f"  Full SIM config:")
    for k, v in full_sim_cfg.items():
        print(f"    {k}: {v}")

    charges_fn = _get_charges_fn(exchange)
    sizing_type = full_sim_cfg.get("sizing_type", "fixed")
    max_positions = full_sim_cfg["max_positions"]
    max_per_instrument = full_sim_cfg.get("max_positions_per_instrument", max_positions)
    max_order_value = full_sim_cfg.get("max_order_value")
    ranking_type = full_sim_cfg.get("ranking_type", "signal_strength")
    ranking_window = full_sim_cfg.get("ranking_window_days", 180)
    payout_cfg = full_sim_cfg.get("payout")

    exit_config = {
        "target_pct": full_sim_cfg["target_pct"],
        "stop_pct": full_sim_cfg["stop_pct"],
        "trailing_stop_pct": full_sim_cfg.get("trailing_stop_pct", 0),
        "min_hold_bars": full_sim_cfg.get("min_hold_bars", 0),
        "use_bar_hilo": full_sim_cfg.get("use_bar_hilo", False),
        "eod_buffer_bars": full_sim_cfg.get("eod_buffer_bars", 30),
        "time_stop_bars": full_sim_cfg.get("time_stop_bars", 0),
        "use_atr_stop": full_sim_cfg.get("use_atr_stop", False),
        "atr_multiplier": full_sim_cfg.get("atr_multiplier", 1.0),
        "exit_reentry_range": full_sim_cfg.get("exit_reentry_range", False),
    }
    print(f"\n  Exit config:")
    for k, v in exit_config.items():
        print(f"    {k}: {v}")

    sample_ov = full_sim_cfg.get("order_value", 50000)
    sample_charges = charges_fn(sample_ov)
    print(f"\n  Charges fn: {charges_fn.__name__}")
    print(f"  Charges at OV=${sample_ov:,}: ${sample_charges:.4f}")
    print(f"  Charges as % of OV: {sample_charges/sample_ov*100:.4f}%")

    # State
    margin = float(initial_capital)
    trade_count = 0
    win_count = 0
    symbol_pnl_history = defaultdict(list)
    daily_returns = []
    bench_returns = []
    day_wise_log = []
    trade_log = []
    total_withdrawn = 0.0
    days_elapsed = 0

    # Payout
    next_payout_day = None
    if payout_cfg:
        lockup = payout_cfg.get("lockup_days", 0)
        interval = payout_cfg.get("interval_days", 30)
        next_payout_day = max(lockup, interval)
        print(f"\n  Payout: {payout_cfg}")
        print(f"  First payout at day: {next_payout_day}")

    # -- step 20-21: day loop --
    hdr("20-21", f"Day-by-Day Simulation ({len(all_entries)} days, tracing {args.trace_days})")

    sorted_dates = sorted(all_entries.keys())
    print(f"  Date range: {sorted_dates[0]} -> {sorted_dates[-1]}")

    day_summaries = []

    for day_idx, d in enumerate(sorted_dates):
        day_entries = all_entries[d]
        traced = day_idx < args.trace_days

        if traced:
            print(f"\n  {'='*58}")
            print(f"  DAY {day_idx+1}/{len(sorted_dates)}: {d}  |  margin=${margin:,.2f}  "
                  f"|  entries={len(day_entries)}")
            print(f"  {'='*58}")

        # -- 21a: rank --
        symbol_scores = {}
        if ranking_type == "top_performer":
            symbol_scores = _compute_symbol_scores(symbol_pnl_history, d, ranking_window)
        ranked = _rank_entries(day_entries, ranking_type, symbol_scores)

        if traced:
            print(f"\n  [21a] RANKING ({ranking_type})  |  {len(ranked)} candidates")
            if ranking_type == "top_performer":
                nonzero = {k: round(v, 3) for k, v in symbol_scores.items() if v != 0}
                if nonzero:
                    print(f"         symbol scores: {nonzero}")
            for i, e in enumerate(ranked[:min(max_positions + 3, len(ranked))]):
                sel = ">>>" if i < max_positions else "   "
                print(f"    {sel} #{i+1} {e['symbol']:>8}  "
                      f"ss={e.get('signal_strength', 0):.4f}  "
                      f"entry_bar={e.get('entry_bar'):>3}  "
                      f"price=${e.get('entry_price', 0):.2f}")
            if len(ranked) > max_positions + 3:
                print(f"    ... {len(ranked) - max_positions - 3} more")

        # -- 21b: select --
        selected = ranked[:max_positions]

        # -- 21c: order value --
        ov = _compute_order_value(sizing_type, full_sim_cfg, margin, max_positions)
        if max_order_value:
            ov = min(ov, max_order_value)

        if traced:
            print(f"\n  [21c] ORDER VALUE: ${ov:,.2f}  (sizing={sizing_type})")

        if ov <= 0:
            daily_returns.append(0.0)
            bench_returns.append(day_entries[0].get("bench_ret") or 0.0)
            day_wise_log.append({
                "log_date_epoch": _date_to_epoch(d),
                "invested_value": 0, "margin_available": margin,
            })
            if traced:
                print(f"    SKIP DAY: ov <= 0")
            day_summaries.append({"date": d, "trades": 0, "pnl": 0, "margin": margin})
            continue

        # -- 21d: charges --
        charges = charges_fn(ov)

        if traced:
            print(f"  [21d] CHARGES: ${charges:.4f} per trade")

        margin_used = 0.0
        instrument_counts = {}
        daily_pnl = 0.0
        day_trade_details = []

        # -- 21e-21i: execute trades --
        for entry_idx, entry in enumerate(selected):
            entry_price = entry.get("entry_price")
            symbol = entry.get("symbol")

            if not entry_price or entry_price <= 0:
                if traced:
                    print(f"\n  [21e] TRADE {entry_idx+1}: {symbol} -- SKIP (no price)")
                continue

            if instrument_counts.get(symbol, 0) >= max_per_instrument:
                if traced:
                    print(f"\n  [21e] TRADE {entry_idx+1}: {symbol} -- SKIP (instrument limit)")
                continue

            if margin - margin_used < ov:
                if traced:
                    print(f"\n  [21e] TRADE {entry_idx+1}: {symbol} -- BREAK "
                          f"(margin ${margin-margin_used:,.2f} < ov ${ov:,.2f})")
                break

            margin_used += ov

            # -- 21f: resolve exit --
            exit_result = _resolve_exit(entry, exit_config)
            exit_price = exit_result["exit_price"]

            pnl = (exit_price - entry_price) / entry_price * ov - charges
            daily_pnl += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1

            instrument_counts[symbol] = instrument_counts.get(symbol, 0) + 1
            trade_pnl_pct = (exit_price - entry_price) / entry_price * 100
            symbol_pnl_history[symbol].append((d, trade_pnl_pct))

            trade_rec = {
                "symbol": symbol, "trade_date": d,
                "entry_bar": entry.get("entry_bar"),
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "exit_type": exit_result["exit_type"],
                "exit_bar": exit_result["exit_bar"],
                "pnl": round(pnl, 2),
                "pnl_pct": round(trade_pnl_pct, 4),
                "charges": round(charges, 2),
                "order_value": round(ov, 2),
                "signal_strength": entry.get("signal_strength"),
            }
            trade_log.append(trade_rec)
            day_trade_details.append(trade_rec)

            if traced:
                or_low = entry.get("or_low", 0)
                or_high_val = entry.get("or_high", 0)
                atr_14 = entry.get("atr_14")
                rvol_val = entry.get("rvol")
                tgt_pct = exit_config["target_pct"]
                target_p = entry_price * (1 + tgt_pct) if tgt_pct > 0 else float("inf")

                if exit_config.get("use_atr_stop") and atr_14:
                    fixed_stop = entry_price - exit_config.get("atr_multiplier", 1.0) * atr_14
                    stop_label = f"ATR stop (atr={atr_14:.2f} x {exit_config.get('atr_multiplier', 1.0)})"
                else:
                    fixed_stop = entry_price * (1 - exit_config["stop_pct"])
                    stop_label = f"pct stop ({exit_config['stop_pct']*100}%)"

                print(f"\n  [21f] TRADE {entry_idx+1}: {symbol}")
                print(f"    entry_bar={entry.get('entry_bar')}  "
                      f"entry=${entry_price:.4f}  "
                      f"or_high=${or_high_val:.4f}  "
                      f"or_low=${or_low:.4f}")
                extras = []
                if rvol_val is not None:
                    extras.append(f"rvol={rvol_val:.2f}")
                if atr_14 is not None:
                    extras.append(f"atr_14={atr_14:.2f}")
                if extras:
                    print(f"    {' '.join(extras)}")
                target_str = f"${target_p:.4f} (+{tgt_pct*100}%)" if tgt_pct > 0 else "DISABLED (trail-only)"
                print(f"    target={target_str}  "
                      f"fixed_stop=${fixed_stop:.4f} ({stop_label})")
                if exit_config.get("time_stop_bars", 0) > 0:
                    print(f"    time_stop={exit_config['time_stop_bars']} bars")
                if exit_config.get("exit_reentry_range"):
                    print(f"    reentry_exit: close < or_high (${or_high_val:.4f})")
                print(f"    EXIT => bar={exit_result['exit_bar']}  "
                      f"price=${exit_price:.4f}  type={exit_result['exit_type']}")
                print(f"    return={trade_pnl_pct:+.4f}%  "
                      f"pnl=${pnl:+.2f}  "
                      f"{'WIN' if pnl > 0 else 'LOSS'}")

                # Bar-by-bar trace
                bars = entry["bars"]
                if len(bars) > 1:
                    entry_bar_num = bars[0]["bar_num"]
                    last_bar_num = bars[-1]["bar_num"]
                    eod_buf = exit_config["eod_buffer_bars"]
                    cutoff = last_bar_num - eod_buf
                    time_stop_n = exit_config.get("time_stop_bars", 0)
                    reentry_on = exit_config.get("exit_reentry_range", False)
                    print(f"\n    Bar trace (last_bar={last_bar_num}, "
                          f"cutoff={cutoff}, eod_buffer={eod_buf}"
                          f"{f', time_stop={time_stop_n}' if time_stop_n else ''}):")
                    highest_trace = entry_price
                    trace_stopped = False

                    for bi, bar in enumerate(bars[1:], 1):
                        if bar["bar_num"] > cutoff:
                            print(f"      bar {bar['bar_num']:>3}: -- past cutoff ({cutoff}), force EOD exit --")
                            trace_stopped = True
                            break

                        # Time stop check
                        if time_stop_n > 0 and (bar["bar_num"] - entry_bar_num) >= time_stop_n:
                            print(f"      bar {bar['bar_num']:>3}: "
                                  f"O={bar['open']:>9.2f} H={bar['high']:>9.2f} "
                                  f"L={bar['low']:>9.2f} C={bar['close']:>9.2f} | "
                                  f"[TIME_STOP at {time_stop_n} bars]")
                            trace_stopped = True
                            break

                        p_h = bar["high"] if exit_config["use_bar_hilo"] else bar["close"]
                        p_l = bar["low"] if exit_config["use_bar_hilo"] else bar["close"]
                        if p_h > highest_trace:
                            highest_trace = p_h

                        if exit_config["trailing_stop_pct"] > 0:
                            trail = highest_trace * (1 - exit_config["trailing_stop_pct"])
                            stop_now = max(fixed_stop, trail)
                        else:
                            stop_now = fixed_stop

                        in_hold = (bar["bar_num"] - entry_bar_num) <= exit_config["min_hold_bars"]
                        tgt_hit = p_h >= target_p
                        stp_hit = p_l <= stop_now

                        flags = ""
                        if in_hold:
                            flags = "[HOLD]"
                        elif tgt_hit and stp_hit:
                            flags = "[TGT+STP=>STP]" if exit_config["use_bar_hilo"] else "[TGT+STP=>CLS]"
                        elif tgt_hit:
                            flags = "[TARGET]"
                        elif stp_hit:
                            flags = "[STOP]"
                        elif reentry_on and bar["close"] < or_high_val and not in_hold:
                            flags = "[REENTRY]"

                        print(f"      bar {bar['bar_num']:>3}: "
                              f"O={bar['open']:>9.2f} H={bar['high']:>9.2f} "
                              f"L={bar['low']:>9.2f} C={bar['close']:>9.2f} | "
                              f"peak={highest_trace:>9.2f} stop={stop_now:>9.2f} {flags}")

                        if flags in ("[TARGET]", "[STOP]", "[TGT+STP=>STP]",
                                     "[TGT+STP=>CLS]", "[REENTRY]"):
                            trace_stopped = True
                            break

                        if bi >= 20:
                            remaining_bars = cutoff - bar["bar_num"]
                            print(f"      ... {remaining_bars} more bars until cutoff")
                            trace_stopped = True
                            break

                    if not trace_stopped and len(bars) > 1:
                        print(f"      -> no signal exit, EOD at cutoff bar")

        # -- 21j: end of day --
        daily_ret = daily_pnl / margin if margin > 0 else 0.0
        margin += daily_pnl
        days_elapsed += 1

        if payout_cfg and next_payout_day and days_elapsed >= next_payout_day:
            withdrawal = _compute_payout(payout_cfg, margin)
            margin -= withdrawal
            total_withdrawn += withdrawal
            next_payout_day += payout_cfg.get("interval_days", 30)
            if traced:
                print(f"\n  PAYOUT: ${withdrawal:,.2f} withdrawn (total: ${total_withdrawn:,.2f})")

        daily_returns.append(daily_ret)
        bench_returns.append(day_entries[0].get("bench_ret") or 0.0)
        day_wise_log.append({
            "log_date_epoch": _date_to_epoch(d),
            "invested_value": 0, "margin_available": margin,
        })

        n_trades = len(day_trade_details)
        day_summaries.append({
            "date": d, "trades": n_trades,
            "pnl": round(daily_pnl, 2), "ret": round(daily_ret, 6),
            "margin": round(margin, 2),
        })

        if traced:
            print(f"\n  [21j] END OF DAY")
            print(f"    trades: {n_trades}  "
                  f"daily_pnl: ${daily_pnl:+,.2f}  "
                  f"daily_ret: {daily_ret*100:+.4f}%")
            print(f"    margin: ${margin:,.2f}  "
                  f"cumulative: {trade_count} trades ({win_count}W/{trade_count-win_count}L)")

    # -- simulation summary --
    subhdr("SIMULATION SUMMARY")
    print(f"  Days traded:      {len(sorted_dates)}")
    print(f"  Total trades:     {trade_count}")
    if trade_count > 0:
        print(f"  Wins:             {win_count} ({win_count/trade_count*100:.1f}%)")
        print(f"  Losses:           {trade_count - win_count} ({(trade_count-win_count)/trade_count*100:.1f}%)")
    print(f"  Starting capital: ${initial_capital:,.2f}")
    print(f"  Ending margin:    ${margin:,.2f}")
    print(f"  Net P&L:          ${margin - initial_capital:+,.2f}")

    # Exit type breakdown
    if trade_log:
        subhdr("Exit type breakdown")
        exit_types = Counter(t["exit_type"] for t in trade_log)
        for et, cnt in exit_types.most_common():
            et_trades = [t for t in trade_log if t["exit_type"] == et]
            avg_pnl = sum(t["pnl"] for t in et_trades) / cnt
            avg_pct = sum(t["pnl_pct"] for t in et_trades) / cnt
            total_pnl = sum(t["pnl"] for t in et_trades)
            wins = sum(1 for t in et_trades if t["pnl"] > 0)
            print(f"    {et:>10}: {cnt:>5} trades ({cnt/trade_count*100:5.1f}%)  "
                  f"winrate={wins/cnt*100:5.1f}%  "
                  f"avg_ret={avg_pct:+6.2f}%  "
                  f"total_pnl=${total_pnl:+,.0f}")

        # Top losers & winners
        subhdr("Top 10 losers")
        for t in sorted(trade_log, key=lambda t: t["pnl"])[:10]:
            print(f"    {t['trade_date']} {t['symbol']:>8} "
                  f"entry=${t['entry_price']:>8.2f} exit=${t['exit_price']:>8.2f} "
                  f"pnl=${t['pnl']:>+9.2f} ({t['pnl_pct']:>+7.2f}%) [{t['exit_type']}]")

        subhdr("Top 10 winners")
        for t in sorted(trade_log, key=lambda t: t["pnl"], reverse=True)[:10]:
            print(f"    {t['trade_date']} {t['symbol']:>8} "
                  f"entry=${t['entry_price']:>8.2f} exit=${t['exit_price']:>8.2f} "
                  f"pnl=${t['pnl']:>+9.2f} ({t['pnl_pct']:>+7.2f}%) [{t['exit_type']}]")

        # Per-symbol P&L (top/bottom)
        subhdr("Per-symbol P&L (top 10 and bottom 10)")
        sym_pnl = defaultdict(lambda: {"pnl": 0, "count": 0, "wins": 0})
        for t in trade_log:
            s = t["symbol"]
            sym_pnl[s]["pnl"] += t["pnl"]
            sym_pnl[s]["count"] += 1
            if t["pnl"] > 0:
                sym_pnl[s]["wins"] += 1
        sym_sorted = sorted(sym_pnl.items(), key=lambda x: x[1]["pnl"])

        print(f"  Bottom 10:")
        for sym, d in sym_sorted[:10]:
            wr = d["wins"] / d["count"] * 100
            print(f"    {sym:>10}: ${d['pnl']:>+10,.0f}  "
                  f"{d['count']:>4} trades  wr={wr:.0f}%")
        print(f"\n  Top 10:")
        for sym, d in sym_sorted[-10:]:
            wr = d["wins"] / d["count"] * 100
            print(f"    {sym:>10}: ${d['pnl']:>+10,.0f}  "
                  f"{d['count']:>4} trades  wr={wr:.0f}%")

    dump_json(dump_dir, "step_21_trade_log.json", trade_log)
    dump_json(dump_dir, "step_21_day_summaries.json", day_summaries)
    dump_json(dump_dir, "step_21_day_wise_log.json", day_wise_log)

    if stop_at <= 3:
        print(f"\n--- Stopped after: sim ---")
        return

    # ==================================================================
    # PHASE 5: METRICS (steps 22-24)
    # ==================================================================

    hdr("22-24", "Metrics")

    if len(daily_returns) < 2:
        print("  < 2 active days, skipping metrics")
    else:
        metrics = compute_metrics(
            daily_returns, bench_returns,
            periods_per_year=252,
            risk_free_rate=risk_free_rate,
        )

        port = metrics["portfolio"]
        comp = metrics["comparison"]

        subhdr("Portfolio metrics")
        for k, v in port.items():
            if v is None:
                print(f"    {k:<30}: N/A")
            elif "ratio" in k or "skew" in k or "kurt" in k or "consecutive" in k:
                print(f"    {k:<30}: {v:.4f}")
            elif isinstance(v, int):
                print(f"    {k:<30}: {v}")
            else:
                print(f"    {k:<30}: {v*100:+.4f}%")

        subhdr("Comparison metrics (vs equal-weight benchmark)")
        for k, v in comp.items():
            if v is None:
                print(f"    {k:<30}: N/A")
            elif "ratio" in k or "beta" in k:
                print(f"    {k:<30}: {v:.4f}")
            elif "capture" in k:
                print(f"    {k:<30}: {v:.4f}")
            else:
                print(f"    {k:<30}: {v*100:+.4f}%")

        # Equity curve
        subhdr("Equity curve checkpoints")
        cum = 1.0
        peak = 1.0
        checkpoints = [0, len(daily_returns)//4, len(daily_returns)//2,
                       3*len(daily_returns)//4, len(daily_returns)-1]
        for ci in checkpoints:
            if ci >= len(daily_returns):
                continue
            cum_at = 1.0
            for r in daily_returns[:ci+1]:
                cum_at *= (1 + r)
            print(f"    Day {ci+1:>5} ({sorted_dates[ci]}): "
                  f"cumulative={cum_at:.4f} ({(cum_at-1)*100:+.2f}%)")

        dump_json(dump_dir, "step_24_metrics.json", metrics)

    # ==================================================================
    # DONE
    # ==================================================================

    print(f"\n{DIV}")
    print(f"  DEBUG COMPLETE")
    print(f"  Output: {dump_dir}/")
    for fn in sorted(os.listdir(dump_dir)):
        size = os.path.getsize(os.path.join(dump_dir, fn))
        print(f"    {fn} ({size:,} bytes)")
    print(DIV)


if __name__ == "__main__":
    main()

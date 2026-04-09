#!/usr/bin/env python3
"""Compare cascade signals from engine vs standalone generators.

Uses the same bhavcopy data for both. Identifies signal-level differences.
"""

import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

import polars as pl

from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark, compute_regime_epochs,
    CetaResearch,
)
from scripts.momentum_breakout_v3 import compute_cascade_entries
from engine.data_provider import BhavcopyDataProvider
from engine.signals.momentum_cascade import MomentumCascadeSignalGenerator
from engine.config_loader import load_config
from scripts.trace_engine_vs_standalone import precompute_exits

START_EPOCH = 1262304000
END_EPOCH = 1773878400
EXCHANGE = "NSE"


def epoch_to_date(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    cr = CetaResearch()

    print("=" * 80)
    print("  SIGNAL COMPARISON: Engine vs Standalone")
    print("=" * 80)

    # ── 1. STANDALONE SIGNALS ──
    print("\n--- 1. Standalone Signal Generator ---")
    t0 = time.time()
    price_data = fetch_universe(cr, EXCHANGE, START_EPOCH, END_EPOCH,
                                source="bhavcopy", turnover_threshold=70_000_000)
    benchmark = fetch_benchmark(cr, "NIFTYBEES", EXCHANGE, START_EPOCH, END_EPOCH,
                                warmup_days=250, source="bhavcopy")
    regime_50 = compute_regime_epochs(benchmark, 50)

    all_entries = compute_cascade_entries(price_data, 42, 126, 0.02, 0.20,
                                         start_epoch=START_EPOCH)
    standalone_signals = [(e["symbol"], e["entry_epoch"], e["entry_price"]) for e in all_entries
                          if e["epoch"] in regime_50]
    standalone_set = {(sym, ep) for sym, ep, _ in standalone_signals}
    standalone_map = {(sym, ep): px for sym, ep, px in standalone_signals}
    print(f"  {len(standalone_signals)} signals, {len(set(s[0] for s in standalone_signals))} symbols")
    print(f"  Time: {time.time()-t0:.0f}s")

    # ── 2. ENGINE SIGNALS ──
    print("\n--- 2. Engine Signal Generator ---")
    t0 = time.time()
    provider = BhavcopyDataProvider(turnover_threshold=70_000_000, price_threshold=50)
    df_tick = provider.fetch_ohlcv(
        exchanges=["NSE"], start_epoch=START_EPOCH, end_epoch=END_EPOCH,
        prefetch_days=600,
    )

    config = load_config("strategies/momentum_cascade/config_trace.yaml")
    context = {
        **config,
        "start_margin": 10_000_000,
        "start_epoch": START_EPOCH,
        "end_epoch": END_EPOCH,
        "prefetch_days": 600,
        "total_exit_configs": 1,
    }

    gen = MomentumCascadeSignalGenerator()
    df_orders = gen.generate_orders(context, df_tick)

    engine_signals = []
    for row in df_orders.to_dicts():
        sym = row["instrument"].replace("NSE:", "")
        engine_signals.append((sym, row["entry_epoch"], row["entry_price"],
                               row["exit_epoch"], row["exit_price"]))
    engine_set = {(sym, ep) for sym, ep, _, _, _ in engine_signals}
    engine_map = {(sym, ep): (px, ex_ep, ex_px) for sym, ep, px, ex_ep, ex_px in engine_signals}
    print(f"  {len(engine_signals)} signals, {len(set(s[0] for s in engine_signals))} symbols")
    print(f"  Time: {time.time()-t0:.0f}s")

    # ── 3. COMPARISON ──
    print(f"\n{'='*80}")
    print("  SIGNAL COMPARISON")
    print(f"{'='*80}")

    shared = standalone_set & engine_set
    standalone_only = standalone_set - engine_set
    engine_only = engine_set - standalone_set

    print(f"\n  Shared signals:      {len(shared)}")
    print(f"  Standalone only:     {len(standalone_only)}")
    print(f"  Engine only:         {len(engine_only)}")

    # Check entry price matches for shared signals
    price_matches = 0
    price_diffs = []
    for sym, ep in shared:
        s_px = standalone_map[(sym, ep)]
        e_px = engine_map[(sym, ep)][0]
        if abs(s_px - e_px) < 0.01:
            price_matches += 1
        else:
            price_diffs.append((sym, ep, s_px, e_px))

    print(f"\n  Shared signal entry price match: {price_matches}/{len(shared)}")
    if price_diffs:
        print(f"  Price mismatches: {len(price_diffs)}")
        for sym, ep, s_px, e_px in price_diffs[:10]:
            print(f"    {sym:<14} {epoch_to_date(ep)} standalone={s_px:.2f} engine={e_px:.2f} "
                  f"diff={e_px-s_px:+.2f}")

    # Standalone-only: which symbols?
    if standalone_only:
        s_only_syms = {s for s, _ in standalone_only}
        print(f"\n  Standalone-only signals by symbol ({len(s_only_syms)} unique):")
        sym_counts = {}
        for sym, ep in standalone_only:
            sym_counts[sym] = sym_counts.get(sym, 0) + 1
        for sym, cnt in sorted(sym_counts.items(), key=lambda x: -x[1])[:15]:
            print(f"    {sym:<14} {cnt} signals")

    # Engine-only: which symbols?
    if engine_only:
        e_only_syms = {s for s, _ in engine_only}
        print(f"\n  Engine-only signals by symbol ({len(e_only_syms)} unique):")
        sym_counts = {}
        for sym, ep in engine_only:
            sym_counts[sym] = sym_counts.get(sym, 0) + 1
        for sym, cnt in sorted(sym_counts.items(), key=lambda x: -x[1])[:15]:
            print(f"    {sym:<14} {cnt} signals")

    # Check if standalone-only symbols exist in engine data
    engine_instruments = set(df_tick["symbol"].unique().to_list())
    standalone_syms = set(s for s, _ in standalone_set)
    engine_syms = set(s for s, _ in engine_set)

    missing_from_engine_data = standalone_syms - engine_instruments
    print(f"\n  Standalone symbols missing from engine data: {len(missing_from_engine_data)}")
    if missing_from_engine_data:
        print(f"    {sorted(missing_from_engine_data)[:20]}")

    # Check if signals are just on different DAYS for the same symbols
    shared_syms_both = standalone_syms & engine_syms
    shared_syms_s_only = {s for s, _ in standalone_only if s in engine_syms}
    print(f"\n  Standalone-only signals for symbols that ALSO appear in engine: "
          f"{len(shared_syms_s_only)} symbols")

    # Time distribution of differences
    if standalone_only:
        s_only_years = {}
        for _, ep in standalone_only:
            year = datetime.fromtimestamp(ep, tz=timezone.utc).year
            s_only_years[year] = s_only_years.get(year, 0) + 1
        print(f"\n  Standalone-only signals by year:")
        for year in sorted(s_only_years):
            print(f"    {year}: {s_only_years[year]}")

    if engine_only:
        e_only_years = {}
        for _, ep in engine_only:
            year = datetime.fromtimestamp(ep, tz=timezone.utc).year
            e_only_years[year] = e_only_years.get(year, 0) + 1
        print(f"\n  Engine-only signals by year:")
        for year in sorted(e_only_years):
            print(f"    {year}: {e_only_years[year]}")

    # Pre-computed exit comparison for shared signals
    print(f"\n{'='*80}")
    print("  EXIT COMPARISON (shared signals)")
    print(f"{'='*80}")

    # Check exit dates for shared signals
    # We need to pre-compute exits for standalone signals to compare
    shared_entries = [e for e in all_entries
                      if e["epoch"] in regime_50
                      and (e["symbol"], e["entry_epoch"]) in shared]
    standalone_exits = precompute_exits(shared_entries, price_data, 13, 504)
    s_exit_map = {(e["symbol"], e["entry_epoch"]): (e["exit_epoch"], e["exit_price"])
                  for e in standalone_exits}

    same_exit = 0
    diff_exit = 0
    exit_diffs_list = []
    for sym, ep in shared:
        if (sym, ep) not in s_exit_map:
            continue
        s_ex_ep, s_ex_px = s_exit_map[(sym, ep)]
        e_ex_ep, e_ex_px = engine_map[(sym, ep)][1], engine_map[(sym, ep)][2]
        diff_days = (e_ex_ep - s_ex_ep) / 86400
        if abs(diff_days) < 1.5:
            same_exit += 1
        else:
            diff_exit += 1
            exit_diffs_list.append((sym, ep, s_ex_ep, e_ex_ep, diff_days,
                                    s_ex_px, e_ex_px))

    print(f"\n  Same exit date (±1d): {same_exit}")
    print(f"  Different exit date:  {diff_exit}")

    if exit_diffs_list:
        exit_diffs_list.sort(key=lambda x: abs(x[4]), reverse=True)
        print(f"\n  Top 15 exit divergences:")
        print(f"    {'Symbol':<14} {'Entry':<11} {'S_Exit':<11} {'E_Exit':<11} {'Diff':>7}")
        for sym, ep, s_ex, e_ex, diff, s_px, e_px in exit_diffs_list[:15]:
            print(f"    {sym:<14} {epoch_to_date(ep):<11} {epoch_to_date(s_ex):<11} "
                  f"{epoch_to_date(e_ex):<11} {diff:>+6.0f}d")


if __name__ == "__main__":
    main()

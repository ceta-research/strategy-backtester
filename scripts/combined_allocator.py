#!/usr/bin/env python3
"""Combined Portfolio Allocator: Quality Dip-Buy + Momentum Dip-Buy.

Strategy 4 from NEXT_STRATEGIES_PLAN.md.

Thesis: Quality dip-buy and momentum-dip catch different stocks (33% overlap).
Running both on shared capital diversifies drawdown exposure. The goal is to
reduce MDD below -23.3% (current champion) while maintaining 23%+ CAGR.

How it works:
1. Compute quality-only dip entries (no momentum filter)
2. Compute momentum-dip entries (quality + momentum intersection)
3. Tag each entry with source, merge, deduplicate (same symbol+epoch -> keep momentum)
4. Feed merged entries to simulate_portfolio() on shared capital
5. Unified exit logic (10% TSL + 504d max hold)

Current champion: Calmar 1.01, +23.7% CAGR, -23.3% MDD
(63d momentum, top 30%, 5% dip, D/E<1.0, 10 positions, fixed 10% TSL)

Supports NSE (native data) and US (FMP data) via --market flag.
"""

import sys
import os
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark, fetch_sector_map,
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)

STRATEGY_NAME = "combined_allocator"

# Fixed params from champion config
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25
TSL_PCT = 10
MOMENTUM_LOOKBACK = 63
MOMENTUM_PERCENTILE = 0.30
DIP_THRESHOLD_PCT = 5


def intersect_universes(quality_universe, momentum_universe):
    """Intersect quality and momentum universes epoch-by-epoch."""
    combined = {}
    all_epochs = set(quality_universe.keys()) | set(momentum_universe.keys())
    for epoch in all_epochs:
        q = quality_universe.get(epoch, set())
        m = momentum_universe.get(epoch, set())
        intersection = q & m
        if intersection:
            combined[epoch] = intersection

    pool_sizes = [len(v) for v in combined.values() if v]
    avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
    print(f"  Combined universe: {len(combined)} epochs, avg pool={avg_pool:.0f} stocks")
    return combined


def merge_entries(quality_entries, momentum_entries):
    """Merge two entry lists, deduplicating by (symbol, entry_epoch).

    If the same stock has a dip signal from both sources on the same day,
    keep the momentum version (higher-conviction signal).

    Returns merged list sorted by entry_epoch.
    """
    # Index momentum entries for fast lookup
    momentum_keys = set()
    for e in momentum_entries:
        momentum_keys.add((e["symbol"], e["entry_epoch"]))

    # Add all momentum entries
    merged = list(momentum_entries)

    # Add quality-only entries that don't overlap with momentum
    added = 0
    skipped = 0
    for e in quality_entries:
        key = (e["symbol"], e["entry_epoch"])
        if key in momentum_keys:
            skipped += 1
        else:
            merged.append(e)
            added += 1

    merged.sort(key=lambda x: x["entry_epoch"])

    print(f"  Merged entries: {len(merged)} total "
          f"({len(momentum_entries)} momentum + {added} quality-only, "
          f"{skipped} overlaps removed)")
    return merged


def main():
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    if market == "nse":
        exchange = "NSE"
        start_epoch = 1262304000   # 2010-01-01
        end_epoch = 1773878400     # 2026-03-19
        benchmark_sym = "NIFTYBEES"
        capital = 10_000_000       # 1 Cr
        description = ("Combined allocator on NSE: quality dip-buy + momentum dip-buy "
                       "on shared capital pool.")
    elif market == "us":
        exchange = "US"
        start_epoch = 1104537600   # 2005-01-01
        end_epoch = 1773878400     # 2026-03-19
        benchmark_sym = "SPY"
        capital = 10_000_000       # $10M
        description = ("Combined allocator on US: quality dip-buy + momentum dip-buy "
                       "on shared capital pool.")
    else:
        print(f"Unknown market: {market}. Use --market nse or --market us")
        return

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME} ({market.upper()}): fetching data")
    print("=" * 80)

    print(f"\nFetching {exchange} universe...")
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch)
    if not price_data:
        print("No data. Aborting.")
        return

    print(f"\nFetching {benchmark_sym} benchmark...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250)

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    print("\nFetching sector map...")
    sector_map = fetch_sector_map(cr, exchange)

    # ── Compute universes ──

    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    print(f"\nComputing momentum universe ({MOMENTUM_LOOKBACK}d, top {MOMENTUM_PERCENTILE*100:.0f}%)...")
    momentum_universe = compute_momentum_universe(
        price_data, MOMENTUM_LOOKBACK, MOMENTUM_PERCENTILE,
        rescreen_days=63, start_epoch=start_epoch)

    # ── Compute entry sets ──

    # Quality-only entries (no momentum filter)
    print("\n--- Quality-Only Entries ---")
    print("Computing quality dip entries...")
    quality_entries_raw = compute_dip_entries(
        price_data, quality_universe, PEAK_LOOKBACK,
        DIP_THRESHOLD_PCT / 100.0, start_epoch=start_epoch)

    # Momentum-dip entries (quality + momentum intersection)
    print("\n--- Momentum-Dip Entries ---")
    momentum_quality_universe = intersect_universes(quality_universe, momentum_universe)
    momentum_entries_raw = compute_dip_entries(
        price_data, momentum_quality_universe, PEAK_LOOKBACK,
        DIP_THRESHOLD_PCT / 100.0, start_epoch=start_epoch)

    # ── Build sweep ──

    # Sweep axes: D/E threshold x max_positions x max_per_sector
    de_values = [0, 1.0]
    position_values = [10, 15, 20]
    sector_values = [0, 2, 3]  # 0 = no limit

    all_configs = []
    for de, pos, sec in product(de_values, position_values, sector_values):
        all_configs.append({
            "group": "combined",
            "momentum_lookback": MOMENTUM_LOOKBACK,
            "momentum_percentile": MOMENTUM_PERCENTILE,
            "dip_threshold_pct": DIP_THRESHOLD_PCT,
            "de_threshold": de,
            "max_positions": pos,
            "max_per_sector": sec,
        })

    # Also add momentum-only baselines (same as champion config, for direct comparison)
    for de, pos in product(de_values, position_values):
        all_configs.append({
            "group": "momentum_only",
            "momentum_lookback": MOMENTUM_LOOKBACK,
            "momentum_percentile": MOMENTUM_PERCENTILE,
            "dip_threshold_pct": DIP_THRESHOLD_PCT,
            "de_threshold": de,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    # Quality-only baselines
    for de, pos in product(de_values, position_values):
        all_configs.append({
            "group": "quality_only",
            "momentum_lookback": 0,
            "momentum_percentile": 0,
            "dip_threshold_pct": DIP_THRESHOLD_PCT,
            "de_threshold": de,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    total = len(all_configs)
    n_combined = sum(1 for c in all_configs if c["group"] == "combined")
    n_mom = sum(1 for c in all_configs if c["group"] == "momentum_only")
    n_qual = sum(1 for c in all_configs if c["group"] == "quality_only")

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({n_combined} combined + {n_mom} momentum-only + "
          f"{n_qual} quality-only baselines)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"TSL={TSL_PCT}%, regime={REGIME_SMA}, hold={MAX_HOLD_DAYS}d")
    print(f"  Momentum: {MOMENTUM_LOOKBACK}d, top {MOMENTUM_PERCENTILE*100:.0f}%")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Cache filtered entries per (group_type, de_threshold)
    entries_cache = {}

    for idx, params in enumerate(all_configs):
        group = params["group"]
        de = params["de_threshold"]
        pos = params["max_positions"]
        sec = params["max_per_sector"]

        cache_key = (group, de)
        if cache_key not in entries_cache:
            if group == "combined":
                # Filter both sets by fundamentals, then merge
                q_filtered = filter_entries_by_fundamentals(
                    quality_entries_raw, fundamentals,
                    ROE_THRESHOLD, de, PE_THRESHOLD, missing_mode="skip")
                m_filtered = filter_entries_by_fundamentals(
                    momentum_entries_raw, fundamentals,
                    ROE_THRESHOLD, de, PE_THRESHOLD, missing_mode="skip")
                # Tag sources
                for e in q_filtered:
                    e["source"] = "quality"
                for e in m_filtered:
                    e["source"] = "momentum"
                entries = merge_entries(q_filtered, m_filtered)
            elif group == "momentum_only":
                entries = filter_entries_by_fundamentals(
                    momentum_entries_raw, fundamentals,
                    ROE_THRESHOLD, de, PE_THRESHOLD, missing_mode="skip")
            elif group == "quality_only":
                entries = filter_entries_by_fundamentals(
                    quality_entries_raw, fundamentals,
                    ROE_THRESHOLD, de, PE_THRESHOLD, missing_mode="skip")
            else:
                entries = []

            entries_cache[cache_key] = entries

        entries_run = entries_cache[cache_key]

        sim_kwargs = dict(
            capital=capital, max_positions=pos,
            tsl_pct=TSL_PCT, max_hold_days=MAX_HOLD_DAYS,
            exchange=exchange, regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME, description=description,
            params=params, start_epoch=start_epoch,
        )
        if sec > 0 and sector_map:
            sim_kwargs["sector_map"] = sector_map
            sim_kwargs["max_per_sector"] = sec

        r, dwl = simulate_portfolio(entries_run, price_data, benchmark, **sim_kwargs)
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        de_label = f"D/E<{de}" if de > 0 else "D/E=off"
        sec_label = f" sec<={sec}" if sec > 0 else ""
        print(f"  [{idx+1}/{total}] {group:15s} pos={pos:2d} {de_label:8s}{sec_label} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ── Always-invested adjustment ──
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (top 10)")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            group = params.get("group", "?")
            sec = params.get("max_per_sector", 0)
            de = params.get("de_threshold", 0)
            de_label = f"D/E<{de}" if de > 0 else "D/E=off"
            sec_label = f" sec<={sec}" if sec > 0 else ""
            label = f"pos={params['max_positions']} {de_label}{sec_label}"
            print(f"  #{i+1} {group:15s} {label} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    # ── Source breakdown for combined configs ──
    print(f"\n{'='*80}")
    print("  SOURCE BREAKDOWN (combined configs)")
    print(f"{'='*80}")

    for de in de_values:
        cache_key = ("combined", de)
        entries = entries_cache.get(cache_key, [])
        if not entries:
            continue
        n_momentum = sum(1 for e in entries if e.get("source") == "momentum")
        n_quality = sum(1 for e in entries if e.get("source") == "quality")
        de_label = f"D/E<{de}" if de > 0 else "D/E=off"
        print(f"  {de_label}: {len(entries)} total entries, "
              f"{n_momentum} momentum ({n_momentum/len(entries)*100:.0f}%), "
              f"{n_quality} quality-only ({n_quality/len(entries)*100:.0f}%)")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

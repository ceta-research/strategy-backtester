#!/usr/bin/env python3
"""Momentum breakout v2: refined sweep around best configs + combined portfolio.

Phase 1: Refined breakout sweep around the two best configs:
  - Best CAGR (22.2%): bw=63d, mom>50%, 5pos, TSL=15%, 126d hold, regime ON
  - Best Calmar (0.71): bw=126d, mom>30%, 5pos, TSL=10%, 504d hold, regime ON

Phase 2: Combined breakout + dip-buy portfolio
  - Allocate capital between breakout entries and dip-buy entries
  - Different allocation ratios

Always runs on bhavcopy with 5 bps slippage.
"""

import sys
import os
import time
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)
from scripts.momentum_breakout import compute_breakout_entries

STRATEGY_NAME = "momentum_breakout_v2"


def intersect_universes(quality_universe, momentum_universe):
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


def merge_entries(breakout_entries, dip_entries):
    """Merge breakout and dip entries, deduplicating by (symbol, entry_epoch).

    When both signal types fire for the same stock on the same day,
    keep the breakout entry (it has stronger momentum confirmation).
    """
    seen = set()
    merged = []

    # Breakout entries take priority
    for e in breakout_entries:
        key = (e["symbol"], e["entry_epoch"])
        if key not in seen:
            seen.add(key)
            entry = dict(e)
            entry["signal_type"] = "breakout"
            merged.append(entry)

    for e in dip_entries:
        key = (e["symbol"], e["entry_epoch"])
        if key not in seen:
            seen.add(key)
            entry = dict(e)
            entry["signal_type"] = "dip"
            merged.append(entry)

    merged.sort(key=lambda x: x["entry_epoch"])
    return merged


def main():
    exchange = "NSE"
    start_epoch = 1262304000
    end_epoch = 1773878400
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Refined breakout + combined portfolio on BHAVCOPY")
    print("=" * 80)

    # Fetch data
    print("\nFetching universe (turnover >= 70M INR)...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, 200)

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 1: Refined breakout sweep
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 1: Refined breakout parameter sweep")
    print(f"{'='*80}")

    # Expanded around best configs
    param_grid_breakout = list(product(
        [63, 126],              # breakout_window
        [0.30, 0.40, 0.50],    # momentum_threshold
        [5, 7, 10],            # max_positions
        [10, 12, 15, 20],      # tsl_pct
        [126, 252, 504],       # max_hold_days
    ))
    # All with regime ON (proven better for breakout)

    total_p1 = len(param_grid_breakout)
    print(f"  {total_p1} configs (all with regime ON)")

    description = "Momentum breakout v2: refined sweep + combined portfolio on bhavcopy"
    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    entry_cache = {}
    for idx, (bw, mom_thresh, pos, tsl, hold) in enumerate(param_grid_breakout):
        cache_key = (bw, mom_thresh)
        if cache_key not in entry_cache:
            entry_cache[cache_key] = compute_breakout_entries(
                price_data, bw, 126, mom_thresh, start_epoch=start_epoch)

        entries = entry_cache[cache_key]
        params = {
            "type": "breakout",
            "breakout_window": bw,
            "momentum_threshold": mom_thresh,
            "max_positions": pos,
            "tsl_pct": tsl,
            "max_hold_days": hold,
        }

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl, max_hold_days=hold,
            exchange=exchange, regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME, description=description,
            params=params, start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [{idx+1}/{total_p1}] bw={bw}d mom>{mom_thresh*100:.0f}% pos={pos} "
              f"tsl={tsl}% hold={hold}d | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 2: Combined breakout + dip-buy portfolio
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 2: Combined breakout + dip-buy portfolio")
    print(f"{'='*80}")

    # Compute dip-buy entries (champion config)
    print("\nComputing dip-buy entries (champion config)...")
    quality_universe = compute_quality_universe(
        price_data, 2, 0, rescreen_days=63, start_epoch=start_epoch)
    momentum_universe = compute_momentum_universe(
        price_data, 126, 0.20, rescreen_days=63, start_epoch=start_epoch)
    combined_universe = intersect_universes(quality_universe, momentum_universe)

    dip_entries_5 = compute_dip_entries(
        price_data, combined_universe, 63, 0.05, start_epoch=start_epoch)
    dip_entries_5 = filter_entries_by_fundamentals(
        dip_entries_5, fundamentals, 15, 0, 25, missing_mode="skip")
    print(f"  Dip entries (5%): {len(dip_entries_5)}")

    dip_entries_7 = compute_dip_entries(
        price_data, combined_universe, 63, 0.07, start_epoch=start_epoch)
    dip_entries_7 = filter_entries_by_fundamentals(
        dip_entries_7, fundamentals, 15, 0, 25, missing_mode="skip")
    print(f"  Dip entries (7%): {len(dip_entries_7)}")

    # Best breakout entries
    breakout_entries_50 = entry_cache.get((63, 0.50)) or compute_breakout_entries(
        price_data, 63, 126, 0.50, start_epoch=start_epoch)
    breakout_entries_30_126 = entry_cache.get((126, 0.30)) or compute_breakout_entries(
        price_data, 126, 126, 0.30, start_epoch=start_epoch)
    breakout_entries_40 = entry_cache.get((63, 0.40)) or compute_breakout_entries(
        price_data, 63, 126, 0.40, start_epoch=start_epoch)

    # Combined configs
    combined_configs = [
        # (label, breakout_entries, dip_entries, positions, tsl, hold)
        ("combo_A1", breakout_entries_50, dip_entries_7, 10, 10, 504,
         "bw63/mom50 + dip7%, 10pos TSL10% 504d"),
        ("combo_A2", breakout_entries_50, dip_entries_7, 10, 15, 504,
         "bw63/mom50 + dip7%, 10pos TSL15% 504d"),
        ("combo_A3", breakout_entries_50, dip_entries_7, 10, 12, 504,
         "bw63/mom50 + dip7%, 10pos TSL12% 504d"),
        ("combo_A4", breakout_entries_50, dip_entries_5, 10, 10, 504,
         "bw63/mom50 + dip5%, 10pos TSL10% 504d"),
        ("combo_A5", breakout_entries_50, dip_entries_5, 10, 15, 504,
         "bw63/mom50 + dip5%, 10pos TSL15% 504d"),
        ("combo_B1", breakout_entries_30_126, dip_entries_7, 10, 10, 504,
         "bw126/mom30 + dip7%, 10pos TSL10% 504d"),
        ("combo_B2", breakout_entries_30_126, dip_entries_7, 10, 15, 504,
         "bw126/mom30 + dip7%, 10pos TSL15% 504d"),
        ("combo_B3", breakout_entries_30_126, dip_entries_5, 10, 10, 504,
         "bw126/mom30 + dip5%, 10pos TSL10% 504d"),
        ("combo_C1", breakout_entries_40, dip_entries_7, 10, 10, 504,
         "bw63/mom40 + dip7%, 10pos TSL10% 504d"),
        ("combo_C2", breakout_entries_40, dip_entries_7, 10, 15, 504,
         "bw63/mom40 + dip7%, 10pos TSL15% 504d"),
        ("combo_C3", breakout_entries_40, dip_entries_7, 10, 12, 252,
         "bw63/mom40 + dip7%, 10pos TSL12% 252d"),
        ("combo_C4", breakout_entries_40, dip_entries_5, 10, 12, 504,
         "bw63/mom40 + dip5%, 10pos TSL12% 504d"),
        # More concentrated
        ("combo_D1", breakout_entries_50, dip_entries_7, 7, 12, 504,
         "bw63/mom50 + dip7%, 7pos TSL12% 504d"),
        ("combo_D2", breakout_entries_50, dip_entries_7, 7, 15, 252,
         "bw63/mom50 + dip7%, 7pos TSL15% 252d"),
        ("combo_D3", breakout_entries_40, dip_entries_7, 7, 12, 504,
         "bw63/mom40 + dip7%, 7pos TSL12% 504d"),
        ("combo_D4", breakout_entries_40, dip_entries_7, 7, 15, 504,
         "bw63/mom40 + dip7%, 7pos TSL15% 504d"),
        # Wider stops for breakout
        ("combo_E1", breakout_entries_50, dip_entries_7, 10, 20, 504,
         "bw63/mom50 + dip7%, 10pos TSL20% 504d"),
        ("combo_E2", breakout_entries_40, dip_entries_7, 10, 20, 504,
         "bw63/mom40 + dip7%, 10pos TSL20% 504d"),
        ("combo_E3", breakout_entries_50, dip_entries_7, 10, 20, 252,
         "bw63/mom50 + dip7%, 10pos TSL20% 252d"),
    ]

    for label, bo_entries, dp_entries, pos, tsl, hold, desc in combined_configs:
        merged = merge_entries(bo_entries, dp_entries)
        print(f"\n  {label}: {len(merged)} merged entries ({desc})")

        params = {
            "type": "combined",
            "label": label,
            "max_positions": pos,
            "tsl_pct": tsl,
            "max_hold_days": hold,
            "description": desc,
        }

        r, dwl = simulate_portfolio(
            merged, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl, max_hold_days=hold,
            exchange=exchange, regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME, description=desc,
            params=params, start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"    CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (top 15)")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:15]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            label = params.get("label", f"bw{params.get('breakout_window','?')}")
            typ = params.get("type", "?")
            print(f"  #{i+1} [{typ}] {label} pos={params['max_positions']} "
                  f"tsl={params['tsl_pct']}% hold={params['max_hold_days']}d | "
                  f"CAGR={(s.get('cagr') or 0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={(s.get('calmar_ratio') or 0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=30)
    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    # Top by CAGR
    print(f"\n{'='*80}")
    print("  TOP 15 BY CAGR")
    print(f"{'='*80}")
    sorted_by_cagr = sweep._sorted("cagr")
    for i, (params, r) in enumerate(sorted_by_cagr[:15]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        label = params.get("label", f"bw{params.get('breakout_window','?')}")
        print(f"  #{i+1} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades} | "
              f"{label} pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"hold={params['max_hold_days']}d")

    # Print best overall
    if sweep.configs:
        print(f"\n{'='*80}")
        print("  BEST BY CALMAR (detailed)")
        print(f"{'='*80}")
        _, best = sorted_configs[0]
        best.print_summary()

        print(f"\n{'='*80}")
        print("  BEST BY CAGR (detailed)")
        print(f"{'='*80}")
        _, best_cagr = sorted_by_cagr[0]
        best_cagr.print_summary()


if __name__ == "__main__":
    main()

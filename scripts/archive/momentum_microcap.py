#!/usr/bin/env python3
"""Micro-cap momentum dip-buy sweep.

Tests the hypothesis that lowering the turnover threshold captures
higher-returning small-cap momentum stocks on bhavcopy data.

Sweeps:
  - turnover_threshold: [5M, 10M, 20M, 70M] (0.5Cr to 7Cr)
  - max_positions: [3, 5, 10]
  - dip_threshold: [3, 5, 7]
  - momentum_lookback: [63, 126]
  - max_hold_days: [126, 252, 504]
  - tsl_pct: [7, 10]

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

STRATEGY_NAME = "momentum_microcap"

# Fixed champion params
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


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


def main():
    exchange = "NSE"
    start_epoch = 1262304000   # 2010-01-01
    end_epoch = 1773878400     # 2026-03-19
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Micro-cap momentum sweep on BHAVCOPY")
    print("=" * 80)

    # Turnover thresholds to test (INR)
    turnover_thresholds = [5_000_000, 10_000_000, 20_000_000, 70_000_000]

    # Fetch data for each turnover level (cache universes)
    universes = {}
    for thresh in turnover_thresholds:
        label = f"{thresh/1_000_000:.0f}M"
        print(f"\nFetching universe (turnover >= {label} INR)...")
        t0 = time.time()
        price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                    source=source, turnover_threshold=thresh)
        elapsed = time.time() - t0
        print(f"  Got {len(price_data)} symbols in {elapsed:.0f}s")
        universes[thresh] = price_data

    # Fetch benchmark (same for all)
    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # Define sweep grid - focused on key variables
    # Phase 1: test turnover × concentration × hold period
    # Keep momentum/dip at champion values to isolate the effect
    momentum_lookbacks = [126]
    momentum_percentile = 0.20  # fixed: top 20%
    dip_thresholds = [5, 7]
    max_positions_list = [3, 5, 10]
    tsl_pcts = [10]
    max_hold_list = [252, 504]

    # Build parameter grid (4×1×2×3×1×2 = 48 configs)
    param_grid = list(product(
        turnover_thresholds,
        momentum_lookbacks,
        dip_thresholds,
        max_positions_list,
        tsl_pcts,
        max_hold_list,
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  Turnover: {[f'{t/1e6:.0f}M' for t in turnover_thresholds]}")
    print(f"  Momentum: {momentum_lookbacks}d, top {momentum_percentile*100:.0f}%")
    print(f"  Dip: {dip_thresholds}%, Positions: {max_positions_list}")
    print(f"  TSL: {tsl_pcts}%, Hold: {max_hold_list}d")
    print(f"{'='*80}")

    description = ("Micro-cap momentum dip-buy sweep on bhavcopy: tests lower turnover "
                    "thresholds to capture higher-returning small-cap momentum stocks.")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Pre-compute quality + momentum universes per (turnover, lookback)
    # to avoid redundant computation
    computed_cache = {}

    for idx, (thresh, mom_lb, dip, pos, tsl, hold) in enumerate(param_grid):
        cache_key = (thresh, mom_lb)
        price_data = universes[thresh]

        if cache_key not in computed_cache:
            # Compute quality universe for this turnover level
            quality_universe = compute_quality_universe(
                price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

            # Compute momentum universe
            momentum_universe = compute_momentum_universe(
                price_data, mom_lb, momentum_percentile, rescreen_days=63, start_epoch=start_epoch)

            # Intersect
            combined = intersect_universes(quality_universe, momentum_universe)
            computed_cache[cache_key] = (quality_universe, momentum_universe, combined)

        _, _, combined_universe = computed_cache[cache_key]

        params = {
            "turnover_threshold": thresh,
            "momentum_lookback": mom_lb,
            "dip_threshold_pct": dip,
            "max_positions": pos,
            "tsl_pct": tsl,
            "max_hold_days": hold,
        }

        # Compute dip entries
        entries = compute_dip_entries(
            price_data, combined_universe, PEAK_LOOKBACK,
            dip / 100.0, start_epoch=start_epoch)

        # Fundamental filter
        entries = filter_entries_by_fundamentals(
            entries, fundamentals, ROE_THRESHOLD, 0, PE_THRESHOLD,
            missing_mode="skip")

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
        thresh_label = f"{thresh/1e6:.0f}M"
        print(f"  [{idx+1}/{total}] to={thresh_label} mom={mom_lb}d dip={dip}% pos={pos} "
              f"tsl={tsl}% hold={hold}d | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # Always-invested adjustment for top configs
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
            print(f"  #{i+1} to={params['turnover_threshold']/1e6:.0f}M "
                  f"mom={params['momentum_lookback']}d "
                  f"dip={params['dip_threshold_pct']}% pos={params['max_positions']} "
                  f"tsl={params['tsl_pct']}% hold={params['max_hold_days']}d | "
                  f"CAGR={(s.get('cagr') or 0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={(s.get('calmar_ratio') or 0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=30)
    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Momentum + Quality Dip-Buy.

Buy stocks in the top N% of trailing momentum that then dip X% from peak.
Quality gate (2yr positive returns) + fundamental overlay (ROE>15, PE<25).

Thesis: "Buy weakness in strong stocks." Momentum catches different stocks
than the quality filter alone (only 33% overlap). Self-correcting in bear
markets since fewer stocks pass the momentum filter.

Supports NSE (native data) and US (FMP data) via --market flag.

Outputs standardized result.json.
"""

import sys
import os
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

STRATEGY_NAME = "momentum_dip_buy"

# Fixed best params from quality dip-buy experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


def intersect_universes(quality_universe, momentum_universe):
    """Intersect quality and momentum universes epoch-by-epoch.

    For each epoch present in both, result is the set intersection.
    Epochs in only one universe use the other's empty set.

    Returns:
        dict[epoch, set[symbol]]
    """
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
        description = ("Momentum + quality dip-buy on NSE: buy top momentum stocks "
                       "that pass quality + fundamental gates and then dip from peak.")
    elif market == "us":
        exchange = "US"
        start_epoch = 1104537600   # 2005-01-01
        end_epoch = 1773878400     # 2026-03-19
        benchmark_sym = "SPY"
        capital = 10_000_000       # $10M
        description = ("Momentum + quality dip-buy on US: buy top momentum stocks "
                       "that pass quality + fundamental gates and then dip from peak.")
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

    # Pre-compute quality universe (fixed champion params)
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # Pre-compute momentum universes for all lookback/percentile combos
    momentum_lookbacks = [63, 126, 252]
    momentum_percentiles = [0.20, 0.30]

    momentum_cache = {}
    for lb in momentum_lookbacks:
        for pct in momentum_percentiles:
            key = (lb, pct)
            print(f"\nComputing momentum universe (lookback={lb}d, top {pct*100:.0f}%)...")
            momentum_cache[key] = compute_momentum_universe(
                price_data, lb, pct, rescreen_days=63, start_epoch=start_epoch)

    # ── Sweep ──
    param_grid = list(product(
        momentum_lookbacks,         # momentum_lookback
        momentum_percentiles,       # top percentile
        [5, 7],                     # dip_threshold_pct
        [5, 10],                    # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs (momentum + quality dip-buy on {market.upper()})")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"regime={REGIME_SMA}, TSL={TSL_PCT}%, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    for idx, (mom_lb, mom_pct, dip, pos) in enumerate(param_grid):
        params = {
            "momentum_lookback": mom_lb,
            "momentum_percentile": mom_pct,
            "dip_threshold_pct": dip,
            "max_positions": pos,
        }

        # Get pre-computed momentum universe and intersect with quality
        momentum_universe = momentum_cache[(mom_lb, mom_pct)]
        combined_universe = intersect_universes(quality_universe, momentum_universe)

        # Compute dip entries using combined universe
        entries = compute_dip_entries(
            price_data, combined_universe, PEAK_LOOKBACK,
            dip / 100.0, start_epoch=start_epoch)

        # Apply fundamental filter (champion's best: ROE>15, PE<25, skip missing)
        entries = filter_entries_by_fundamentals(
            entries, fundamentals, ROE_THRESHOLD, 0, PE_THRESHOLD,
            missing_mode="skip")

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=TSL_PCT, max_hold_days=MAX_HOLD_DAYS,
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
        print(f"  [{idx+1}/{total}] mom={mom_lb}d top{mom_pct*100:.0f}% dip={dip}% pos={pos} | "
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
            print(f"  #{i+1} mom={params['momentum_lookback']}d "
                  f"top{params['momentum_percentile']*100:.0f}% "
                  f"dip={params['dip_threshold_pct']}% pos={params['max_positions']} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

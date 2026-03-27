#!/usr/bin/env python3
"""Extended Momentum Dip-Buy Sweep: D/E Filter + More Positions + Sector Limits.

Focused sweep building on the combined sweep results (be0b01a):
- Champion: Calmar 1.01, +23.7% CAGR, -23.3% MDD (63d, top 30%, D/E<1.0, 10 pos)
- Highest CAGR: +31.7% but -36.5% MDD (126d, top 20%, D/E off, 5 pos)

Tests:
- D/E<1.0 on high-CAGR parameter space (126d, top 20%)
- Extended positions (5, 10, 15, 20) for further diversification
- Cross-parameter combos (126d+top30%, 63d+top20%)
- Sector concentration limits (max 2-3 per sector)

Fixed 10% TSL only (vol-adjusted proven to hurt momentum-dip).

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

STRATEGY_NAME = "momentum_dip_de_positions"

# Fixed params from prior experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25
TSL_PCT = 10  # fixed 10% TSL (vol-adj hurts momentum-dip)


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


def main():
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    from scripts.quality_dip_buy_lib import FMP_EXCHANGES

    MARKET_CONFIGS = {
        "nse": {"exchange": "NSE", "start": 1262304000, "benchmark": "NIFTYBEES", "capital": 10_000_000},
        "us":  {"exchange": "US",  "start": 1104537600, "benchmark": "SPY",       "capital": 10_000_000},
    }
    for exch, cfg in FMP_EXCHANGES.items():
        MARKET_CONFIGS[exch.lower()] = {
            "exchange": exch, "start": 1262304000, "benchmark": cfg["benchmark"],
            "capital": 10_000_000,
        }

    if market not in MARKET_CONFIGS:
        print(f"Unknown market: {market}. Supported: {', '.join(MARKET_CONFIGS.keys())}")
        return

    mc = MARKET_CONFIGS[market]
    exchange = mc["exchange"]
    start_epoch = mc["start"]
    end_epoch = 1773878400     # 2026-03-19
    benchmark_sym = mc["benchmark"]
    capital = mc["capital"]
    description = (f"Extended momentum dip-buy sweep on {exchange}: "
                   "D/E filter, extended positions, sector limits.")

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

    # Pre-compute quality universe
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # Pre-compute momentum universes for all (lookback, percentile) combos
    momentum_params = [(63, 0.20), (63, 0.30), (126, 0.20), (126, 0.30)]
    momentum_cache = {}
    for lb, pct in momentum_params:
        key = (lb, pct)
        print(f"\nComputing momentum universe (lookback={lb}d, top {pct*100:.0f}%)...")
        momentum_cache[key] = compute_momentum_universe(
            price_data, lb, pct, rescreen_days=63, start_epoch=start_epoch)

    # ── Build sweep groups ──

    positions_range = [5, 10, 15, 20]

    # Group A: High-CAGR space (126d, top 20%) + D/E<1.0
    group_a = []
    for pos in positions_range:
        group_a.append({
            "group": "A_highcagr_de",
            "momentum_lookback": 126,
            "momentum_percentile": 0.20,
            "dip_threshold_pct": 5,
            "de_threshold": 1.0,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    # Group B: High-CAGR space WITHOUT D/E (baselines)
    group_b = []
    for pos in positions_range:
        group_b.append({
            "group": "B_highcagr_base",
            "momentum_lookback": 126,
            "momentum_percentile": 0.20,
            "dip_threshold_pct": 5,
            "de_threshold": 0,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    # Group C: Champion space (63d, top 30%) + extended positions
    group_c = []
    for de, pos in product([0, 1.0], positions_range):
        group_c.append({
            "group": "C_champion_ext",
            "momentum_lookback": 63,
            "momentum_percentile": 0.30,
            "dip_threshold_pct": 5,
            "de_threshold": de,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    # Group D: Cross-tests with D/E<1.0
    group_d = []
    for (mom_lb, mom_pct), pos in product([(126, 0.30), (63, 0.20)], positions_range):
        group_d.append({
            "group": "D_cross_de",
            "momentum_lookback": mom_lb,
            "momentum_percentile": mom_pct,
            "dip_threshold_pct": 5,
            "de_threshold": 1.0,
            "max_positions": pos,
            "max_per_sector": 0,
        })

    # Group E: Sector-limited variants on best combos
    group_e = []
    for (mom_lb, mom_pct), pos, sec in product(
        [(63, 0.30), (126, 0.20)], [10, 15, 20], [2, 3]
    ):
        group_e.append({
            "group": "E_sector_limit",
            "momentum_lookback": mom_lb,
            "momentum_percentile": mom_pct,
            "dip_threshold_pct": 5,
            "de_threshold": 1.0,
            "max_positions": pos,
            "max_per_sector": sec,
        })

    all_configs = group_a + group_b + group_c + group_d + group_e
    total = len(all_configs)

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  A={len(group_a)} highCAGR+DE | B={len(group_b)} highCAGR base | "
          f"C={len(group_c)} champion ext | D={len(group_d)} cross+DE | "
          f"E={len(group_e)} sector limit")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"TSL={TSL_PCT}%, regime={REGIME_SMA}, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Cache dip entries per (mom_lb, mom_pct, dip, de_threshold)
    entries_cache = {}

    for idx, params in enumerate(all_configs):
        mom_lb = params["momentum_lookback"]
        mom_pct = params["momentum_percentile"]
        dip = params["dip_threshold_pct"]
        pos = params["max_positions"]
        de = params["de_threshold"]
        group = params["group"]
        sec_limit = params["max_per_sector"]

        # Get or compute filtered entries
        cache_key = (mom_lb, mom_pct, dip, de)
        if cache_key not in entries_cache:
            momentum_universe = momentum_cache[(mom_lb, mom_pct)]
            combined_universe = intersect_universes(quality_universe, momentum_universe)

            entries = compute_dip_entries(
                price_data, combined_universe, PEAK_LOOKBACK,
                dip / 100.0, start_epoch=start_epoch)

            entries = filter_entries_by_fundamentals(
                entries, fundamentals, ROE_THRESHOLD, de, PE_THRESHOLD,
                missing_mode="skip")

            entries_cache[cache_key] = entries

        entries_run = entries_cache[cache_key]

        sim_kwargs = dict(
            capital=capital, max_positions=pos,
            tsl_pct=TSL_PCT, max_hold_days=MAX_HOLD_DAYS,
            exchange=exchange, regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME, description=description,
            params=params, start_epoch=start_epoch,
        )
        if sec_limit > 0 and sector_map:
            sim_kwargs["sector_map"] = sector_map
            sim_kwargs["max_per_sector"] = sec_limit

        r, dwl = simulate_portfolio(entries_run, price_data, benchmark, **sim_kwargs)
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        de_label = f"D/E<{de}" if de > 0 else "D/E=off"
        sec_label = f" sec<={sec_limit}" if sec_limit > 0 else ""
        print(f"  [{idx+1}/{total}] {group} mom={mom_lb}d top{mom_pct*100:.0f}% "
              f"pos={pos} {de_label}{sec_label} | "
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
            label = (f"mom={params['momentum_lookback']}d "
                     f"top{params['momentum_percentile']*100:.0f}% "
                     f"pos={params['max_positions']} {de_label}{sec_label}")
            print(f"  #{i+1} {group} {label} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

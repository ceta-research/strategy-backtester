#!/usr/bin/env python3
"""Momentum + Quality Dip-Buy with Volatility-Adjusted Exits.

Combines two independent improvements over the original champion:
1. Momentum-dip stock selection (Strategy 5): buy top N% momentum stocks that dip
2. Vol-adjusted exits (Strategy 6): per-stock TSL = k * daily_vol

Best standalone results:
- Momentum-dip: Calmar 0.90, +26.3% CAGR, -29.1% MDD (63d, top 30%, 5% dip, 10 pos)
- Vol-adjusted: Calmar 0.70, +25.4% CAGR, -36.5% MDD (k=4, 60d vol, 5 pos)

Supports NSE (native data) and US (FMP data) via --market flag.

Outputs standardized result.json.
"""

import sys
import os
import copy
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    compute_realized_vol, simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)
from scripts.vol_adjusted_exits import attach_vol_tsl

STRATEGY_NAME = "momentum_dip_vol_exits"

# Fixed params from prior experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25
TSL_FLOOR = 3.0
TSL_CEIL = 15.0


def intersect_universes(quality_universe, momentum_universe):
    """Intersect quality and momentum universes epoch-by-epoch.

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
    market = os.environ.get("MARKET", "nse").lower()
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
    description = (f"Momentum dip-buy + vol-adjusted exits on {exchange}: "
                   "top momentum stocks with per-stock TSL = k * daily_vol.")

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

    # Pre-compute quality universe
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # Pre-compute momentum universes
    momentum_lookbacks = [63, 126]
    momentum_percentiles = [0.20, 0.30]

    momentum_cache = {}
    for lb in momentum_lookbacks:
        for pct in momentum_percentiles:
            key = (lb, pct)
            print(f"\nComputing momentum universe (lookback={lb}d, top {pct*100:.0f}%)...")
            momentum_cache[key] = compute_momentum_universe(
                price_data, lb, pct, rescreen_days=63, start_epoch=start_epoch)

    # Pre-compute realized vol
    vol_lookback = 60
    print(f"\nComputing realized vol (lookback={vol_lookback}d)...")
    vol_data = compute_realized_vol(price_data, lookback=vol_lookback)

    # ── Build sweep groups ──

    # Group A: Vol-adjusted momentum-dip (D/E off)
    group_a = []
    for mom_lb, mom_pct, dip, k, pos in product(
        momentum_lookbacks, momentum_percentiles, [5], [3, 4, 5, 6], [5, 10]
    ):
        group_a.append({
            "group": "A_vol_adj",
            "momentum_lookback": mom_lb,
            "momentum_percentile": mom_pct,
            "dip_threshold_pct": dip,
            "k": k,
            "vol_lookback": vol_lookback,
            "de_threshold": 0,
            "max_positions": pos,
        })

    # Group B: Fixed TSL baselines (D/E off)
    group_b = []
    for mom_lb, mom_pct, dip, pos in product(
        momentum_lookbacks, momentum_percentiles, [5], [5, 10]
    ):
        group_b.append({
            "group": "B_fixed_tsl",
            "momentum_lookback": mom_lb,
            "momentum_percentile": mom_pct,
            "dip_threshold_pct": dip,
            "k": 0,
            "de_threshold": 0,
            "max_positions": pos,
        })

    # Group C: Vol-adjusted with D/E<1.0
    group_c = []
    for k, pos in product([3, 4, 5, 6], [5, 10]):
        group_c.append({
            "group": "C_vol_adj_de",
            "momentum_lookback": 63,
            "momentum_percentile": 0.30,
            "dip_threshold_pct": 5,
            "k": k,
            "vol_lookback": vol_lookback,
            "de_threshold": 1.0,
            "max_positions": pos,
        })

    # Group D: Fixed TSL with D/E<1.0
    group_d = []
    for pos in [5, 10]:
        group_d.append({
            "group": "D_fixed_tsl_de",
            "momentum_lookback": 63,
            "momentum_percentile": 0.30,
            "dip_threshold_pct": 5,
            "k": 0,
            "de_threshold": 1.0,
            "max_positions": pos,
        })

    all_configs = group_a + group_b + group_c + group_d
    total = len(all_configs)

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({len(group_a)} vol-adj + {len(group_b)} fixed + "
          f"{len(group_c)} vol-adj+D/E + {len(group_d)} fixed+D/E)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"regime={REGIME_SMA}, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Cache dip entries per (mom_lb, mom_pct, dip, de_threshold) to avoid recomputation
    entries_cache = {}

    for idx, params in enumerate(all_configs):
        mom_lb = params["momentum_lookback"]
        mom_pct = params["momentum_percentile"]
        dip = params["dip_threshold_pct"]
        k = params["k"]
        pos = params["max_positions"]
        de = params["de_threshold"]
        group = params["group"]

        # Get or compute filtered entries for this (mom, dip, de) combo
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

        base_entries = entries_cache[cache_key]

        if k > 0:
            # Vol-adjusted: deep copy and attach per-stock TSL
            entries_run = copy.deepcopy(base_entries)
            attach_vol_tsl(entries_run, vol_data, k, TSL_FLOOR, TSL_CEIL)
            tsl_fallback = 10
        else:
            # Fixed TSL baseline
            entries_run = base_entries
            tsl_fallback = 10  # fixed 10% TSL

        r, dwl = simulate_portfolio(
            entries_run, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl_fallback, max_hold_days=MAX_HOLD_DAYS,
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

        de_label = f"D/E<{de}" if de > 0 else "D/E=off"
        if k > 0:
            print(f"  [{idx+1}/{total}] {group} mom={mom_lb}d top{mom_pct*100:.0f}% "
                  f"k={k} pos={pos} {de_label} | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")
        else:
            print(f"  [{idx+1}/{total}] {group} mom={mom_lb}d top{mom_pct*100:.0f}% "
                  f"fixed=10% pos={pos} {de_label} | "
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
            k = params.get("k", 0)
            de = params.get("de_threshold", 0)
            de_label = f"D/E<{de}" if de > 0 else "D/E=off"
            if k > 0:
                label = (f"mom={params['momentum_lookback']}d "
                         f"top{params['momentum_percentile']*100:.0f}% "
                         f"k={k} pos={params['max_positions']} {de_label}")
            else:
                label = (f"mom={params['momentum_lookback']}d "
                         f"top{params['momentum_percentile']*100:.0f}% "
                         f"fixed=10% pos={params['max_positions']} {de_label}")
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

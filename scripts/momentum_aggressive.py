#!/usr/bin/env python3
"""Aggressive momentum dip-buy sweep.

Tests whether loosening filters boosts CAGR beyond 20.1% on bhavcopy.
Key hypotheses:
  1. Fundamental filter is too restrictive (blocks good momentum stocks)
  2. Regime filter costs returns in V-shaped recoveries
  3. Tighter TSL (5-7%) captures more profits
  4. 63d momentum catches trends earlier
  5. 3% dip gives more entry signals

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

STRATEGY_NAME = "momentum_aggressive"

# Champion defaults
PEAK_LOOKBACK = 63


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
    print(f"  {STRATEGY_NAME}: Aggressive parameter sweep on BHAVCOPY")
    print("=" * 80)

    # Fetch data once (70M turnover, proven best)
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

    # Pre-compute quality universes (1yr and 2yr)
    quality_cache = {}
    for consec_years in [1, 2]:
        print(f"\nComputing quality universe ({consec_years}yr)...")
        quality_cache[consec_years] = compute_quality_universe(
            price_data, consec_years, 0, rescreen_days=63, start_epoch=start_epoch)

    # Pre-compute momentum universes
    momentum_cache = {}
    for lb in [63, 126]:
        for pct in [0.10, 0.20, 0.30]:
            print(f"\nComputing momentum universe ({lb}d, top {pct*100:.0f}%)...")
            momentum_cache[(lb, pct)] = compute_momentum_universe(
                price_data, lb, pct, rescreen_days=63, start_epoch=start_epoch)

    # Sweep configs - each is a named configuration
    configs = []

    # Group A: Momentum lookback variants (champion baseline + 63d)
    for lb in [63, 126]:
        for pct in [0.10, 0.20]:
            for dip in [3, 5, 7]:
                for pos in [5, 10]:
                    configs.append({
                        "group": "A",
                        "quality_years": 2,
                        "momentum_lookback": lb,
                        "momentum_percentile": pct,
                        "dip_threshold_pct": dip,
                        "max_positions": pos,
                        "tsl_pct": 10,
                        "max_hold_days": 504,
                        "use_regime": True,
                        "use_fundamentals": True,
                        "roe_threshold": 15,
                        "pe_threshold": 25,
                    })

    # Group B: TSL variants (tighter stops for faster profit capture)
    for tsl in [5, 7]:
        for dip in [5, 7]:
            for pos in [5, 10]:
                configs.append({
                    "group": "B",
                    "quality_years": 2,
                    "momentum_lookback": 126,
                    "momentum_percentile": 0.20,
                    "dip_threshold_pct": dip,
                    "max_positions": pos,
                    "tsl_pct": tsl,
                    "max_hold_days": 504,
                    "use_regime": True,
                    "use_fundamentals": True,
                    "roe_threshold": 15,
                    "pe_threshold": 25,
                })

    # Group C: No regime filter (captures V-shaped recoveries)
    for dip in [5, 7]:
        for pos in [5, 10]:
            for tsl in [7, 10]:
                configs.append({
                    "group": "C",
                    "quality_years": 2,
                    "momentum_lookback": 126,
                    "momentum_percentile": 0.20,
                    "dip_threshold_pct": dip,
                    "max_positions": pos,
                    "tsl_pct": tsl,
                    "max_hold_days": 504,
                    "use_regime": False,
                    "use_fundamentals": True,
                    "roe_threshold": 15,
                    "pe_threshold": 25,
                })

    # Group D: No fundamental filter (pure momentum + quality)
    for dip in [5, 7]:
        for pos in [5, 10]:
            for tsl in [7, 10]:
                configs.append({
                    "group": "D",
                    "quality_years": 2,
                    "momentum_lookback": 126,
                    "momentum_percentile": 0.20,
                    "dip_threshold_pct": dip,
                    "max_positions": pos,
                    "tsl_pct": tsl,
                    "max_hold_days": 504,
                    "use_regime": True,
                    "use_fundamentals": False,
                    "roe_threshold": 0,
                    "pe_threshold": 999,
                })

    # Group E: Looser quality (1yr instead of 2yr)
    for dip in [5, 7]:
        for pos in [5, 10]:
            configs.append({
                "group": "E",
                "quality_years": 1,
                "momentum_lookback": 126,
                "momentum_percentile": 0.20,
                "dip_threshold_pct": dip,
                "max_positions": pos,
                "tsl_pct": 10,
                "max_hold_days": 504,
                "use_regime": True,
                "use_fundamentals": True,
                "roe_threshold": 15,
                "pe_threshold": 25,
            })

    # Group F: Combined best ideas (no regime + no fundamentals + 63d momentum)
    for lb in [63, 126]:
        for dip in [3, 5, 7]:
            for pos in [5, 10]:
                configs.append({
                    "group": "F",
                    "quality_years": 2,
                    "momentum_lookback": lb,
                    "momentum_percentile": 0.20,
                    "dip_threshold_pct": dip,
                    "max_positions": pos,
                    "tsl_pct": 10,
                    "max_hold_days": 504,
                    "use_regime": False,
                    "use_fundamentals": False,
                    "roe_threshold": 0,
                    "pe_threshold": 999,
                })

    # Group G: Top 10% momentum (more selective)
    for dip in [3, 5, 7]:
        for pos in [5, 10]:
            configs.append({
                "group": "G",
                "quality_years": 2,
                "momentum_lookback": 126,
                "momentum_percentile": 0.10,
                "dip_threshold_pct": dip,
                "max_positions": pos,
                "tsl_pct": 10,
                "max_hold_days": 504,
                "use_regime": True,
                "use_fundamentals": True,
                "roe_threshold": 15,
                "pe_threshold": 25,
            })

    total = len(configs)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs across 7 groups")
    print(f"  A: Momentum variants ({sum(1 for c in configs if c['group']=='A')})")
    print(f"  B: TSL variants ({sum(1 for c in configs if c['group']=='B')})")
    print(f"  C: No regime ({sum(1 for c in configs if c['group']=='C')})")
    print(f"  D: No fundamentals ({sum(1 for c in configs if c['group']=='D')})")
    print(f"  E: 1yr quality ({sum(1 for c in configs if c['group']=='E')})")
    print(f"  F: No regime + no fund ({sum(1 for c in configs if c['group']=='F')})")
    print(f"  G: Top 10% momentum ({sum(1 for c in configs if c['group']=='G')})")
    print(f"{'='*80}")

    description = ("Aggressive momentum sweep: tests loosening filters, tighter TSL, "
                    "faster momentum, and more signals on bhavcopy.")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Cache for combined universes
    universe_cache = {}

    for idx, cfg in enumerate(configs):
        qy = cfg["quality_years"]
        lb = cfg["momentum_lookback"]
        pct = cfg["momentum_percentile"]
        dip = cfg["dip_threshold_pct"]
        pos = cfg["max_positions"]
        tsl = cfg["tsl_pct"]
        hold = cfg["max_hold_days"]
        use_regime = cfg["use_regime"]
        use_fund = cfg["use_fundamentals"]
        roe = cfg["roe_threshold"]
        pe = cfg["pe_threshold"]

        # Build combined universe
        u_key = (qy, lb, pct)
        if u_key not in universe_cache:
            quality_u = quality_cache[qy]
            momentum_u = momentum_cache[(lb, pct)]
            universe_cache[u_key] = intersect_universes(quality_u, momentum_u)

        combined_universe = universe_cache[u_key]

        # Compute dip entries
        entries = compute_dip_entries(
            price_data, combined_universe, PEAK_LOOKBACK,
            dip / 100.0, start_epoch=start_epoch)

        # Apply fundamental filter if enabled
        if use_fund and roe > 0:
            entries = filter_entries_by_fundamentals(
                entries, fundamentals, roe, 0, pe, missing_mode="skip")

        # Simulate
        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl, max_hold_days=hold,
            exchange=exchange,
            regime_epochs=regime_epochs if use_regime else None,
            strategy_name=STRATEGY_NAME, description=description,
            params=cfg, start_epoch=start_epoch,
        )

        params_for_sweep = {k: v for k, v in cfg.items()}
        sweep.add_config(params_for_sweep, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        grp = cfg["group"]
        regime_flag = "R" if use_regime else "-"
        fund_flag = "F" if use_fund else "-"
        print(f"  [{idx+1}/{total}] {grp} mom={lb}d top{pct*100:.0f}% dip={dip}% "
              f"pos={pos} tsl={tsl}% {regime_flag}{fund_flag} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # Leaderboard
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
            grp = params.get("group", "?")
            print(f"  #{i+1} [{grp}] mom={params['momentum_lookback']}d "
                  f"top{params['momentum_percentile']*100:.0f}% "
                  f"dip={params['dip_threshold_pct']}% pos={params['max_positions']} "
                  f"tsl={params['tsl_pct']}% "
                  f"{'R' if params.get('use_regime') else '-'}"
                  f"{'F' if params.get('use_fundamentals') else '-'} | "
                  f"CAGR={(s.get('cagr') or 0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={(s.get('calmar_ratio') or 0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=30)
    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    # Print best by CAGR (not just Calmar)
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
        grp = params.get("group", "?")
        print(f"  #{i+1} [{grp}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades} | "
              f"mom={params['momentum_lookback']}d top{params['momentum_percentile']*100:.0f}% "
              f"dip={params['dip_threshold_pct']}% pos={params['max_positions']} "
              f"tsl={params['tsl_pct']}% "
              f"{'R' if params.get('use_regime') else '-'}"
              f"{'F' if params.get('use_fundamentals') else '-'}")


if __name__ == "__main__":
    main()

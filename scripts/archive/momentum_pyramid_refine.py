#!/usr/bin/env python3
"""Refinement sweep around best momentum pyramid configs.

Goal A: Push above 30% CAGR (refine around wf=2.0/an=0.10/10pos/tsl=12%)
Goal B: MDD < 30% with max CAGR (refine around combined mpi=2/wf=1.5/sma=50/12pos/tsl=15%)

Always runs on bhavcopy with 5 bps slippage.
"""

import sys
import os
import time
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import BacktestResult, SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_regime_epochs,
    CetaResearch,
    SLIPPAGE,
)
from scripts.momentum_breakout_v3 import compute_cascade_entries
from scripts.momentum_pyramid import simulate_pyramid_portfolio

STRATEGY_NAME = "momentum_pyramid_refine"


def main():
    exchange = "NSE"
    start_epoch = 1262304000
    end_epoch = 1773878400
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Focused refinement around best configs")
    print("=" * 80)

    print("\nFetching universe (turnover >= 70M INR)...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nComputing regime filters...")
    regime_200 = compute_regime_epochs(benchmark, 200)
    regime_50 = compute_regime_epochs(benchmark, 50)
    regime_75 = compute_regime_epochs(benchmark, 75)

    description = "Refinement sweep around best pyramid configs"
    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Pre-compute cascade entries (f=42d is the winner)
    print("\nComputing cascade entries...")
    cascade_cache = {}
    for accel in [0.02, 0.03]:
        for min_mom in [0.15, 0.20, 0.25]:
            key = (42, 126, accel, min_mom)
            cascade_cache[key] = compute_cascade_entries(
                price_data, 42, 126, accel, min_mom, start_epoch=start_epoch)

    # ══════════════════════════════════════════════════════════════════════
    # GOAL A: Push above 30% CAGR
    # Base: f=42d, accel>2%, mom>20%, wf=2.0, an=0.10, 10pos, tsl=12%
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  GOAL A: Push above 30% CAGR")
    print("  Base: wf=2.0, an=0.10, 10pos, tsl=12%, sma=200")
    print(f"{'='*80}")

    a_grid = list(product(
        [(42, 126, 0.02, 0.20), (42, 126, 0.02, 0.15), (42, 126, 0.03, 0.20)],
        [2.0, 2.25, 2.5, 3.0],    # momentum_weight_factor
        [0.07, 0.10, 0.12, 0.15], # accel_norm
        [8, 10, 12],               # max_positions
        [10, 11, 12],              # tsl_pct
    ))

    total_a = len(a_grid)
    print(f"  {total_a} configs")
    best_cagr = 0
    best_cagr_config = None

    for idx, (ck, wf, an, pos, tsl) in enumerate(a_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "goal_a_cagr",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "momentum_weight_factor": wf, "accel_norm": an,
            "max_positions": pos, "tsl_pct": tsl,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=1,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            momentum_weight_factor=wf, accel_norm=an,
            regime_epochs=regime_200, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        if cagr > best_cagr:
            best_cagr = cagr
            best_cagr_config = params

        flag = " ***" if cagr > 30 else (" **" if cagr > 28 else "")
        if idx % 5 == 0 or cagr > 25:
            print(f"  [{idx+1}/{total_a}] wf={wf} an={an} pos={pos} tsl={tsl}% "
                  f"a>{ck[2]*100:.0f}% m>{ck[3]*100:.0f}% | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    print(f"\n  >> Best CAGR so far: {best_cagr:+.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # GOAL A2: Push above 30% with pyramid + weighting
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  GOAL A2: 30%+ CAGR with pyramid + momentum weight")
    print(f"{'='*80}")

    a2_grid = list(product(
        [(42, 126, 0.02, 0.20), (42, 126, 0.02, 0.15)],
        [2, 3],                    # max_per_instrument
        [2.0, 2.5],               # momentum_weight_factor
        [0.10, 0.12],             # accel_norm
        [10, 12],                  # max_positions
        [10, 12],                  # tsl_pct
        [0.5, 0.75],              # pyramid_decay
        [14, 21],                  # pyramid_min_gap
    ))

    total_a2 = len(a2_grid)
    print(f"  {total_a2} configs")

    for idx, (ck, mpi, wf, an, pos, tsl, pd, pg) in enumerate(a2_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "goal_a2_pyramid_cagr",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "max_per_instrument": mpi,
            "momentum_weight_factor": wf, "accel_norm": an,
            "max_positions": pos, "tsl_pct": tsl,
            "pyramid_decay": pd, "pyramid_min_gap": pg,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=mpi,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            pyramid_decay=pd, pyramid_min_gap=pg,
            momentum_weight_factor=wf, accel_norm=an,
            regime_epochs=regime_200, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        if cagr > best_cagr:
            best_cagr = cagr
            best_cagr_config = params

        flag = " ***" if cagr > 30 else (" **" if cagr > 28 else "")
        if idx % 8 == 0 or cagr > 25:
            print(f"  [{idx+1}/{total_a2}] mpi={mpi} wf={wf} an={an} pos={pos} "
                  f"tsl={tsl}% pd={pd} pg={pg} | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    print(f"\n  >> Best CAGR so far: {best_cagr:+.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # GOAL B: MDD < 30% with maximum CAGR
    # Base: combined mpi=2, wf=1.5, sma=50, 12pos, tsl=15%
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  GOAL B: Max CAGR with MDD < 30%")
    print("  Base: mpi=2, wf=1.5, sma=50, 12pos, tsl=15%")
    print(f"{'='*80}")

    b_grid = list(product(
        [(42, 126, 0.02, 0.20), (42, 126, 0.02, 0.15), (42, 126, 0.03, 0.20)],
        [1, 2, 3],                 # max_per_instrument
        [1.25, 1.5, 1.75, 2.0],   # momentum_weight_factor
        [10, 12, 14],              # max_positions
        [12, 13, 14, 15],          # tsl_pct
    ))

    # Only run with sma=50 (proven best for MDD control) + regime_75
    total_b = len(b_grid)
    print(f"  {total_b} configs (sma=50)")
    best_safe_cagr = 0
    best_safe_config = None

    for idx, (ck, mpi, wf, pos, tsl) in enumerate(b_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "goal_b_safe",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "max_per_instrument": mpi,
            "momentum_weight_factor": wf,
            "regime_sma": 50,
            "max_positions": pos, "tsl_pct": tsl,
            "pyramid_decay": 0.5, "pyramid_min_gap": 21,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=mpi,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            pyramid_decay=0.5, pyramid_min_gap=21,
            momentum_weight_factor=wf, accel_norm=0.10,
            regime_epochs=regime_50, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        in_safe = mdd > -30
        if in_safe and cagr > best_safe_cagr:
            best_safe_cagr = cagr
            best_safe_config = params

        flag = " ***" if (in_safe and cagr > 25) else (" **" if (in_safe and cagr > 22) else "")
        if idx % 10 == 0 or (in_safe and cagr > 22):
            print(f"  [{idx+1}/{total_b}] mpi={mpi} wf={wf} pos={pos} tsl={tsl}% "
                  f"a>{ck[2]*100:.0f}% m>{ck[3]*100:.0f}% | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    print(f"\n  >> Best safe CAGR (MDD < 30%): {best_safe_cagr:+.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # GOAL B2: Same but with sma=75 (between 50 and 100)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  GOAL B2: Max CAGR with MDD < 30% (sma=75)")
    print(f"{'='*80}")

    # Smaller grid, focused on winners from B
    b2_grid = list(product(
        [(42, 126, 0.02, 0.20), (42, 126, 0.02, 0.15)],
        [1, 2],                    # max_per_instrument
        [1.5, 1.75],              # momentum_weight_factor
        [10, 12, 14],              # max_positions
        [13, 14, 15],              # tsl_pct
    ))

    total_b2 = len(b2_grid)
    print(f"  {total_b2} configs (sma=75)")

    for idx, (ck, mpi, wf, pos, tsl) in enumerate(b2_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "goal_b2_safe75",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "max_per_instrument": mpi,
            "momentum_weight_factor": wf,
            "regime_sma": 75,
            "max_positions": pos, "tsl_pct": tsl,
            "pyramid_decay": 0.5, "pyramid_min_gap": 21,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=mpi,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            pyramid_decay=0.5, pyramid_min_gap=21,
            momentum_weight_factor=wf, accel_norm=0.10,
            regime_epochs=regime_75, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0

        in_safe = mdd > -30
        if in_safe and cagr > best_safe_cagr:
            best_safe_cagr = cagr
            best_safe_config = params

        flag = " ***" if (in_safe and cagr > 25) else (" **" if (in_safe and cagr > 22) else "")
        if idx % 8 == 0 or (in_safe and cagr > 22):
            print(f"  [{idx+1}/{total_b2}] mpi={mpi} wf={wf} pos={pos} tsl={tsl}% "
                  f"a>{ck[2]*100:.0f}% m>{ck[3]*100:.0f}% | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    print(f"\n  >> Best safe CAGR (MDD < 30%): {best_safe_cagr:+.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  TOP 20 BY CAGR (all configs)")
    print(f"{'='*80}")
    sorted_by_cagr = sweep._sorted("cagr")
    for i, (params, r) in enumerate(sorted_by_cagr[:20]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        safe = " SAFE" if mdd > -30 else ""
        print(f"  #{i+1} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} T={trades}{safe} | "
              f"wf={params.get('momentum_weight_factor', 1.0)} "
              f"an={params.get('accel_norm', 0.10)} "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"mpi={params.get('max_per_instrument', 1)} "
              f"sma={params.get('regime_sma', 200)}")

    print(f"\n{'='*80}")
    print("  TOP 15 SAFE (MDD < 30%)")
    print(f"{'='*80}")
    count = 0
    for params, r in sorted_by_cagr:
        s = r.to_dict()["summary"]
        mdd = (s.get("max_drawdown") or 0) * 100
        if mdd < -30:
            continue
        cagr = (s.get("cagr") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        sharpe = s.get("sharpe_ratio") or 0
        typ = params.get("type", "?")
        count += 1
        print(f"  #{count} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} Sharpe={sharpe:.2f} T={trades} | "
              f"wf={params.get('momentum_weight_factor', 1.0)} "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"mpi={params.get('max_per_instrument', 1)} "
              f"sma={params.get('regime_sma', 200)} "
              f"a>{params.get('accel', 0.02)*100:.0f}% m>{params.get('min_mom', 0.20)*100:.0f}%")
        if count >= 15:
            break

    print(f"\n{'='*80}")
    print("  TOP 10 BY CALMAR")
    print(f"{'='*80}")
    sorted_by_calmar = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_by_calmar[:10]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        safe = " SAFE" if mdd > -30 else ""
        print(f"  #{i+1} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} T={trades}{safe} | "
              f"wf={params.get('momentum_weight_factor', 1.0)} "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}%")

    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    # Print detailed view of best CAGR and best safe
    if best_cagr_config:
        print(f"\n  >> BEST CAGR: {best_cagr:+.1f}%")
        print(f"     Config: {best_cagr_config}")
    if best_safe_config:
        print(f"\n  >> BEST SAFE (MDD < 30%): {best_safe_cagr:+.1f}%")
        print(f"     Config: {best_safe_config}")

    # Print detailed summary for best overall
    if sweep.configs:
        _, best = sorted_by_cagr[0]
        print(f"\n{'='*80}")
        print("  BEST BY CAGR (detailed)")
        print(f"{'='*80}")
        best.print_summary()


if __name__ == "__main__":
    main()

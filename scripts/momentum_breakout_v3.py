#!/usr/bin/env python3
"""Momentum breakout v3: Low-MDD optimization + new "Momentum Cascade" strategy.

Phase 1: Push CAGR higher while keeping MDD < 30%
  - Refined around best Calmar (bw126, mom50%, 10pos, TSL12%, 504d = 20.7%/−27.2%)
  - Combined portfolio tuning for MDD < 30%

Phase 2: "Momentum Cascade" - a new strategy targeting 30%+ CAGR
  - Buy stocks with ACCELERATING momentum (momentum-of-momentum)
  - Wider TSL (15-20%) to let trends run
  - Exit on momentum deceleration (not just price)
  - Variable position sizing based on momentum strength

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
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)
from scripts.momentum_breakout import compute_breakout_entries
from engine.charges import calculate_charges

STRATEGY_NAME = "momentum_breakout_v3"
SLIPPAGE = 0.0005


def intersect_universes(quality_universe, momentum_universe):
    combined = {}
    for epoch in set(quality_universe.keys()) | set(momentum_universe.keys()):
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
    seen = set()
    merged = []
    for e in breakout_entries:
        key = (e["symbol"], e["entry_epoch"])
        if key not in seen:
            seen.add(key)
            merged.append(dict(e))
    for e in dip_entries:
        key = (e["symbol"], e["entry_epoch"])
        if key not in seen:
            seen.add(key)
            merged.append(dict(e))
    merged.sort(key=lambda x: x["entry_epoch"])
    return merged


def compute_cascade_entries(price_data, fast_lookback, slow_lookback,
                            accel_threshold, min_momentum, start_epoch=None):
    """Generate momentum cascade (acceleration) entry signals.

    Signal fires when:
    1. Fast momentum (e.g., 21d) is positive
    2. Slow momentum (e.g., 126d) is positive and above min_momentum
    3. Momentum acceleration (fast_mom - lagged_fast_mom) exceeds threshold
    4. Stock is making new 63d high (breakout confirmation)

    This captures stocks with ACCELERATING momentum — the strongest trend signal.
    """
    entries = []
    lookback_needed = max(slow_lookback, fast_lookback * 2, 63) + 10

    for symbol, bars in price_data.items():
        if len(bars) < lookback_needed + 2:
            continue

        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]

        for i in range(lookback_needed, len(bars) - 1):
            if start_epoch and epochs[i] < start_epoch:
                continue

            current_close = closes[i]
            if current_close <= 0:
                continue

            # Slow momentum (must be above threshold)
            past_slow = closes[i - slow_lookback]
            if past_slow <= 0:
                continue
            slow_mom = (current_close - past_slow) / past_slow
            if slow_mom < min_momentum:
                continue

            # Fast momentum now
            past_fast = closes[i - fast_lookback]
            if past_fast <= 0:
                continue
            fast_mom_now = (current_close - past_fast) / past_fast

            # Fast momentum N days ago
            if i - fast_lookback < fast_lookback:
                continue
            past_fast_ago = closes[i - fast_lookback - fast_lookback]
            if past_fast_ago <= 0:
                continue
            fast_mom_ago = (closes[i - fast_lookback] - past_fast_ago) / past_fast_ago

            # Acceleration: current fast_mom - previous fast_mom
            acceleration = fast_mom_now - fast_mom_ago
            if acceleration < accel_threshold:
                continue

            # Breakout confirmation: new 63d high
            window_start = max(0, i - 63)
            window_high = max(closes[window_start:i])
            if current_close <= window_high:
                continue

            # Entry at next day's open (MOC)
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            entries.append({
                "epoch": epochs[i],
                "symbol": symbol,
                "peak_price": current_close,
                "dip_pct": 0.0,
                "entry_epoch": epochs[i + 1],
                "entry_price": next_open,
                "momentum": slow_mom,
                "acceleration": acceleration,
            })

    entries.sort(key=lambda x: (-x.get("acceleration", 0), x["entry_epoch"]))
    # Re-sort by epoch for simulation
    entries.sort(key=lambda x: x["entry_epoch"])
    print(f"  Cascade entries: {len(entries)} signals "
          f"(fast={fast_lookback}d, slow={slow_lookback}d, accel>={accel_threshold*100:.0f}%)")
    return entries


def simulate_cascade(
    price_data, entries, benchmark_data,
    *, capital, max_positions, tsl_pct, max_hold_days,
    exchange, regime_epochs=None, start_epoch=None,
    momentum_ranking=True, params=None,
):
    """Custom simulator for cascade strategy with momentum-ranked entry priority.

    Key difference from standard simulate_portfolio:
    - When multiple entries fire on the same day, prioritize by acceleration
    - Position sizing weighted by momentum strength (optional)
    """
    # Sort entries by entry_epoch, then by acceleration (descending) for priority
    sorted_entries = sorted(entries, key=lambda x: (x["entry_epoch"], -x.get("acceleration", 0)))

    # Use standard simulator (it processes entries in order, so priority matters)
    return simulate_portfolio(
        sorted_entries, price_data, benchmark_data,
        capital=capital, max_positions=max_positions,
        tsl_pct=tsl_pct, max_hold_days=max_hold_days,
        exchange=exchange, regime_epochs=regime_epochs,
        strategy_name=STRATEGY_NAME,
        description="Momentum cascade: accelerating momentum + breakout",
        params=params, start_epoch=start_epoch,
    )


def main():
    exchange = "NSE"
    start_epoch = 1262304000
    end_epoch = 1773878400
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Low-MDD optimization + Momentum Cascade")
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

    description = "Breakout v3: low-MDD optimization + momentum cascade"
    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 1: Low-MDD optimization (target: >22% CAGR, <30% MDD)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 1: Low-MDD breakout optimization")
    print(f"{'='*80}")

    # Refined grid around best Calmar config
    # bw=126, mom>50%, 10pos, TSL=12%, 504d = 20.7% CAGR, -27.2% MDD
    p1_grid = list(product(
        [126],                  # breakout_window (126d proven best for Calmar)
        [0.40, 0.45, 0.50],    # momentum_threshold
        [8, 10, 12],           # max_positions
        [11, 12, 13, 14],      # tsl_pct (fine-tune around 12%)
        [252, 504],             # max_hold_days
    ))

    entry_cache = {}
    total_p1 = len(p1_grid)
    print(f"  {total_p1} configs")

    for idx, (bw, mom_thresh, pos, tsl, hold) in enumerate(p1_grid):
        cache_key = (bw, mom_thresh)
        if cache_key not in entry_cache:
            entry_cache[cache_key] = compute_breakout_entries(
                price_data, bw, 126, mom_thresh, start_epoch=start_epoch)

        entries = entry_cache[cache_key]
        params = {
            "type": "breakout_refined",
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
        flag = " ***" if cagr > 20 and mdd > -30 else ""
        print(f"  [{idx+1}/{total_p1}] mom>{mom_thresh*100:.0f}% pos={pos} "
              f"tsl={tsl}% hold={hold}d | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 1b: Combined low-MDD configs
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 1b: Combined breakout + dip-buy (MDD < 30% target)")
    print(f"{'='*80}")

    # Compute dip-buy entries
    print("\nComputing dip-buy entries...")
    quality_universe = compute_quality_universe(
        price_data, 2, 0, rescreen_days=63, start_epoch=start_epoch)
    momentum_universe = compute_momentum_universe(
        price_data, 126, 0.20, rescreen_days=63, start_epoch=start_epoch)
    combined_universe = intersect_universes(quality_universe, momentum_universe)

    dip_entries_5 = compute_dip_entries(
        price_data, combined_universe, 63, 0.05, start_epoch=start_epoch)
    dip_entries_5 = filter_entries_by_fundamentals(
        dip_entries_5, fundamentals, 15, 0, 25, missing_mode="skip")

    dip_entries_7 = compute_dip_entries(
        price_data, combined_universe, 63, 0.07, start_epoch=start_epoch)
    dip_entries_7 = filter_entries_by_fundamentals(
        dip_entries_7, fundamentals, 15, 0, 25, missing_mode="skip")

    # Best low-MDD breakout entries
    bo_50_126 = entry_cache.get((126, 0.50)) or compute_breakout_entries(
        price_data, 126, 126, 0.50, start_epoch=start_epoch)
    bo_45_126 = entry_cache.get((126, 0.45)) or compute_breakout_entries(
        price_data, 126, 126, 0.45, start_epoch=start_epoch)

    combo_configs = [
        ("lo_A1", bo_50_126, dip_entries_7, 10, 12, 504, "bw126/m50 + dip7% 10p t12 504d"),
        ("lo_A2", bo_50_126, dip_entries_5, 10, 12, 504, "bw126/m50 + dip5% 10p t12 504d"),
        ("lo_A3", bo_50_126, dip_entries_7, 12, 12, 504, "bw126/m50 + dip7% 12p t12 504d"),
        ("lo_A4", bo_50_126, dip_entries_5, 12, 12, 504, "bw126/m50 + dip5% 12p t12 504d"),
        ("lo_A5", bo_50_126, dip_entries_7, 10, 11, 504, "bw126/m50 + dip7% 10p t11 504d"),
        ("lo_A6", bo_50_126, dip_entries_5, 10, 11, 504, "bw126/m50 + dip5% 10p t11 504d"),
        ("lo_B1", bo_45_126, dip_entries_7, 10, 12, 504, "bw126/m45 + dip7% 10p t12 504d"),
        ("lo_B2", bo_45_126, dip_entries_5, 10, 12, 504, "bw126/m45 + dip5% 10p t12 504d"),
        ("lo_B3", bo_45_126, dip_entries_7, 12, 12, 504, "bw126/m45 + dip7% 12p t12 504d"),
        ("lo_B4", bo_45_126, dip_entries_5, 12, 12, 504, "bw126/m45 + dip5% 12p t12 504d"),
    ]

    for label, bo, dp, pos, tsl, hold, desc in combo_configs:
        merged = merge_entries(bo, dp)
        params = {"type": "combined_lo", "label": label, "max_positions": pos,
                  "tsl_pct": tsl, "max_hold_days": hold, "description": desc}

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
        flag = " ***" if cagr > 22 and mdd > -30 else ""
        print(f"  {label}: CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 2: Momentum Cascade — NEW STRATEGY
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 2: MOMENTUM CASCADE — New Strategy")
    print("  Buy stocks with ACCELERATING momentum + breakout confirmation")
    print(f"{'='*80}")

    # Cascade parameters to sweep
    cascade_grid = list(product(
        [21, 42],             # fast_lookback (momentum derivative window)
        [126],                # slow_lookback (base momentum)
        [0.02, 0.05, 0.10],  # accel_threshold (min acceleration)
        [0.15, 0.20, 0.30],  # min_momentum (slow momentum gate)
        [5, 7, 10],          # max_positions
        [12, 15, 20],        # tsl_pct
        [252, 504],           # max_hold_days
    ))

    # Cache cascade entries
    cascade_cache = {}
    total_p2 = len(cascade_grid)
    print(f"  {total_p2} configs")

    for idx, (fast_lb, slow_lb, accel, min_mom, pos, tsl, hold) in enumerate(cascade_grid):
        cache_key = (fast_lb, slow_lb, accel, min_mom)
        if cache_key not in cascade_cache:
            cascade_cache[cache_key] = compute_cascade_entries(
                price_data, fast_lb, slow_lb, accel, min_mom, start_epoch=start_epoch)

        entries = cascade_cache[cache_key]
        if len(entries) < 10:
            continue  # skip configs with too few signals

        params = {
            "type": "cascade",
            "fast_lookback": fast_lb,
            "slow_lookback": slow_lb,
            "accel_threshold": accel,
            "min_momentum": min_mom,
            "max_positions": pos,
            "tsl_pct": tsl,
            "max_hold_days": hold,
        }

        r, dwl = simulate_cascade(
            price_data, entries, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl, max_hold_days=hold,
            exchange=exchange, regime_epochs=regime_epochs,
            start_epoch=start_epoch, params=params,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        flag = " ***" if cagr > 25 else (" **" if cagr > 22 else "")
        print(f"  [{idx+1}/{total_p2}] f={fast_lb}d accel>{accel*100:.0f}% "
              f"mom>{min_mom*100:.0f}% pos={pos} tsl={tsl}% hold={hold}d | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 3: Combined cascade + breakout + dip-buy (kitchen sink)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 3: Combined cascade + breakout + dip-buy")
    print(f"{'='*80}")

    # Pick best cascade entries
    best_cascade_keys = [
        (21, 126, 0.05, 0.20),
        (21, 126, 0.02, 0.20),
        (42, 126, 0.05, 0.20),
        (21, 126, 0.05, 0.15),
        (42, 126, 0.02, 0.15),
    ]

    for ck in best_cascade_keys:
        cascade_entries = cascade_cache.get(ck)
        if not cascade_entries or len(cascade_entries) < 10:
            continue

        # Triple merge: cascade + breakout + dip
        for bo_entries, dp_entries, label_suffix in [
            (bo_50_126, dip_entries_7, "bo50+dip7"),
            (bo_45_126, dip_entries_5, "bo45+dip5"),
        ]:
            for pos in [10, 12]:
                for tsl in [12, 15]:
                    # Cascade entries get priority, then breakout, then dip
                    seen = set()
                    merged = []
                    for source_entries in [cascade_entries, bo_entries, dp_entries]:
                        for e in source_entries:
                            key = (e["symbol"], e["entry_epoch"])
                            if key not in seen:
                                seen.add(key)
                                merged.append(dict(e))
                    merged.sort(key=lambda x: x["entry_epoch"])

                    label = f"triple_{ck[0]}d_a{ck[2]*100:.0f}_{label_suffix}_p{pos}_t{tsl}"
                    params = {
                        "type": "triple_combined",
                        "label": label,
                        "cascade_key": str(ck),
                        "max_positions": pos,
                        "tsl_pct": tsl,
                        "max_hold_days": 504,
                    }

                    r, dwl = simulate_portfolio(
                        merged, price_data, benchmark,
                        capital=capital, max_positions=pos,
                        tsl_pct=tsl, max_hold_days=504,
                        exchange=exchange, regime_epochs=regime_epochs,
                        strategy_name=STRATEGY_NAME, description="Triple combined",
                        params=params, start_epoch=start_epoch,
                    )
                    sweep.add_config(params, r)
                    r._day_wise_log = dwl

                    s = r.to_dict().get("summary", {})
                    cagr = (s.get("cagr") or 0) * 100
                    mdd = (s.get("max_drawdown") or 0) * 100
                    calmar = s.get("calmar_ratio") or 0
                    trades = s.get("total_trades") or 0
                    flag = " ***" if cagr > 25 else (" **" if cagr > 22 else "")
                    print(f"  {label}: CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
                          f"Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  FINAL LEADERBOARD")
    print(f"{'='*80}")

    sweep.print_leaderboard(top_n=20)

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
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"hold={params['max_hold_days']}d")

    print(f"\n{'='*80}")
    print("  TOP 10 WITH MDD < 30% (low risk)")
    print(f"{'='*80}")
    sorted_configs = sweep._sorted("calmar_ratio")
    count = 0
    for params, r in sorted_configs:
        s = r.to_dict()["summary"]
        mdd = (s.get("max_drawdown") or 0) * 100
        if mdd > -30:
            continue
        cagr = (s.get("cagr") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        count += 1
        print(f"  #{count} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades} | "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"hold={params['max_hold_days']}d")
        if count >= 10:
            break

    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    # Print best overall
    if sweep.configs:
        _, best = sorted_configs[0]
        print(f"\n{'='*80}")
        print("  BEST BY CALMAR (detailed)")
        print(f"{'='*80}")
        best.print_summary()


if __name__ == "__main__":
    main()

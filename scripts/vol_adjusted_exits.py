#!/usr/bin/env python3
"""Quality Dip-Buy with Volatility-Adjusted TSL on NSE.

Replaces uniform trailing stop loss with per-stock k * daily_vol(lookback).
Low-vol stocks get tighter stops (cut dead money faster).
High-vol stocks get wider stops (don't get shaken out by noise).

Champion baseline: fixed 10% TSL, Calmar 0.64, +23.8% CAGR, -37.0% MDD.

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
    compute_quality_universe, compute_dip_entries, compute_regime_epochs,
    compute_realized_vol, simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)

CAPITAL = 10_000_000
STRATEGY_NAME = "vol_adjusted_exits"
DESCRIPTION = ("Quality dip-buy with vol-adjusted TSL: k * daily_vol per stock at entry. "
               "Low-vol stocks get tighter stops, high-vol stocks get wider stops.")

TSL_FLOOR = 3.0   # minimum TSL %
TSL_CEIL = 15.0    # maximum TSL %


def attach_vol_tsl(entries, vol_data, k, tsl_floor=TSL_FLOOR, tsl_ceil=TSL_CEIL):
    """Set per_entry_tsl = clamp(k * daily_vol * 100, floor, ceil) on each entry.

    Uses signal-day epoch to look up vol (not entry_epoch which is next day).
    Entries without vol data get the midpoint of floor/ceil as default.

    Returns entries (mutated in place).
    """
    default_tsl = (tsl_floor + tsl_ceil) / 2.0
    attached = 0
    tsl_values = []

    for entry in entries:
        sym = entry["symbol"]
        signal_epoch = entry["epoch"]
        sym_vol = vol_data.get(sym, {})
        daily_vol = sym_vol.get(signal_epoch)

        if daily_vol and daily_vol > 0:
            raw_tsl = k * daily_vol * 100.0
            clamped = max(tsl_floor, min(tsl_ceil, raw_tsl))
            entry["per_entry_tsl"] = clamped
            tsl_values.append(clamped)
            attached += 1
        else:
            entry["per_entry_tsl"] = default_tsl

    if tsl_values:
        avg = sum(tsl_values) / len(tsl_values)
        tsl_values.sort()
        med = tsl_values[len(tsl_values) // 2]
        print(f"  Vol TSL: {attached}/{len(entries)} entries | "
              f"avg={avg:.1f}% med={med:.1f}% "
              f"min={tsl_values[0]:.1f}% max={tsl_values[-1]:.1f}%")
    else:
        print(f"  Vol TSL: 0/{len(entries)} entries with vol data")

    return entries


def main():
    start_epoch = 1262304000   # 2010-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: fetching data")
    print("=" * 80)

    print("\nFetching NSE universe...")
    price_data = fetch_universe(cr, "NSE", start_epoch, end_epoch)
    if not price_data:
        print("No data. Aborting.")
        return

    print("\nFetching NIFTYBEES benchmark...")
    benchmark = fetch_benchmark(cr, "NIFTYBEES", "NSE", start_epoch, end_epoch,
                                warmup_days=250)

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, "NSE")

    # ── Fixed champion params ──
    consecutive_years = 2
    min_yearly_return = 0
    dip_threshold_pct = 5
    peak_lookback = 63
    regime_sma = 200
    max_hold_days = 504

    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, consecutive_years, min_yearly_return,
        rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing dip entries...")
    all_entries = compute_dip_entries(
        price_data, quality_universe, peak_lookback,
        dip_threshold_pct / 100.0, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, regime_sma)

    print("\nApplying fundamental filter (champion: ROE>15, PE<25, skip missing)...")
    filtered_entries = filter_entries_by_fundamentals(
        all_entries, fundamentals, 15, 0, 25, missing_mode="skip")

    # ── Pre-compute vol for all lookback windows ──
    vol_lookbacks = [30, 60, 90]
    vol_data_by_lookback = {}
    for lb in vol_lookbacks:
        print(f"\nComputing realized vol (lookback={lb}d)...")
        vol_data_by_lookback[lb] = compute_realized_vol(price_data, lookback=lb)

    # ── Sweep: vol-adjusted TSL ──
    vol_grid = list(product(
        [3, 4, 5, 6, 7],      # k (multiplier on daily vol)
        vol_lookbacks,          # vol_lookback_days
        [5, 10],                # max_positions
    ))

    # Fixed-TSL baselines for comparison
    fixed_grid = list(product(
        [5, 7, 10, 15],   # fixed tsl_pct
        [5, 10],            # max_positions
    ))

    total = len(vol_grid) + len(fixed_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({len(vol_grid)} vol-adjusted + {len(fixed_grid)} fixed baselines)")
    print(f"  Fixed: {consecutive_years}yr quality, ROE>15% PE<25, {dip_threshold_pct}% dip, "
          f"{peak_lookback}d peak, regime={regime_sma}, hold={max_hold_days}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "NSE", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    # ── Vol-adjusted configs ──
    for idx, (k, vol_lb, pos) in enumerate(vol_grid):
        entries_copy = copy.deepcopy(filtered_entries)
        vol_data = vol_data_by_lookback[vol_lb]
        entries_copy = attach_vol_tsl(entries_copy, vol_data, k)

        params = {
            "mode": "vol_adjusted",
            "k": k,
            "vol_lookback": vol_lb,
            "max_positions": pos,
            "tsl_floor": TSL_FLOOR,
            "tsl_ceil": TSL_CEIL,
        }

        r, dwl = simulate_portfolio(
            entries_copy, price_data, benchmark,
            capital=CAPITAL, max_positions=pos,
            tsl_pct=10,  # global fallback (won't be used -- all entries have per_entry_tsl)
            max_hold_days=max_hold_days,
            exchange="NSE",
            regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME,
            description=DESCRIPTION,
            params=params,
            start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [{idx+1}/{total}] k={k} vol_lb={vol_lb} pos={pos} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ── Fixed-TSL baselines ──
    for idx, (fixed_tsl, pos) in enumerate(fixed_grid):
        params = {"mode": "fixed", "tsl_pct": fixed_tsl, "max_positions": pos}

        r, dwl = simulate_portfolio(
            filtered_entries, price_data, benchmark,
            capital=CAPITAL, max_positions=pos,
            tsl_pct=fixed_tsl,
            max_hold_days=max_hold_days,
            exchange="NSE",
            regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME,
            description=DESCRIPTION,
            params=params,
            start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [F{idx+1}/{len(fixed_grid)}] fixed_tsl={fixed_tsl}% pos={pos} | "
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
        adj = compute_always_invested(dwl, benchmark, CAPITAL)
        if adj:
            s = r.to_dict()["summary"]
            mode = params.get("mode", "?")
            if mode == "vol_adjusted":
                label = f"k={params['k']} lb={params['vol_lookback']} pos={params['max_positions']}"
            else:
                label = f"fixed={params['tsl_pct']}% pos={params['max_positions']}"
            print(f"  #{i+1} {mode} {label} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

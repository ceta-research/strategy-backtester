#!/usr/bin/env python3
"""Quality Dip-Buy with Multi-Tier Averaging on NSE.

Instead of 1 buy at X% dip, buys in tiers at progressively deeper dip levels:
  Tier 1: 1/n at dip_threshold (e.g., 5%)
  Tier 2: 1/n at dip_threshold * tier_mult (e.g., 7.5%)
  Tier 3: 1/n at dip_threshold * tier_mult^2 (e.g., 11.25%)

All tiers exit together when combined position triggers exit condition.

Tests whether DCA into dips lowers average entry and improves returns.

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
)

CAPITAL = 10_000_000
STRATEGY_NAME = "quality_dip_buy_tiered"
DESCRIPTION = ("Quality dip-buy with multi-tier averaging: DCA into deeper dips "
               "at 1/n allocation per tier. All tiers exit together.")


def generate_tiered_entries(price_data, quality_universe, peak_lookback,
                            base_dip_pct, n_tiers, tier_multiplier, start_epoch):
    """Generate entry signals at multiple dip tiers.

    For n_tiers=3 and base_dip=5%, tier_mult=1.5:
      Tier 1: 5% dip
      Tier 2: 7.5% dip
      Tier 3: 11.25% dip

    Returns list of entry dicts with added 'tier' field (1-based).
    """
    all_entries = []

    for tier in range(1, n_tiers + 1):
        dip_threshold = base_dip_pct * (tier_multiplier ** (tier - 1))

        tier_entries = compute_dip_entries(
            price_data, quality_universe, peak_lookback,
            dip_threshold / 100.0, start_epoch=start_epoch)

        for e in tier_entries:
            e["tier"] = tier
            e["tier_dip_threshold"] = dip_threshold

        all_entries.extend(tier_entries)

    # Sort by entry_epoch, then by tier (lower tier first within same epoch)
    all_entries.sort(key=lambda x: (x["entry_epoch"], x["tier"]))

    # Deduplicate: same (symbol, entry_epoch) should only appear once per tier
    seen = set()
    deduped = []
    for e in all_entries:
        key = (e["symbol"], e["entry_epoch"], e["tier"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    print(f"  Tiered entries: {len(deduped)} signals "
          f"({n_tiers} tiers, base={base_dip_pct}%, mult={tier_multiplier})")
    return deduped


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

    # Fixed best params from NSE baseline
    consecutive_years = 2
    peak_lookback = 63
    regime_sma = 200
    tsl_pct = 10
    max_hold_days = 504

    # Pre-compute shared data
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, consecutive_years, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, regime_sma)

    # ── Sweep ──
    param_grid = list(product(
        [1, 2, 3],       # n_tiers
        [1.5, 2.0],      # tier_multiplier
        [5, 7],           # base_dip_threshold_pct
        [5, 10],          # max_positions (unique stocks)
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs (multi-tier averaging)")
    print(f"  Fixed: {consecutive_years}yr, peak={peak_lookback}d, "
          f"regime={regime_sma}, TSL={tsl_pct}%, hold={max_hold_days}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "NSE", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (n_tiers, tier_mult, base_dip, pos) in enumerate(param_grid):
        params = {
            "n_tiers": n_tiers,
            "tier_multiplier": tier_mult,
            "base_dip_pct": base_dip,
            "max_positions": pos,
            "tsl_pct": tsl_pct,
            "max_hold_days": max_hold_days,
        }

        # Generate tiered entries
        entries = generate_tiered_entries(
            price_data, quality_universe, peak_lookback,
            base_dip, n_tiers, tier_mult, start_epoch)

        # For multi-tier: allow multiple positions per instrument
        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos * n_tiers,  # total slots = stocks * tiers
            max_per_instrument=n_tiers,
            tsl_pct=tsl_pct,
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
        print(f"  [{idx+1}/{total}] tiers={n_tiers} mult={tier_mult} "
              f"dip={base_dip}% pos={pos} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ── Always-invested ──
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, CAPITAL)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} tiers={params['n_tiers']} mult={params['tier_multiplier']} "
                  f"dip={params['base_dip_pct']}% | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

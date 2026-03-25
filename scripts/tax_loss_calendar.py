#!/usr/bin/env python3
"""Tax-Loss Harvesting Calendar Strategy (Strategy 1b/1c).

Buys quality stocks that drop during tax-loss selling season, holds for
fixed period through the recovery.

US (1b): Fiscal year ends Dec 31. Selling season Oct-Dec. Buy dips during
         Oct-Dec, hold 30-90 days (January effect recovery).
NSE (1c): Fiscal year ends Mar 31. Selling season Jan-Mar. Buy dips during
          Jan-Mar, hold 30-90 days (April recovery).

Signal:
  - Quality stock (2yr consecutive positive returns)
  - Fundamental overlay (ROE>15%, PE<25)
  - Stock drops >= threshold from rolling peak during selling season months
  - Entry: next-day open (MOC execution)
  - Exit: fixed holding period (no early exit)

Supports --market nse (default) and --market us.

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os
import time
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_dip_entries,
    simulate_portfolio, compute_always_invested,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)

CAPITAL = 10_000_000

# Fixed best params from quality dip-buy experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63

# Fundamental overlay (proven best)
ROE_THRESHOLD = 15
PE_THRESHOLD = 25

# Market-specific config
MARKET_CONFIG = {
    "nse": {
        "exchange": "NSE",
        "benchmark": "NIFTYBEES",
        "source": "native",
        "start_epoch": 1262304000,   # 2010-01-01
        "end_epoch": 1773878400,     # 2026-03-19
        "selling_months": {1, 2, 3},  # Jan-Mar (fiscal year ends Mar 31)
        "season_label": "Jan-Mar",
    },
    "us": {
        "exchange": "US",
        "benchmark": "SPY",
        "source": "fmp",
        "start_epoch": 1104537600,   # 2005-01-01
        "end_epoch": 1773878400,     # 2026-03-19
        "selling_months": {10, 11, 12},  # Oct-Dec (fiscal year ends Dec 31)
        "season_label": "Oct-Dec",
    },
}


def epoch_to_month(epoch):
    """Convert epoch to month number (1-12)."""
    t = time.gmtime(epoch)
    return t.tm_mon


def filter_entries_by_season(entries, selling_months):
    """Keep only entries whose signal day falls in the selling season."""
    filtered = []
    for e in entries:
        month = epoch_to_month(e["epoch"])
        if month in selling_months:
            filtered.append(e)

    print(f"  Calendar filter: {len(filtered)}/{len(entries)} entries in selling season")
    return filtered


def main():
    # Parse market flag
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    cfg = MARKET_CONFIG[market]
    exchange = cfg["exchange"]
    strategy_name = f"tax_loss_calendar_{market}"
    description = (f"Tax-loss calendar on {exchange}: buy quality dips during "
                   f"{cfg['season_label']} selling season, hold for fixed period.")

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {strategy_name}: fetching data")
    print(f"  Selling season: {cfg['season_label']}")
    print("=" * 80)

    # ── 1. Fetch data ──
    print(f"\nFetching {exchange} universe...")
    price_data = fetch_universe(cr, exchange, cfg["start_epoch"], cfg["end_epoch"],
                                warmup_days=800, source=cfg["source"])
    if not price_data:
        print("No data. Aborting.")
        return

    print(f"\nFetching {cfg['benchmark']} benchmark...")
    benchmark = fetch_benchmark(cr, cfg["benchmark"], exchange,
                                cfg["start_epoch"], cfg["end_epoch"],
                                source=cfg["source"])

    # ── 2. Compute quality universe ──
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63,
        start_epoch=cfg["start_epoch"])

    # ── 3. Compute dip entries at multiple thresholds ──
    print("\nComputing dip entries...")
    entries_by_dip = {}
    for dip_pct in [7, 10, 15, 20]:
        entries = compute_dip_entries(
            price_data, quality_universe, PEAK_LOOKBACK,
            dip_pct / 100.0, start_epoch=cfg["start_epoch"])
        entries_by_dip[dip_pct] = entries

    # ── 4. Fundamental overlay ──
    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    for dip_pct in entries_by_dip:
        entries_by_dip[dip_pct] = filter_entries_by_fundamentals(
            entries_by_dip[dip_pct], fundamentals,
            ROE_THRESHOLD, 0, PE_THRESHOLD, missing_mode="skip")

    # ── 5. Calendar filter ──
    print("\nApplying calendar filter...")
    for dip_pct in entries_by_dip:
        entries_by_dip[dip_pct] = filter_entries_by_season(
            entries_by_dip[dip_pct], cfg["selling_months"])

    # ── 6. Sweep ──
    # dip_threshold × hold_days × max_positions
    dip_thresholds = [7, 10, 15, 20]
    hold_days_list = [30, 60, 90]
    position_counts = [5, 10]

    param_grid = list(product(dip_thresholds, hold_days_list, position_counts))
    total = len(param_grid)

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({strategy_name})")
    print(f"  Selling season: {cfg['season_label']}")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, peak={PEAK_LOOKBACK}d, "
          f"ROE>{ROE_THRESHOLD}%, PE<{PE_THRESHOLD}")
    print(f"{'='*80}")

    sweep = SweepResult(strategy_name, "PORTFOLIO", exchange, CAPITAL,
                        slippage_bps=5, description=description)

    for idx, (dip_pct, hold_days, pos) in enumerate(param_grid):
        params = {
            "dip_threshold_pct": dip_pct,
            "hold_days": hold_days,
            "max_positions": pos,
            "selling_season": cfg["season_label"],
        }

        entries = entries_by_dip.get(dip_pct, [])

        # Fixed hold period: use tsl_pct=99 (never triggers trailing stop)
        # and max_hold_days=hold_days for pure time-based exit.
        # Set peak_price very high so peak recovery never triggers.
        hold_entries = []
        for e in entries:
            he = dict(e)
            he["peak_price"] = e["entry_price"] * 100  # unreachable peak
            hold_entries.append(he)

        r, dwl = simulate_portfolio(
            hold_entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos,
            tsl_pct=99,  # effectively disabled
            max_hold_days=hold_days,
            exchange=exchange,
            strategy_name=strategy_name,
            description=description,
            params=params,
            start_epoch=cfg["start_epoch"],
        )

        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        wr = (s.get("win_rate") or 0) * 100
        print(f"  [{idx+1}/{total}] dip={dip_pct}% hold={hold_days}d pos={pos} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} "
              f"WR={wr:.0f}% T={trades}")

    # ── Always-invested adjustment ──
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
            print(f"  #{i+1} dip={params['dip_threshold_pct']}% "
                  f"hold={params['hold_days']}d pos={params['max_positions']} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    # ── Hold period comparison ──
    print(f"\n{'='*80}")
    print("  HOLD PERIOD COMPARISON")
    print(f"{'='*80}")

    for hold in hold_days_list:
        hold_configs = [(p, r) for p, r in sweep.configs if p["hold_days"] == hold]
        if hold_configs:
            calmars = [(r.to_dict()["summary"].get("calmar_ratio") or 0) for _, r in hold_configs]
            avg_calmar = sum(calmars) / len(calmars)
            best_calmar = max(calmars)
            print(f"  {hold}d hold: avg Calmar={avg_calmar:.2f}, best Calmar={best_calmar:.2f}")

    sweep.print_leaderboard(top_n=24)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

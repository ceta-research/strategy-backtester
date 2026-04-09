#!/usr/bin/env python3
"""Quality Dip-Buy with Intraday Execution on NSE.

Uses daily signals (quality filter + dip detection from EOD data) but
compares different execution prices from pre-aggregated minute data:
  - open: next-day open (baseline)
  - vwap: next-day VWAP
  - near_low: next-day low * 1.02 (optimistic bound)
  - midpoint: (open + low) / 2

Tests whether better fills improve returns enough to justify complexity.

Data: fmp.stock_prices_minute (Nov 2022 onwards for most liquid stocks).
Timestamps are LOCAL time labeled as UTC.

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
STRATEGY_NAME = "quality_dip_buy_intraday"
DESCRIPTION = ("Quality dip-buy with intraday execution: compare open vs VWAP "
               "vs near-low vs midpoint fills from minute data.")


def fetch_intraday_aggregates(cr, symbols, start_epoch, end_epoch):
    """Fetch pre-aggregated daily VWAP, low, open from minute data.

    Returns dict[symbol, dict[epoch, {vwap, day_low, day_open}]]
    where symbol is bare (no .NS suffix).
    """
    # Build .NS suffix symbols for FMP query
    ns_symbols = [f"{s}.NS" for s in symbols]
    sym_list = ", ".join(f"'{s}'" for s in ns_symbols)

    # Timestamps in stock_prices_minute are LOCAL time labeled UTC
    # NSE trading: 09:15-15:30 IST, stored as "UTC" in FMP
    sql = f"""
    SELECT symbol,
           (CAST(dateEpoch / 86400 AS BIGINT) * 86400) AS day_epoch,
           SUM(close * volume) / NULLIF(SUM(volume), 0) AS vwap,
           MIN(low) AS day_low
    FROM fmp.stock_prices_minute
    WHERE symbol IN ({sym_list})
      AND dateEpoch >= {start_epoch} AND dateEpoch <= {end_epoch}
    GROUP BY symbol, (CAST(dateEpoch / 86400 AS BIGINT) * 86400)
    HAVING COUNT(*) >= 10
    ORDER BY symbol, day_epoch
    """

    print(f"  Fetching intraday aggregates for {len(symbols)} symbols...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  WARNING: No intraday data fetched")
        return {}

    intraday = {}
    for r in results:
        sym = r["symbol"]
        if sym.endswith(".NS"):
            sym = sym[:-3]

        epoch = int(r.get("day_epoch") or 0)
        vwap = r.get("vwap")
        day_low = r.get("day_low")

        if epoch <= 0 or vwap is None:
            continue

        if sym not in intraday:
            intraday[sym] = {}
        intraday[sym][epoch] = {
            "vwap": float(vwap),
            "day_low": float(day_low) if day_low else float(vwap),
        }

    total_days = sum(len(v) for v in intraday.values())
    print(f"  Loaded intraday data: {len(intraday)} symbols, {total_days} symbol-days")
    return intraday


def build_execution_prices(intraday, price_data, model):
    """Build execution price overrides for a given model.

    For "midpoint", uses EOD open from price_data + intraday low.

    Returns dict[symbol, dict[epoch, price]]
    """
    # Build EOD open lookup for midpoint model
    eod_open = {}
    if model == "midpoint":
        for sym, bars in price_data.items():
            eod_open[sym] = {b["epoch"]: b["open"] for b in bars}

    exec_prices = {}
    for sym, days in intraday.items():
        exec_prices[sym] = {}
        for epoch, data in days.items():
            if model == "vwap":
                exec_prices[sym][epoch] = data["vwap"]
            elif model == "near_low":
                exec_prices[sym][epoch] = data["day_low"] * 1.02
            elif model == "midpoint":
                o = eod_open.get(sym, {}).get(epoch, data["vwap"])
                l = data["day_low"]
                exec_prices[sym][epoch] = (o + l) / 2
    return exec_prices


def main():
    # Minute data available from ~Nov 2022 for most liquid NSE stocks
    start_epoch = 1667260800   # 2022-11-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: fetching data")
    print("=" * 80)

    # Use shorter warmup since we only have ~3 years of minute data
    # but quality filter needs 2yr lookback, so EOD data starts earlier
    eod_start = 1577836800  # 2020-01-01 (2yr before minute data start)

    print("\nFetching NSE universe (EOD)...")
    price_data = fetch_universe(cr, "NSE", eod_start, end_epoch, warmup_days=600)
    if not price_data:
        print("No data. Aborting.")
        return

    print("\nFetching NIFTYBEES benchmark...")
    benchmark = fetch_benchmark(cr, "NIFTYBEES", "NSE", eod_start, end_epoch)

    # Fixed best params
    consecutive_years = 2
    peak_lookback = 63
    regime_sma = 200
    tsl_pct = 10
    max_hold_days = 504

    # Pre-compute
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, consecutive_years, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, regime_sma)

    # Compute entries with both dip thresholds
    print("\nComputing dip entries...")
    entries_5 = compute_dip_entries(
        price_data, quality_universe, peak_lookback, 0.05, start_epoch=start_epoch)
    entries_7 = compute_dip_entries(
        price_data, quality_universe, peak_lookback, 0.07, start_epoch=start_epoch)

    # Collect symbols that have entry signals for intraday fetch
    signal_symbols = set()
    for e in entries_5 + entries_7:
        signal_symbols.add(e["symbol"])

    print(f"\n{len(signal_symbols)} symbols have entry signals")

    # Fetch intraday aggregates only for signal symbols
    print("\nFetching intraday minute aggregates...")
    intraday = fetch_intraday_aggregates(cr, list(signal_symbols), start_epoch, end_epoch)

    # ── Sweep ──
    execution_models = ["open", "vwap", "near_low", "midpoint"]
    dip_configs = [(5, entries_5), (7, entries_7)]
    position_counts = [5, 10]

    param_grid = list(product(execution_models, dip_configs, position_counts))
    total = len(param_grid)

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs (intraday execution)")
    print(f"  Period: Nov 2022 - Mar 2026 (~3.3 years)")
    print(f"  Fixed: {consecutive_years}yr, peak={peak_lookback}d, "
          f"regime={regime_sma}, TSL={tsl_pct}%, hold={max_hold_days}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "NSE", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (model, (dip, entries), pos) in enumerate(param_grid):
        params = {
            "execution_model": model,
            "dip_threshold_pct": dip,
            "max_positions": pos,
        }

        # Build execution price overrides
        if model == "open":
            exec_prices = None  # use default (next day's open from EOD)
        else:
            exec_prices = build_execution_prices(intraday, price_data, model)

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos,
            tsl_pct=tsl_pct,
            max_hold_days=max_hold_days,
            exchange="NSE",
            regime_epochs=regime_epochs,
            execution_prices=exec_prices,
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
        print(f"  [{idx+1}/{total}] exec={model:10s} dip={dip}% pos={pos} | "
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
            print(f"  #{i+1} exec={params['execution_model']:10s} "
                  f"dip={params['dip_threshold_pct']}% | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    # ── Execution model comparison ──
    print(f"\n{'='*80}")
    print("  EXECUTION MODEL COMPARISON")
    print(f"{'='*80}")

    for model in execution_models:
        model_configs = [(p, r) for p, r in sweep.configs if p["execution_model"] == model]
        if model_configs:
            cagrs = [(r.to_dict()["summary"].get("cagr") or 0) for _, r in model_configs]
            avg_cagr = sum(cagrs) / len(cagrs) * 100
            best_cagr = max(cagrs) * 100
            print(f"  {model:10s}: avg CAGR={avg_cagr:+.1f}%, best CAGR={best_cagr:+.1f}%")

    sweep.print_leaderboard(top_n=16)
    sweep.save("result.json", top_n=16, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

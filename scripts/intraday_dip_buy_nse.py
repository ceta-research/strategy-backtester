#!/usr/bin/env python3
"""Strategy 3e: Quality Dip-Buy with Intraday Execution on NSE Native Minute Data.

Validates the quality dip-buy strategy on 8 years of NSE native minute data
(2015-02 to 2022-10) covering the 2018 correction and 2020 COVID crash.

Uses daily signals (quality filter + dip detection from EOD data) but
compares different execution prices from nse.nse_charting_minute:
  - open: next-day open (baseline, same as EOD version)
  - vwap: next-day VWAP (volume-weighted average price)
  - near_low: next-day MIN(open,close) * 1.02 (optimistic bound)
  - midpoint: (open + near_low) / 2

NSE native minute data columns: symbol, date_epoch, open, close, volume
(no high/low columns -- uses LEAST(open, close) as low proxy).

Fundamental overlay: ROE>15%, PE<25 from fmp.financial_ratios (proven best).

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
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)

CAPITAL = 10_000_000
STRATEGY_NAME = "intraday_dip_buy_nse"
DESCRIPTION = ("Quality dip-buy with intraday execution on NSE native minute data "
               "(2015-2022). Compares open vs VWAP vs near-low execution. "
               "Fundamental overlay: ROE>15%, PE<25.")

# Fixed best params from quality dip-buy experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


def fetch_nse_minute_aggregates(cr, symbols, start_epoch, end_epoch):
    """Fetch daily VWAP, low proxy from nse.nse_charting_minute.

    NSE native minute data has no high/low columns, so we use:
    - VWAP: SUM(close * volume) / SUM(volume)
    - day_low: MIN(LEAST(open, close)) as proxy for intraday low

    Returns dict[symbol, dict[epoch, {vwap, day_low}]]
    """
    # Batch symbols to stay under query size limits
    BATCH_SIZE = 200
    all_results = []

    sym_list_all = list(symbols)
    for batch_start in range(0, len(sym_list_all), BATCH_SIZE):
        batch = sym_list_all[batch_start:batch_start + BATCH_SIZE]
        sym_str = ", ".join(f"'{s}'" for s in batch)

        sql = f"""
        SELECT symbol,
               (CAST(date_epoch / 86400 AS BIGINT) * 86400) AS day_epoch,
               SUM(close * volume) / NULLIF(SUM(volume), 0) AS vwap,
               MIN(LEAST(open, close)) AS day_low
        FROM nse.nse_charting_minute
        WHERE symbol IN ({sym_str})
          AND date_epoch >= {start_epoch} AND date_epoch <= {end_epoch}
        GROUP BY symbol, (CAST(date_epoch / 86400 AS BIGINT) * 86400)
        HAVING COUNT(*) >= 10
        ORDER BY symbol, day_epoch
        """

        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(sym_list_all) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  Fetching minute aggregates batch {batch_num}/{total_batches} "
              f"({len(batch)} symbols)...")
        results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                           memory_mb=16384, threads=6)
        if results:
            all_results.extend(results)

    if not all_results:
        print("  WARNING: No minute data fetched")
        return {}

    intraday = {}
    for r in all_results:
        sym = r["symbol"]
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
    print(f"  Loaded minute aggregates: {len(intraday)} symbols, {total_days} symbol-days")
    return intraday


def build_execution_prices(intraday, price_data, model):
    """Build execution price overrides for a given model.

    Returns dict[symbol, dict[epoch, price]]
    """
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
    # NSE native minute data: 2015-02 to 2022-10
    # EOD data starts earlier for quality filter warmup (2yr lookback)
    minute_start = 1422748800   # 2015-02-01
    minute_end = 1664582400     # 2022-10-01
    eod_start = 1293840000      # 2011-01-01 (4yr warmup for 2yr quality filter)

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: fetching data")
    print(f"  Period: Feb 2015 - Oct 2022 (~7.7 years)")
    print("=" * 80)

    # ── 1. Fetch EOD data for signal generation ──
    print("\nFetching NSE universe (EOD, native)...")
    price_data = fetch_universe(cr, "NSE", eod_start, minute_end,
                                warmup_days=600, source="native")
    if not price_data:
        print("No data. Aborting.")
        return

    print("\nFetching NIFTYBEES benchmark...")
    benchmark = fetch_benchmark(cr, "NIFTYBEES", "NSE", eod_start, minute_end,
                                source="native")

    # ── 2. Compute quality universe + dip entries ──
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=minute_start)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    print("\nComputing dip entries...")
    entries_5 = compute_dip_entries(
        price_data, quality_universe, PEAK_LOOKBACK, 0.05, start_epoch=minute_start)
    entries_7 = compute_dip_entries(
        price_data, quality_universe, PEAK_LOOKBACK, 0.07, start_epoch=minute_start)

    # ── 3. Fundamental overlay ──
    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, "NSE")

    entries_5_fund = filter_entries_by_fundamentals(
        entries_5, fundamentals, ROE_THRESHOLD, 0, PE_THRESHOLD, missing_mode="skip")
    entries_7_fund = filter_entries_by_fundamentals(
        entries_7, fundamentals, ROE_THRESHOLD, 0, PE_THRESHOLD, missing_mode="skip")

    # ── 4. Fetch minute aggregates for signal symbols ──
    signal_symbols = set()
    for e in entries_5_fund + entries_7_fund:
        signal_symbols.add(e["symbol"])

    print(f"\n{len(signal_symbols)} symbols have entry signals")

    print("\nFetching NSE native minute aggregates...")
    intraday = fetch_nse_minute_aggregates(cr, list(signal_symbols),
                                           minute_start, minute_end)

    # ── 5. Sweep ──
    execution_models = ["open", "vwap", "near_low", "midpoint"]
    dip_configs = [(5, entries_5_fund), (7, entries_7_fund)]
    position_counts = [5, 10]

    param_grid = list(product(execution_models, dip_configs, position_counts))
    total = len(param_grid)

    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs (intraday execution on NSE native)")
    print(f"  Period: Feb 2015 - Oct 2022 (~7.7 years)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr, peak={PEAK_LOOKBACK}d, "
          f"regime={REGIME_SMA}, TSL={TSL_PCT}%, hold={MAX_HOLD_DAYS}d")
    print(f"  Fundamental: ROE>{ROE_THRESHOLD}%, PE<{PE_THRESHOLD}")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "NSE", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (model, (dip, entries), pos) in enumerate(param_grid):
        params = {
            "execution_model": model,
            "dip_threshold_pct": dip,
            "max_positions": pos,
        }

        if model == "open":
            exec_prices = None
        else:
            exec_prices = build_execution_prices(intraday, price_data, model)

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos,
            tsl_pct=TSL_PCT,
            max_hold_days=MAX_HOLD_DAYS,
            exchange="NSE",
            regime_epochs=regime_epochs,
            execution_prices=exec_prices,
            strategy_name=STRATEGY_NAME,
            description=DESCRIPTION,
            params=params,
            start_epoch=minute_start,
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
            print(f"  #{i+1} exec={params['execution_model']:10s} "
                  f"dip={params['dip_threshold_pct']}% pos={params['max_positions']} | "
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
            calmars = [(r.to_dict()["summary"].get("calmar_ratio") or 0) for _, r in model_configs]
            avg_cagr = sum(cagrs) / len(cagrs) * 100
            avg_calmar = sum(calmars) / len(calmars)
            best_calmar = max(calmars)
            print(f"  {model:10s}: avg CAGR={avg_cagr:+.1f}%, "
                  f"avg Calmar={avg_calmar:.2f}, best Calmar={best_calmar:.2f}")

    sweep.print_leaderboard(top_n=16)
    sweep.save("result.json", top_n=16, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

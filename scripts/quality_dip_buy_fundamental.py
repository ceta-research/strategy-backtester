#!/usr/bin/env python3
"""Quality Dip-Buy with Fundamental Overlay on NSE.

Adds ROE, debt/equity, and P/E filters from fmp.financial_ratios_ttm
to the quality dip-buy strategy. 45-day filing lag applied.

Tests whether fundamental quality filters reduce losers and improve returns.

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
    fetch_universe, fetch_benchmark, fetch_sector_map,
    compute_quality_universe, compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
)

CAPITAL = 10_000_000
STRATEGY_NAME = "quality_dip_buy_fundamental"
DESCRIPTION = ("Quality dip-buy with fundamental overlay: ROE, debt/equity, P/E "
               "filters from fmp.financial_ratios_ttm with 45-day filing lag.")

FILING_LAG_DAYS = 45


# ── Fundamental Data ─────────────────────────────────────────────────────────

def fetch_fundamentals(cr, exchange):
    """Fetch FY fundamental ratios. Returns dict[symbol, list[{epoch, roe, de, pe}]].

    Uses fmp.financial_ratios (FY data with dateEpoch) not financial_ratios_ttm
    (snapshot-only, no history). ROE computed as netIncomePerShare / shareholdersEquityPerShare.
    """
    from scripts.quality_dip_buy_lib import FMP_EXCHANGES

    # Determine suffix filter for SQL and suffix to strip from symbols
    suffix = ""
    if exchange == "NSE":
        suffix_filter = "symbol LIKE '%.NS'"
        suffix = ".NS"
    elif exchange in FMP_EXCHANGES:
        s = FMP_EXCHANGES[exchange]["suffix"]
        suffix_filter = f"symbol LIKE '%{s}'"
        suffix = s
    else:
        suffix_filter = "1=1"

    sql = f"""
    SELECT symbol, CAST(dateEpoch AS BIGINT) AS dateEpoch,
           netIncomePerShare, shareholdersEquityPerShare,
           debtToEquityRatio, priceToEarningsRatio
    FROM fmp.financial_ratios
    WHERE {suffix_filter}
      AND period = 'FY'
      AND shareholdersEquityPerShare IS NOT NULL
      AND shareholdersEquityPerShare > 0
    ORDER BY symbol, dateEpoch
    """
    print("  Fetching fundamental ratios (FY)...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  WARNING: No fundamental data fetched")
        return {}

    fundamentals = {}
    for r in results:
        sym = r["symbol"]
        if suffix and sym.endswith(suffix):
            sym = sym[:-len(suffix)]

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        # Compute ROE = net income per share / shareholders equity per share
        ni = r.get("netIncomePerShare")
        eq = r.get("shareholdersEquityPerShare")
        roe = (float(ni) / float(eq) * 100) if (ni is not None and eq and float(eq) > 0) else None

        de = r.get("debtToEquityRatio")
        pe = r.get("priceToEarningsRatio")

        if sym not in fundamentals:
            fundamentals[sym] = []
        fundamentals[sym].append({
            "epoch": epoch,
            "roe": roe,
            "de": float(de) if de is not None else None,
            "pe": float(pe) if pe is not None else None,
        })

    # Sort by epoch within each symbol
    for sym in fundamentals:
        fundamentals[sym].sort(key=lambda x: x["epoch"])

    print(f"  Loaded fundamentals for {len(fundamentals)} symbols, "
          f"{sum(len(v) for v in fundamentals.values())} data points")
    return fundamentals


def get_fundamental_at(fundamentals, symbol, epoch, lag_days=FILING_LAG_DAYS):
    """Get latest fundamental data before lag-adjusted epoch.

    Returns dict {roe, de, pe} or None if no data available.
    """
    records = fundamentals.get(symbol)
    if not records:
        return None

    lag_epoch = epoch - lag_days * 86400
    best = None
    for rec in records:
        if rec["epoch"] <= lag_epoch:
            best = rec
        else:
            break

    return best


def filter_entries_by_fundamentals(entries, fundamentals, roe_threshold, de_threshold,
                                   pe_threshold, missing_mode="pass"):
    """Filter entry signals by fundamental criteria.

    Args:
        entries: list of entry dicts from compute_dip_entries()
        fundamentals: from fetch_fundamentals()
        roe_threshold: min ROE % (0 = off)
        de_threshold: max debt/equity ratio (0 = off)
        pe_threshold: max P/E ratio (0 = off)
        missing_mode: "pass" = allow stocks without data, "skip" = exclude them

    Returns:
        filtered list of entries
    """
    if roe_threshold == 0 and de_threshold == 0 and pe_threshold == 0:
        return entries

    filtered = []
    skipped_no_data = 0
    skipped_filter = 0

    for entry in entries:
        sym = entry["symbol"]
        fund = get_fundamental_at(fundamentals, sym, entry["epoch"])

        if fund is None:
            if missing_mode == "pass":
                filtered.append(entry)
            else:
                skipped_no_data += 1
            continue

        passes = True

        if roe_threshold > 0:
            roe = fund.get("roe")
            if roe is not None and roe < roe_threshold:
                passes = False

        if de_threshold > 0 and passes:
            de = fund.get("de")
            if de is not None and de > de_threshold:
                passes = False

        if pe_threshold > 0 and passes:
            pe = fund.get("pe")
            if pe is not None and (pe <= 0 or pe > pe_threshold):
                passes = False

        if passes:
            filtered.append(entry)
        else:
            skipped_filter += 1

    print(f"  Fundamental filter: {len(filtered)}/{len(entries)} passed "
          f"(ROE>{roe_threshold}% DE<{de_threshold} PE<{pe_threshold}, "
          f"skipped: {skipped_filter} failed, {skipped_no_data} no data)")
    return filtered


# ── Main ─────────────────────────────────────────────────────────────────────

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

    # Fixed best params from NSE baseline
    consecutive_years = 2
    min_yearly_return = 0
    dip_threshold_pct = 5
    peak_lookback = 63
    regime_sma = 200
    tsl_pct = 10
    max_hold_days = 504

    # Pre-compute shared data
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

    # ── Sweep ──
    param_grid = list(product(
        [0, 10, 15],      # roe_threshold (% -- 0=off, 10%, 15%)
        [0, 1.0, 2.0],    # de_threshold (max ratio -- 0=off)
        [0, 20, 25],       # pe_threshold (max P/E -- 0=off)
        [5, 10],           # max_positions
        ["pass", "skip"],  # missing_mode
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs (fundamental overlay)")
    print(f"  Fixed: {consecutive_years}yr, {dip_threshold_pct}% dip, {peak_lookback}d peak, "
          f"regime={regime_sma}, TSL={tsl_pct}%, hold={max_hold_days}d")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "NSE", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (roe, de, pe, pos, missing) in enumerate(param_grid):
        params = {
            "roe_threshold": roe,
            "de_threshold": de,
            "pe_threshold": pe,
            "max_positions": pos,
            "missing_mode": missing,
        }

        # Filter entries by fundamentals
        filtered_entries = filter_entries_by_fundamentals(
            all_entries, fundamentals, roe, de, pe, missing_mode=missing)

        r, dwl = simulate_portfolio(
            filtered_entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos,
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
        print(f"  [{idx+1}/{total}] ROE>{roe}% DE<{de} PE<{pe} pos={pos} "
              f"miss={missing} | "
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
            print(f"  #{i+1} ROE>{params['roe_threshold']} DE<{params['de_threshold']} "
                  f"PE<{params['pe_threshold']} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

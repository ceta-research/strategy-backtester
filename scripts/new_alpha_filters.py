#!/usr/bin/env python3
"""New Alpha Filters Sweep: Gross Profitability, Revenue Growth, Tighter P/E, Current Ratio.

Based on feature importance analysis (feature_importance.py, 2289 entries):
- Gross profitability (GP/Assets): Q1 64.8% → Q4 70.2% win rate
- Revenue growth: Q1 65.0% → Q4 71.3%
- P/E ratio: tighter = better (Q1 71.6% at P/E<10 vs Q4 67.0% at P/E 20-25)
- Current ratio: non-linear, Q2 (1.3-1.7) is best at 72.3%

Tests each filter individually (Group A) and in combination (Group B)
on the champion config (63d mom, top 30%, 5% dip, D/E<1.0, 10 pos, 10% TSL).

Supports NSE (native data) and US (FMP data) via --market flag.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark, fetch_sector_map,
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, get_fundamental_at, filter_entries_by_fundamentals,
)
from scripts.momentum_dip_de_positions import intersect_universes

STRATEGY_NAME = "new_alpha_filters"

# Champion fixed params
MOMENTUM_LOOKBACK = 63
MOMENTUM_PERCENTILE = 0.30
DIP_THRESHOLD_PCT = 5
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
MAX_HOLD_DAYS = 504
TSL_PCT = 10
DE_THRESHOLD = 1.0
MAX_POSITIONS = 10
FILING_LAG_DAYS = 45


# ── Extended Fundamentals ────────────────────────────────────────────────────

def fetch_extended_fundamentals(cr, exchange):
    """Fetch gross profitability, revenue, current ratio from FMP."""
    suffix_filter = "i.symbol LIKE '%.NS'" if exchange == "NSE" else "1=1"
    sql = f"""
    SELECT i.symbol, CAST(i.dateEpoch AS BIGINT) AS dateEpoch,
           i.grossProfit, i.revenue,
           b.totalAssets, b.totalCurrentAssets, b.totalCurrentLiabilities
    FROM fmp.income_statement i
    JOIN fmp.balance_sheet b ON i.symbol = b.symbol
        AND i.fiscalYear = b.fiscalYear AND i.period = b.period
    WHERE {suffix_filter} AND i.period = 'FY'
      AND b.totalAssets IS NOT NULL AND b.totalAssets > 0
    ORDER BY i.symbol, i.dateEpoch
    """
    print("  Fetching extended fundamentals (income_statement + balance_sheet)...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  WARNING: No extended fundamental data fetched")
        return {}

    raw = {}
    for r in results:
        sym = r["symbol"]
        if exchange == "NSE" and sym.endswith(".NS"):
            sym = sym[:-3]
        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        gp = r.get("grossProfit")
        ta = r.get("totalAssets")
        rev = r.get("revenue")
        tca = r.get("totalCurrentAssets")
        tcl = r.get("totalCurrentLiabilities")

        gp_ratio = (float(gp) / float(ta)) if (gp is not None and ta and float(ta) > 0) else None
        curr_ratio = (float(tca) / float(tcl)) if (tca is not None and tcl and float(tcl) > 0) else None

        if sym not in raw:
            raw[sym] = []
        raw[sym].append({
            "epoch": epoch,
            "gross_profit_ratio": gp_ratio,
            "revenue": float(rev) if rev is not None else None,
            "current_ratio": curr_ratio,
        })

    ext = {}
    for sym, records in raw.items():
        records.sort(key=lambda x: x["epoch"])
        for i, rec in enumerate(records):
            if i > 0 and records[i - 1]["revenue"] and records[i - 1]["revenue"] > 0 and rec["revenue"]:
                rec["revenue_growth"] = (rec["revenue"] - records[i - 1]["revenue"]) / abs(records[i - 1]["revenue"])
            else:
                rec["revenue_growth"] = None
        ext[sym] = records

    print(f"  Extended fundamentals: {len(ext)} symbols, "
          f"{sum(len(v) for v in ext.values())} data points")
    return ext


# ── Extended Filter ──────────────────────────────────────────────────────────

def filter_entries_by_extended(entries, ext_fundamentals, gp_min, rev_growth_min,
                               cr_min, cr_max):
    """Filter entries by gross profitability, revenue growth, current ratio.

    Args:
        gp_min: min gross profit / total assets (0 = off)
        rev_growth_min: min YoY revenue growth fraction (-999 = off)
        cr_min: min current ratio (0 = off)
        cr_max: max current ratio (0 = no upper bound)
    """
    if gp_min <= 0 and rev_growth_min <= -999 and cr_min <= 0 and cr_max <= 0:
        return entries

    filtered = []
    skipped_no_data = 0
    skipped_filter = 0

    for entry in entries:
        sym = entry["symbol"]
        ext = get_fundamental_at(ext_fundamentals, sym, entry["epoch"],
                                 lag_days=FILING_LAG_DAYS)

        if ext is None:
            skipped_no_data += 1
            continue

        passes = True

        if gp_min > 0 and passes:
            gp = ext.get("gross_profit_ratio")
            if gp is not None and gp < gp_min:
                passes = False
            elif gp is None:
                passes = False

        if rev_growth_min > -999 and passes:
            rg = ext.get("revenue_growth")
            if rg is not None and rg < rev_growth_min:
                passes = False
            elif rg is None:
                passes = False

        if cr_min > 0 and passes:
            cr = ext.get("current_ratio")
            if cr is not None and cr < cr_min:
                passes = False
            elif cr is None:
                passes = False

        if cr_max > 0 and passes:
            cr = ext.get("current_ratio")
            if cr is not None and cr > cr_max:
                passes = False

        if passes:
            filtered.append(entry)
        else:
            skipped_filter += 1

    labels = []
    if gp_min > 0:
        labels.append(f"GP>{gp_min:.0%}")
    if rev_growth_min > -999:
        labels.append(f"RevG>{rev_growth_min:.0%}")
    if cr_min > 0:
        labels.append(f"CR>{cr_min}")
    if cr_max > 0:
        labels.append(f"CR<{cr_max}")
    label = " ".join(labels) if labels else "none"
    print(f"  Extended filter [{label}]: {len(filtered)}/{len(entries)} passed "
          f"(skipped: {skipped_filter} failed, {skipped_no_data} no data)")
    return filtered


# ── Sweep Config Builder ─────────────────────────────────────────────────────

def build_configs():
    """Build sweep configs: Group A (individual) + Group B (combined)."""
    defaults = {
        "roe_min": 15, "pe_max": 25,
        "gp_min": 0, "rev_growth_min": -999,
        "cr_min": 0, "cr_max": 0,
        "max_per_sector": 0,
    }

    configs = []

    def cfg(group, **overrides):
        c = dict(defaults)
        c["group"] = group
        c.update(overrides)
        configs.append(c)

    # ── Group A: Individual filters (12 configs) ──

    # A1: Baseline (champion as-is)
    cfg("A_baseline")

    # A2-A4: Gross profitability
    cfg("A_gp", gp_min=0.30)
    cfg("A_gp", gp_min=0.40)
    cfg("A_gp", gp_min=0.50)

    # A5-A6: Revenue growth
    cfg("A_revg", rev_growth_min=0.0)
    cfg("A_revg", rev_growth_min=0.05)

    # A7-A8: Tighter P/E
    cfg("A_pe", pe_max=20)
    cfg("A_pe", pe_max=15)

    # A9-A10: Current ratio
    cfg("A_cr", cr_min=1.0)
    cfg("A_cr", cr_min=1.0, cr_max=2.5)

    # A11-A12: Higher ROE
    cfg("A_roe", roe_min=20)
    cfg("A_roe", roe_min=25)

    # ── Group B: Combined filters (12 configs) ──

    # B1-B3: GP + RevGrowth
    cfg("B_combined", gp_min=0.30, rev_growth_min=0.0)
    cfg("B_combined", gp_min=0.40, rev_growth_min=0.0)
    cfg("B_combined", gp_min=0.40, rev_growth_min=0.05)

    # B4-B6: GP + RevGrowth + tighter PE
    cfg("B_combined", gp_min=0.30, rev_growth_min=0.0, pe_max=20)
    cfg("B_combined", gp_min=0.40, rev_growth_min=0.0, pe_max=20)
    cfg("B_combined", gp_min=0.40, rev_growth_min=0.0, pe_max=15)

    # B7-B8: All filters stacked
    cfg("B_combined", gp_min=0.30, rev_growth_min=0.0, pe_max=20, cr_min=1.0, cr_max=2.5)
    cfg("B_combined", gp_min=0.40, rev_growth_min=0.0, pe_max=20, cr_min=1.0, cr_max=2.5)

    # B9-B12: Best combos + sector limits
    cfg("B_sector", gp_min=0.30, rev_growth_min=0.0, pe_max=20, max_per_sector=2)
    cfg("B_sector", gp_min=0.40, rev_growth_min=0.0, pe_max=20, max_per_sector=2)
    cfg("B_sector", gp_min=0.30, rev_growth_min=0.0, pe_max=20, cr_min=1.0, cr_max=2.5, max_per_sector=2)
    cfg("B_sector", gp_min=0.40, rev_growth_min=0.0, pe_max=20, cr_min=1.0, cr_max=2.5, max_per_sector=2)

    return configs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    if market == "nse":
        exchange = "NSE"
        start_epoch = 1262304000   # 2010-01-01
        end_epoch = 1773878400     # 2026-03-17
        benchmark_sym = "NIFTYBEES"
        capital = 10_000_000
        description = "New alpha filters sweep on NSE: GP, RevGrowth, PE, CR."
    elif market == "us":
        exchange = "US"
        start_epoch = 1104537600   # 2005-01-01
        end_epoch = 1773878400
        benchmark_sym = "SPY"
        capital = 10_000_000
        description = "New alpha filters sweep on US: GP, RevGrowth, PE, CR."
    else:
        print(f"Unknown market: {market}")
        return

    source = os.environ.get("SOURCE", "native" if exchange == "NSE" else "fmp")
    if "--fmp" in sys.argv:
        source = "fmp"
    elif "--bhavcopy" in sys.argv:
        source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME} ({market.upper()}): fetching data (source={source})")
    print("=" * 80)

    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch, source=source)
    if not price_data:
        print("No data. Aborting.")
        return
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)
    fundamentals = fetch_fundamentals(cr, exchange)
    ext_fundamentals = fetch_extended_fundamentals(cr, exchange)
    sector_map = fetch_sector_map(cr, exchange)

    # Pre-compute universes (champion config only)
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("Computing momentum universe...")
    momentum_universe = compute_momentum_universe(
        price_data, MOMENTUM_LOOKBACK, MOMENTUM_PERCENTILE,
        rescreen_days=63, start_epoch=start_epoch)
    combined_universe = intersect_universes(quality_universe, momentum_universe)

    print("Computing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # Compute base entries: quality + momentum + dip + D/E<1.0 (no ROE/PE filter yet)
    print("Computing base dip entries (D/E<1.0 only)...")
    raw_entries = compute_dip_entries(
        price_data, combined_universe, PEAK_LOOKBACK,
        DIP_THRESHOLD_PCT / 100.0, start_epoch=start_epoch)
    base_entries = filter_entries_by_fundamentals(
        raw_entries, fundamentals, roe_threshold=0, de_threshold=DE_THRESHOLD,
        pe_threshold=0, missing_mode="skip")
    print(f"  Base entries (D/E<{DE_THRESHOLD} only): {len(base_entries)}")

    # ── Sweep ──
    all_configs = build_configs()
    total = len(all_configs)

    print(f"\n{'=' * 80}")
    print(f"  SWEEP: {total} configs")
    group_a = sum(1 for c in all_configs if c["group"].startswith("A_"))
    group_b = sum(1 for c in all_configs if c["group"].startswith("B_"))
    print(f"  Group A (individual): {group_a} | Group B (combined): {group_b}")
    print(f"  Fixed: {MOMENTUM_LOOKBACK}d mom top{MOMENTUM_PERCENTILE*100:.0f}% "
          f"D/E<{DE_THRESHOLD} {MAX_POSITIONS}pos TSL={TSL_PCT}% "
          f"regime={REGIME_SMA} hold={MAX_HOLD_DAYS}d")
    print(f"{'=' * 80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    entries_cache = {}

    for idx, params in enumerate(all_configs):
        roe_min = params["roe_min"]
        pe_max = params["pe_max"]
        gp_min = params["gp_min"]
        rev_growth_min = params["rev_growth_min"]
        cr_min = params["cr_min"]
        cr_max = params["cr_max"]
        sec_limit = params["max_per_sector"]
        group = params["group"]

        cache_key = (roe_min, pe_max, gp_min, rev_growth_min, cr_min, cr_max)
        if cache_key not in entries_cache:
            # Apply ROE + PE filter on base entries
            entries = filter_entries_by_fundamentals(
                base_entries, fundamentals, roe_min, 0, pe_max,
                missing_mode="skip")

            # Apply extended filters
            entries = filter_entries_by_extended(
                entries, ext_fundamentals, gp_min, rev_growth_min, cr_min, cr_max)

            entries_cache[cache_key] = entries

        entries_run = entries_cache[cache_key]

        sim_kwargs = dict(
            capital=capital, max_positions=MAX_POSITIONS,
            tsl_pct=TSL_PCT, max_hold_days=MAX_HOLD_DAYS,
            exchange=exchange, regime_epochs=regime_epochs,
            strategy_name=STRATEGY_NAME, description=description,
            params=params, start_epoch=start_epoch,
        )
        if sec_limit > 0 and sector_map:
            sim_kwargs["sector_map"] = sector_map
            sim_kwargs["max_per_sector"] = sec_limit

        r, dwl = simulate_portfolio(entries_run, price_data, benchmark, **sim_kwargs)
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        sharpe = s.get("sharpe_ratio") or 0
        trades = s.get("total_trades") or 0
        win_rate = (s.get("win_rate") or 0) * 100

        # Build label
        labels = [group]
        if gp_min > 0:
            labels.append(f"GP>{gp_min:.0%}")
        if rev_growth_min > -999:
            labels.append(f"RG>{rev_growth_min:.0%}")
        if pe_max != 25:
            labels.append(f"PE<{pe_max}")
        if cr_min > 0:
            labels.append(f"CR>{cr_min}")
        if cr_max > 0:
            labels.append(f"CR<{cr_max}")
        if roe_min != 15:
            labels.append(f"ROE>{roe_min}")
        if sec_limit > 0:
            labels.append(f"sec<={sec_limit}")
        label = " ".join(labels)

        print(f"  [{idx+1}/{total}] {label:<55} "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} "
              f"Sh={sharpe:.2f} WR={win_rate:.0f}% T={trades}")

    # ── Always-invested adjustment (top 10) ──
    print(f"\n{'=' * 80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (top 10)")
    print(f"{'=' * 80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f} "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"T={s.get('total_trades',0)} | {params}")

    sweep.print_leaderboard(top_n=24)
    sweep.save("result.json", top_n=24, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

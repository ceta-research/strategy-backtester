#!/usr/bin/env python3
"""Debug: compare engine (Polars) vs standalone (Python) signal generation.

Loads data via engine pipeline, converts to standalone format, runs both
quality/momentum/dip algorithms on identical data. Prints side-by-side
comparison to isolate where they diverge.

Usage:
    source ../.venv/bin/activate
    python3 scripts/debug_signal_gen.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl
from engine.config_loader import load_config, get_scanner_config_iterator
from engine.data_provider import CRDataProvider
from engine.signals.base import add_next_day_values
from scripts.quality_dip_buy_lib import (
    compute_quality_universe,
    compute_momentum_universe,
    compute_dip_entries,
    _find_epoch_idx,
)
from lib.data_fetchers import intersect_universes

# ── Config (matches config_nse_champion.yaml) ──
START_EPOCH = 1262304000      # 2010-01-01
END_EPOCH   = 1773878400      # 2026-03-17
PREFETCH_DAYS = 1500
CONSECUTIVE_YEARS = 2
MIN_YEARLY_RETURN = 0.0
MOMENTUM_LOOKBACK = 63
MOMENTUM_PERCENTILE = 0.30
PEAK_LOOKBACK = 63
DIP_THRESHOLD = 0.05
RESCREEN_DAYS = 63
TURNOVER_THRESHOLD = 70_000_000
TRADING_DAYS_PER_YEAR = 252

# Sample dates spread across the backtest for comparison
SAMPLE_DATES = [
    1293840000,  # 2011-01-01
    1356998400,  # 2013-01-01
    1420070400,  # 2015-01-01
    1483228800,  # 2017-01-01
    1546300800,  # 2019-01-01
    1609459200,  # 2021-01-01
    1672531200,  # 2023-01-01
]


def load_engine_data():
    """Load data using the engine pipeline's CRDataProvider."""
    print("=" * 80)
    print("  Loading data via CRDataProvider (FMP stock_eod)")
    print("=" * 80)
    dp = CRDataProvider(format="parquet")
    df = dp.fetch_ohlcv(
        exchanges=["NSE"],
        symbols=None,
        start_epoch=START_EPOCH,
        end_epoch=END_EPOCH,
        prefetch_days=PREFETCH_DAYS,
    )
    print(f"  Loaded: {df.height} rows, {df['instrument'].n_unique()} instruments")
    print(f"  Date range: {df['date_epoch'].min()} to {df['date_epoch'].max()}")
    return df


def polars_to_price_data(df):
    """Convert engine's Polars DataFrame to standalone's dict[symbol, list[dict]] format."""
    price_data = {}
    for (inst,), group in df.group_by("instrument"):
        g = group.sort("date_epoch")
        sym = inst.split(":")[1] if ":" in inst else inst
        bars = []
        for row in g.iter_rows(named=True):
            bars.append({
                "epoch": row["date_epoch"],
                "open": row["open"],
                "close": row["close"],
                "volume": row["volume"],
            })
        price_data[sym] = bars
    return price_data


def engine_period_avg_filter(df):
    """Replicate engine's period-average turnover filter."""
    period_avg = (
        df.group_by("instrument")
        .agg(
            (pl.col("close") * pl.col("volume")).mean().alias("avg_turnover"),
            pl.col("close").mean().alias("avg_close"),
        )
        .filter(
            (pl.col("avg_turnover") > TURNOVER_THRESHOLD)
            & (pl.col("avg_close") > 50)
        )
    )
    return set(period_avg["instrument"].to_list())


def standalone_period_avg_filter(price_data):
    """Replicate standalone's SQL-based period-average turnover filter."""
    qualifying = {}
    for sym, bars in price_data.items():
        if not bars:
            continue
        avg_turnover = sum(b["close"] * b["volume"] for b in bars) / len(bars)
        avg_close = sum(b["close"] for b in bars) / len(bars)
        if avg_turnover > TURNOVER_THRESHOLD and avg_close > 50:
            qualifying[sym] = avg_turnover
    return qualifying


def engine_quality_filter(df_signals, period_universe_set, epoch):
    """Run engine's quality filter for a specific epoch."""
    day_data = df_signals.filter(
        (pl.col("date_epoch") == epoch)
        & (pl.col("instrument").is_in(list(period_universe_set)))
        & (pl.col("is_quality") == True)  # noqa: E712
    )
    return set(day_data["instrument"].to_list())


def engine_momentum_filter(df_signals, period_universe_set, epoch, percentile):
    """Run engine's momentum filter for a specific epoch."""
    day_data = (
        df_signals.filter(
            (pl.col("date_epoch") == epoch)
            & (pl.col("instrument").is_in(list(period_universe_set)))
            & (pl.col("momentum_return").is_not_null())
        )
        .sort("momentum_return", descending=True)
    )
    total = day_data.height
    top_n = max(1, int(total * percentile))
    return set(day_data["instrument"].head(top_n).to_list()), total, top_n


def find_nearest_epoch(epochs_list, target):
    """Find the trading epoch nearest to (at or after) the target."""
    for ep in epochs_list:
        if ep >= target:
            return ep
    return epochs_list[-1] if epochs_list else None


def compare_universes():
    """Main comparison function."""
    def _f2(v):
        return f"{v:.2f}" if v is not None else "None"
    def _f4(v):
        return f"{v:.4f}" if v is not None else "None"
    def _match(a, b, tol=0.001):
        if a is None or b is None:
            return "BOTH_NULL" if a is None and b is None else "NO"
        return "YES" if abs(a - b) < tol else "NO"

    # ── Step 1: Load data ──
    df_tick_data = load_engine_data()

    # Prepare engine data (add next-day values, compute indicators)
    df_ind = df_tick_data.clone()
    df_ind = add_next_day_values(df_ind)
    df_ind = df_ind.sort(["instrument", "date_epoch"])

    # Convert to standalone format
    print("\n  Converting to standalone format...")
    price_data = polars_to_price_data(df_tick_data)
    print(f"  Standalone format: {len(price_data)} symbols")

    # ── Step 2: Compare period-average universe ──
    print("\n" + "=" * 80)
    print("  STEP 1: Period-Average Universe Filter")
    print("=" * 80)

    engine_universe = engine_period_avg_filter(df_ind)
    standalone_qualifying = standalone_period_avg_filter(price_data)
    standalone_universe_syms = set(standalone_qualifying.keys())

    # Map engine instruments to bare symbols for comparison
    engine_syms = set(inst.split(":")[1] for inst in engine_universe)

    common = engine_syms & standalone_universe_syms
    engine_only = engine_syms - standalone_universe_syms
    standalone_only = standalone_universe_syms - engine_syms

    print(f"  Engine:     {len(engine_syms)} symbols")
    print(f"  Standalone: {len(standalone_universe_syms)} symbols")
    print(f"  Common:     {len(common)}")
    print(f"  Engine-only: {len(engine_only)} {list(engine_only)[:10]}")
    print(f"  Standalone-only: {len(standalone_only)} {list(standalone_only)[:10]}")

    if engine_only or standalone_only:
        print("\n  *** DIFFERENCE: Period-average filter produces different universes!")
        print("  This is expected (engine uses df_ind with next-day shift, standalone uses raw data)")
        print("  Checking if this matters...")

    # ── Step 3: Compute engine quality indicators ──
    print("\n" + "=" * 80)
    print("  STEP 2: Quality Filter Comparison")
    print("=" * 80)

    df_signals = df_ind.clone()

    # Rolling peak
    df_signals = df_signals.with_columns(
        pl.col("close")
        .rolling_max(window_size=PEAK_LOOKBACK, min_samples=PEAK_LOOKBACK)
        .over("instrument")
        .alias("rolling_peak")
    )

    # Dip percentage
    df_signals = df_signals.with_columns(
        ((pl.col("rolling_peak") - pl.col("close")) / pl.col("rolling_peak")).alias("dip_pct")
    )

    # Quality filter: yearly returns
    yearly_return_cols = []
    for yr in range(CONSECUTIVE_YEARS):
        shift_recent = yr * TRADING_DAYS_PER_YEAR
        shift_older = (yr + 1) * TRADING_DAYS_PER_YEAR
        col_name = f"yr_return_{yr + 1}"
        df_signals = df_signals.with_columns(
            (
                pl.col("close").shift(shift_recent).over("instrument")
                / pl.col("close").shift(shift_older).over("instrument")
                - 1.0
            ).alias(col_name)
        )
        yearly_return_cols.append(col_name)

    quality_expr = pl.lit(True)
    for col_name in yearly_return_cols:
        quality_expr = quality_expr & (pl.col(col_name) > MIN_YEARLY_RETURN)
    df_signals = df_signals.with_columns(quality_expr.alias("is_quality"))

    # Momentum return
    df_signals = df_signals.with_columns(
        (
            pl.col("close")
            / pl.col("close").shift(MOMENTUM_LOOKBACK).over("instrument")
            - 1.0
        ).alias("momentum_return")
    )

    # Trim to sim range
    df_signals = df_signals.filter(pl.col("date_epoch") >= START_EPOCH)

    # Get all trading epochs for finding nearest dates
    all_engine_epochs = sorted(df_signals["date_epoch"].unique().to_list())

    # ── Compute standalone quality universe ──
    # IMPORTANT: filter price_data to same 875 symbols as engine period-universe
    filtered_price_data_quality = {
        sym: bars for sym, bars in price_data.items()
        if sym in engine_syms
    }
    print(f"  Computing standalone quality universe (filtered to {len(filtered_price_data_quality)} symbols)...")
    standalone_quality = compute_quality_universe(
        filtered_price_data_quality, CONSECUTIVE_YEARS, MIN_YEARLY_RETURN,
        rescreen_days=RESCREEN_DAYS, start_epoch=START_EPOCH,
    )

    # ── Compare at sample dates ──
    print("\n  Quality universe comparison at sample dates:")
    print(f"  {'Date':<12} {'Engine':<10} {'Standalone':<12} {'Common':<10} {'Eng-only':<10} {'SA-only':<10}")
    print("  " + "-" * 65)

    for target_date in SAMPLE_DATES:
        # Find nearest actual trading epoch
        engine_epoch = find_nearest_epoch(all_engine_epochs, target_date)
        if engine_epoch is None:
            continue

        # Engine quality universe at this epoch
        eng_quality = engine_quality_filter(df_signals, engine_universe, engine_epoch)
        eng_quality_syms = set(inst.split(":")[1] for inst in eng_quality)

        # Standalone quality universe at this epoch
        sa_quality = standalone_quality.get(engine_epoch, set())
        # If exact epoch not found, find nearest
        if not sa_quality:
            for ep in sorted(standalone_quality.keys()):
                if ep >= engine_epoch:
                    sa_quality = standalone_quality[ep]
                    break

        common_q = eng_quality_syms & sa_quality
        eng_only_q = eng_quality_syms - sa_quality
        sa_only_q = sa_quality - eng_quality_syms

        import datetime
        dt = datetime.datetime.utcfromtimestamp(engine_epoch).strftime("%Y-%m-%d")
        print(f"  {dt:<12} {len(eng_quality_syms):<10} {len(sa_quality):<12} "
              f"{len(common_q):<10} {len(eng_only_q):<10} {len(sa_only_q):<10}")

        # Show some examples of differences
        if eng_only_q:
            examples = list(eng_only_q)[:5]
            # Check why standalone excluded these
            for sym in examples[:2]:
                _debug_quality_single_stock(
                    sym, engine_epoch, df_signals, price_data, "ENGINE-ONLY"
                )

        if sa_only_q:
            examples = list(sa_only_q)[:5]
            for sym in examples[:2]:
                _debug_quality_single_stock(
                    sym, engine_epoch, df_signals, price_data, "STANDALONE-ONLY"
                )

    # ── Step 4: Momentum Filter Comparison ──
    print("\n" + "=" * 80)
    print("  STEP 3: Momentum Filter Comparison")
    print("=" * 80)

    print(f"  Computing standalone momentum universe (filtered to {len(filtered_price_data_quality)} symbols)...")
    standalone_momentum = compute_momentum_universe(
        filtered_price_data_quality, MOMENTUM_LOOKBACK, MOMENTUM_PERCENTILE,
        rescreen_days=RESCREEN_DAYS, start_epoch=START_EPOCH,
    )

    print(f"\n  {'Date':<12} {'E-total':<10} {'E-topN':<8} {'SA-total':<10} {'SA-topN':<8} "
          f"{'E-mom':<8} {'SA-mom':<8} {'Common':<8} {'E-only':<8} {'SA-only':<8}")
    print("  " + "-" * 90)

    for target_date in SAMPLE_DATES:
        engine_epoch = find_nearest_epoch(all_engine_epochs, target_date)
        if engine_epoch is None:
            continue

        # Engine momentum
        eng_mom_set, eng_total, eng_top_n = engine_momentum_filter(
            df_signals, engine_universe, engine_epoch, MOMENTUM_PERCENTILE
        )
        eng_mom_syms = set(inst.split(":")[1] for inst in eng_mom_set)

        # Standalone momentum
        sa_mom = standalone_momentum.get(engine_epoch, set())
        if not sa_mom:
            for ep in sorted(standalone_momentum.keys()):
                if ep >= engine_epoch:
                    sa_mom = standalone_momentum[ep]
                    break

        # Count standalone total for this date (same filtered universe)
        sa_total = 0
        for sym, bars in filtered_price_data_quality.items():
            idx = _find_epoch_idx(bars, engine_epoch)
            if idx is not None and idx >= MOMENTUM_LOOKBACK:
                older_idx = idx - MOMENTUM_LOOKBACK
                if older_idx >= 0 and bars[older_idx]["close"] > 0:
                    sa_total += 1
        sa_top_n = max(1, int(sa_total * MOMENTUM_PERCENTILE))

        common_m = eng_mom_syms & sa_mom
        eng_only_m = eng_mom_syms - sa_mom
        sa_only_m = sa_mom - eng_mom_syms

        dt = datetime.datetime.utcfromtimestamp(engine_epoch).strftime("%Y-%m-%d")
        print(f"  {dt:<12} {eng_total:<10} {eng_top_n:<8} {sa_total:<10} {sa_top_n:<8} "
              f"{len(eng_mom_syms):<8} {len(sa_mom):<8} {len(common_m):<8} "
              f"{len(eng_only_m):<8} {len(sa_only_m):<8}")

    # ── Step 5: Combined Universe + Dip Entries ──
    print("\n" + "=" * 80)
    print("  STEP 4: Combined Universe + Dip Entries Comparison")
    print("=" * 80)

    # Engine combined universe (full computation)
    print("  Computing engine combined universe (full)...")
    engine_quality_universe = {}
    engine_momentum_universe = {}
    rescreen_interval = RESCREEN_DAYS * 86400
    rerank_interval = RESCREEN_DAYS * 86400

    last_screen = None
    last_rank = None
    for epoch in all_engine_epochs:
        # Quality rescreen
        if last_screen is None or (epoch - last_screen) >= rescreen_interval:
            engine_quality_universe[epoch] = engine_quality_filter(
                df_signals, engine_universe, epoch
            )
            last_screen = epoch
        else:
            engine_quality_universe[epoch] = engine_quality_universe[last_screen]

        # Momentum rerank
        if last_rank is None or (epoch - last_rank) >= rerank_interval:
            eng_mom, _, _ = engine_momentum_filter(
                df_signals, engine_universe, epoch, MOMENTUM_PERCENTILE
            )
            engine_momentum_universe[epoch] = eng_mom
            last_rank = epoch
        else:
            engine_momentum_universe[epoch] = engine_momentum_universe[last_rank]

    # Engine combined
    engine_combined = {}
    for epoch in all_engine_epochs:
        q = engine_quality_universe.get(epoch, set())
        m = engine_momentum_universe.get(epoch, set())
        intersection = q & m
        if intersection:
            engine_combined[epoch] = intersection

    # Standalone combined
    standalone_combined = intersect_universes(standalone_quality, standalone_momentum)

    # Compare combined universes at sample dates
    print(f"\n  Combined universe sizes:")
    print(f"  {'Date':<12} {'Engine':<10} {'Standalone':<12} {'Common':<10}")
    print("  " + "-" * 45)

    for target_date in SAMPLE_DATES:
        engine_epoch = find_nearest_epoch(all_engine_epochs, target_date)
        if engine_epoch is None:
            continue

        eng_combined = engine_combined.get(engine_epoch, set())
        eng_combined_syms = set(inst.split(":")[1] for inst in eng_combined)

        sa_combined = standalone_combined.get(engine_epoch, set())
        if not sa_combined:
            for ep in sorted(standalone_combined.keys()):
                if ep >= engine_epoch:
                    sa_combined = standalone_combined[ep]
                    break

        common_c = eng_combined_syms & sa_combined
        dt = datetime.datetime.utcfromtimestamp(engine_epoch).strftime("%Y-%m-%d")
        print(f"  {dt:<12} {len(eng_combined_syms):<10} {len(sa_combined):<12} {len(common_c):<10}")

    # ── Step 6: Dip entries comparison ──
    print("\n  Computing dip entries...")
    # Filter price_data to only period-universe symbols (matching standalone fetch_universe)
    filtered_price_data = {
        sym: bars for sym, bars in price_data.items()
        if sym in standalone_universe_syms
    }

    standalone_entries = compute_dip_entries(
        filtered_price_data, standalone_combined, PEAK_LOOKBACK,
        DIP_THRESHOLD, start_epoch=START_EPOCH,
    )

    # Engine entries (from df_signals)
    entry_filter = (
        (pl.col("dip_pct") >= DIP_THRESHOLD)
        & (pl.col("is_quality") == True)  # noqa: E712
        & (pl.col("instrument").is_in(list(engine_universe)))
        & (pl.col("next_epoch").is_not_null())
        & (pl.col("next_open").is_not_null())
        & (pl.col("rolling_peak").is_not_null())
    )
    engine_entry_rows = (
        df_signals.filter(entry_filter)
        .select(["instrument", "date_epoch", "next_epoch", "next_open", "rolling_peak", "dip_pct"])
        .to_dicts()
    )

    # Filter to combined universe
    engine_entries = []
    for entry in engine_entry_rows:
        inst = entry["instrument"]
        epoch = entry["date_epoch"]
        universe = engine_combined.get(epoch, set())
        if inst in universe:
            engine_entries.append(entry)

    print(f"\n  Engine entries (combined universe): {len(engine_entries)}")
    print(f"  Standalone entries (combined universe): {len(standalone_entries)}")

    # Build sets of (symbol, entry_epoch) for comparison
    engine_entry_set = set()
    for e in engine_entries:
        sym = e["instrument"].split(":")[1]
        engine_entry_set.add((sym, e["next_epoch"]))

    standalone_entry_set = set()
    for e in standalone_entries:
        standalone_entry_set.add((e["symbol"], e["entry_epoch"]))

    common_entries = engine_entry_set & standalone_entry_set
    engine_only_entries = engine_entry_set - standalone_entry_set
    standalone_only_entries = standalone_entry_set - engine_entry_set

    print(f"\n  Common entries: {len(common_entries)}")
    print(f"  Engine-only:    {len(engine_only_entries)}")
    print(f"  Standalone-only: {len(standalone_only_entries)}")

    # ── Step 7: Categorize WHY entries are missing from engine ──
    print("\n  Categorizing standalone-only entries...")
    reasons = {
        "peak_le_entry": 0,         # peak_price <= entry_price (gap-up)
        "not_in_combined": 0,        # not in engine combined universe
        "rolling_peak_null": 0,      # engine rolling_peak is null
        "no_signal_row": 0,          # no row in df_signals (entry_epoch missing)
        "quality_null": 0,           # engine is_quality is null
        "quality_false": 0,          # engine is_quality is False
        "dip_below_threshold": 0,    # engine dip_pct below threshold
        "not_in_universe": 0,        # not in period-average universe
        "other": 0,
    }
    sample_reasons = {k: [] for k in reasons}

    for sym, entry_ep in standalone_only_entries:
        inst = f"NSE:{sym}"
        in_universe = inst in engine_universe

        if not in_universe:
            reasons["not_in_universe"] += 1
            continue

        # Find the signal row (day before entry)
        sig_row = df_signals.filter(
            (pl.col("instrument") == inst) & (pl.col("next_epoch") == entry_ep)
        )
        if sig_row.height == 0:
            reasons["no_signal_row"] += 1
            if len(sample_reasons["no_signal_row"]) < 3:
                sample_reasons["no_signal_row"].append((sym, entry_ep))
            continue

        row = sig_row.to_dicts()[0]
        sig_epoch = row["date_epoch"]
        dip = row.get("dip_pct")
        is_q = row.get("is_quality")
        peak = row.get("rolling_peak")
        next_open = row.get("next_open")
        in_combined = inst in engine_combined.get(sig_epoch, set())

        if peak is None:
            reasons["rolling_peak_null"] += 1
            if len(sample_reasons["rolling_peak_null"]) < 3:
                sample_reasons["rolling_peak_null"].append((sym, entry_ep, row))
        elif next_open is not None and peak <= next_open:
            reasons["peak_le_entry"] += 1
            if len(sample_reasons["peak_le_entry"]) < 3:
                sample_reasons["peak_le_entry"].append((sym, entry_ep, peak, next_open))
        elif is_q is None:
            reasons["quality_null"] += 1
            if len(sample_reasons["quality_null"]) < 3:
                sample_reasons["quality_null"].append((sym, entry_ep, row))
        elif is_q is False:
            reasons["quality_false"] += 1
        elif dip is not None and dip < DIP_THRESHOLD:
            reasons["dip_below_threshold"] += 1
        elif not in_combined:
            reasons["not_in_combined"] += 1
            if len(sample_reasons["not_in_combined"]) < 3:
                sample_reasons["not_in_combined"].append((sym, entry_ep, sig_epoch, row))
        else:
            reasons["other"] += 1
            if len(sample_reasons["other"]) < 3:
                sample_reasons["other"].append((sym, entry_ep, row))

    print(f"\n  Breakdown of {len(standalone_only_entries)} standalone-only entries:")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        if count > 0:
            pct = count / len(standalone_only_entries) * 100
            print(f"    {reason:<25} {count:>6} ({pct:.1f}%)")

    # Print samples for top reasons
    for reason in ["peak_le_entry", "not_in_combined", "rolling_peak_null", "quality_null", "no_signal_row"]:
        samples = sample_reasons.get(reason, [])
        if samples:
            print(f"\n  Samples for '{reason}':")
            for s in samples:
                if reason == "peak_le_entry":
                    sym, ep, peak, nopen = s
                    dt = datetime.datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d")
                    print(f"    {sym} entry={dt}: peak={peak:.2f}, next_open={nopen:.2f}, gap_up={nopen/peak-1:.1%}")
                elif reason == "not_in_combined":
                    sym, ep, sig_ep, row = s
                    dt = datetime.datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d")
                    print(f"    {sym} entry={dt}: quality={row.get('is_quality')}, "
                          f"mom={_f4(row.get('momentum_return'))}")
                elif reason in ("rolling_peak_null", "quality_null"):
                    sym, ep, row = s
                    dt = datetime.datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d")
                    print(f"    {sym} entry={dt}: yr1={_f4(row.get('yr_return_1'))}, "
                          f"yr2={_f4(row.get('yr_return_2'))}, peak={row.get('rolling_peak')}")
                elif reason == "no_signal_row":
                    sym, ep = s
                    dt = datetime.datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d")
                    print(f"    {sym} entry={dt}: entry_epoch not in df_signals next_epoch column")

    if standalone_only_entries:
        print("\n  --- Sample standalone-only entries (first 10) ---")
        sample = sorted(standalone_only_entries, key=lambda x: x[1])[:10]
        for sym, entry_ep in sample:
            # Find why engine missed this
            inst = f"NSE:{sym}"

            # Check if in engine_universe
            in_universe = inst in engine_universe
            # Check if it had a dip signal
            sig_row = df_signals.filter(
                (pl.col("instrument") == inst) & (pl.col("next_epoch") == entry_ep)
            )
            if sig_row.height > 0:
                row = sig_row.to_dicts()[0]
                sig_epoch = row["date_epoch"]
                dip = row.get("dip_pct")
                is_q = row.get("is_quality")
                mom = row.get("momentum_return")
                peak = row.get("rolling_peak")
                has_peak = peak is not None
                in_combined = inst in engine_combined.get(sig_epoch, set())
                next_open = row.get("next_open")
                peak_gt_entry = (peak is not None and next_open is not None and peak > next_open) if peak is not None else None
                dt = datetime.datetime.utcfromtimestamp(entry_ep).strftime("%Y-%m-%d")
                print(f"    {sym} entry={dt}: univ={in_universe}, q={is_q}, "
                      f"dip={_f4(dip)}, peak={_f2(peak)}, next_open={_f2(next_open)}, "
                      f"peak>entry={peak_gt_entry}, in_comb={in_combined}")
            else:
                dt = datetime.datetime.utcfromtimestamp(entry_ep).strftime("%Y-%m-%d")
                print(f"    {sym} entry={dt}: NO signal row found! universe={in_universe}")

    # ── Step 8: Rolling peak comparison for specific stocks ──
    print("\n" + "=" * 80)
    print("  STEP 5: Rolling Peak Comparison (specific stocks)")
    print("=" * 80)

    # Pick 5 high-liquidity stocks to compare rolling peaks
    test_stocks = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]
    test_epoch = find_nearest_epoch(all_engine_epochs, 1483228800)  # ~2017-01-01

    for sym in test_stocks:
        inst = f"NSE:{sym}"
        if inst not in engine_universe:
            print(f"  {sym}: not in engine universe")
            continue
        if sym not in price_data:
            print(f"  {sym}: not in price_data")
            continue

        # Engine values at test_epoch
        eng_row = df_signals.filter(
            (pl.col("instrument") == inst) & (pl.col("date_epoch") == test_epoch)
        )
        if eng_row.height == 0:
            # Find nearest
            eng_row = df_signals.filter(
                (pl.col("instrument") == inst)
                & (pl.col("date_epoch") >= test_epoch)
            ).sort("date_epoch").head(1)
        if eng_row.height == 0:
            print(f"  {sym}: no engine data near test epoch")
            continue

        eng = eng_row.to_dicts()[0]
        eng_epoch = eng["date_epoch"]
        eng_close = eng["close"]
        eng_peak = eng.get("rolling_peak")
        eng_dip = eng.get("dip_pct")
        eng_yr1 = eng.get("yr_return_1")
        eng_yr2 = eng.get("yr_return_2")
        eng_mom = eng.get("momentum_return")
        eng_quality = eng.get("is_quality")

        # Standalone values at same epoch
        bars = price_data[sym]
        idx = _find_epoch_idx(bars, eng_epoch)
        if idx is None or idx < PEAK_LOOKBACK:
            print(f"  {sym}: not enough standalone bars at epoch")
            continue

        sa_close = bars[idx]["close"]
        window_start = max(0, idx - PEAK_LOOKBACK + 1)
        sa_peak = max(b["close"] for b in bars[window_start:idx + 1])
        sa_dip = (sa_peak - sa_close) / sa_peak if sa_peak > 0 else 0

        # Quality check
        sa_quality = True
        for yr in range(CONSECUTIVE_YEARS):
            recent_idx = idx - yr * TRADING_DAYS_PER_YEAR
            older_idx = idx - (yr + 1) * TRADING_DAYS_PER_YEAR
            if recent_idx < 0 or older_idx < 0:
                sa_quality = False
                break
            yr_ret = bars[recent_idx]["close"] / bars[older_idx]["close"] - 1.0
            if yr_ret <= MIN_YEARLY_RETURN:
                sa_quality = False
                break

        # Momentum
        sa_mom = None
        if idx >= MOMENTUM_LOOKBACK:
            older_close = bars[idx - MOMENTUM_LOOKBACK]["close"]
            if older_close > 0:
                sa_mom = sa_close / older_close - 1.0

        # Standalone yearly returns for debugging
        sa_yr1 = None
        sa_yr2 = None
        if idx >= TRADING_DAYS_PER_YEAR:
            sa_yr1 = bars[idx]["close"] / bars[idx - TRADING_DAYS_PER_YEAR]["close"] - 1.0
        if idx >= 2 * TRADING_DAYS_PER_YEAR:
            sa_yr2 = bars[idx - TRADING_DAYS_PER_YEAR]["close"] / bars[idx - 2 * TRADING_DAYS_PER_YEAR]["close"] - 1.0

        dt = datetime.datetime.utcfromtimestamp(eng_epoch).strftime("%Y-%m-%d")
        print(f"\n  {sym} @ {dt} (epoch={eng_epoch}):")
        print(f"    {'Metric':<20} {'Engine':<15} {'Standalone':<15} {'Match':<6}")
        print(f"    {'-'*56}")
        print(f"    {'close':<20} {_f2(eng_close):<15} {_f2(sa_close):<15} {_match(eng_close, sa_close, 0.01)}")
        print(f"    {'rolling_peak':<20} {_f2(eng_peak):<15} {_f2(sa_peak):<15} {_match(eng_peak, sa_peak, 0.01)}")
        print(f"    {'dip_pct':<20} {_f4(eng_dip):<15} {_f4(sa_dip):<15} {_match(eng_dip, sa_dip, 0.0001)}")
        print(f"    {'yr_return_1':<20} {_f4(eng_yr1):<15} {_f4(sa_yr1):<15} {_match(eng_yr1, sa_yr1)}")
        print(f"    {'yr_return_2':<20} {_f4(eng_yr2):<15} {_f4(sa_yr2):<15} {_match(eng_yr2, sa_yr2)}")
        print(f"    {'momentum':<20} {_f4(eng_mom):<15} {_f4(sa_mom):<15} {_match(eng_mom, sa_mom)}")
        print(f"    {'is_quality':<20} {str(eng_quality):<15} {str(sa_quality):<15} "
              f"{'YES' if eng_quality == sa_quality else 'NO'}")

        # If yearly returns differ, dig deeper
        if eng_yr1 and sa_yr1 and abs(eng_yr1 - sa_yr1) > 0.001:
            print(f"\n    ** yr_return_1 DIFFERS! Checking bar alignment...")
            # Engine: shift(0) and shift(252) relative to this row
            eng_rows = (
                df_signals.filter(pl.col("instrument") == inst)
                .sort("date_epoch")
            )
            eng_dates = eng_rows["date_epoch"].to_list()
            eng_idx = eng_dates.index(eng_epoch) if eng_epoch in eng_dates else None
            if eng_idx is not None and eng_idx >= TRADING_DAYS_PER_YEAR:
                eng_close_252 = eng_rows["close"][eng_idx - TRADING_DAYS_PER_YEAR]
                sa_close_252 = bars[idx - TRADING_DAYS_PER_YEAR]["close"]
                eng_date_252 = eng_dates[eng_idx - TRADING_DAYS_PER_YEAR]
                sa_date_252 = bars[idx - TRADING_DAYS_PER_YEAR]["epoch"]
                eng_dt252 = datetime.datetime.utcfromtimestamp(eng_date_252).strftime("%Y-%m-%d")
                sa_dt252 = datetime.datetime.utcfromtimestamp(sa_date_252).strftime("%Y-%m-%d")
                print(f"    Engine: close[now]={eng_close:.2f}, close[now-252]={eng_close_252} "
                      f"(date={eng_dt252})")
                print(f"    Standalone: close[now]={sa_close:.2f}, close[idx-252]={sa_close_252} "
                      f"(date={sa_dt252})")
                print(f"    Engine row count for {sym}: {len(eng_dates)}")
                print(f"    Standalone bar count for {sym}: {len(bars)}")

    print("\n" + "=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print(f"  Period-avg universe: Engine={len(engine_syms)}, Standalone={len(standalone_universe_syms)}")
    print(f"  Total engine entries: {len(engine_entries)}")
    print(f"  Total standalone entries: {len(standalone_entries)}")
    print(f"  Common entries: {len(common_entries)}")
    print(f"  Engine-only: {len(engine_only_entries)}")
    print(f"  Standalone-only: {len(standalone_only_entries)}")
    overlap_pct = len(common_entries) / max(1, len(standalone_entry_set)) * 100
    print(f"  Overlap: {overlap_pct:.1f}%")


def _debug_quality_single_stock(sym, epoch, df_signals, price_data, label):
    """Print debug info for why a stock is in one system but not the other."""
    import datetime

    inst = f"NSE:{sym}"
    dt = datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")

    # Engine
    eng_row = df_signals.filter(
        (pl.col("instrument") == inst) & (pl.col("date_epoch") == epoch)
    )
    eng_quality = None
    eng_yr1 = None
    eng_yr2 = None
    if eng_row.height > 0:
        row = eng_row.to_dicts()[0]
        eng_quality = row.get("is_quality")
        eng_yr1 = row.get("yr_return_1")
        eng_yr2 = row.get("yr_return_2")

    # Standalone
    bars = price_data.get(sym, [])
    sa_quality = None
    sa_yr1 = None
    sa_yr2 = None
    if bars:
        idx = _find_epoch_idx(bars, epoch)
        if idx is not None and idx >= 2 * TRADING_DAYS_PER_YEAR:
            sa_yr1 = bars[idx]["close"] / bars[idx - TRADING_DAYS_PER_YEAR]["close"] - 1.0
            sa_yr2 = bars[idx - TRADING_DAYS_PER_YEAR]["close"] / bars[idx - 2 * TRADING_DAYS_PER_YEAR]["close"] - 1.0
            sa_quality = sa_yr1 > MIN_YEARLY_RETURN and sa_yr2 > MIN_YEARLY_RETURN

    def _fmt(v):
        return f"{v:.4f}" if v is not None else "None"

    print(f"      [{label}] {sym} @ {dt}: eng_q={eng_quality}, sa_q={sa_quality}, "
          f"eng_yr1={_fmt(eng_yr1)}, sa_yr1={_fmt(sa_yr1)}, "
          f"eng_yr2={_fmt(eng_yr2)}, sa_yr2={_fmt(sa_yr2)}")


import datetime


def _f2(v):
    return f"{v:.2f}" if v is not None else "None"

def _f4(v):
    return f"{v:.4f}" if v is not None else "None"


if __name__ == "__main__":
    compare_universes()

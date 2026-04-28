"""Build per-trade feature table — Phase 5a of inspection drill (2026-04-28).

Joins trade_log_audit (at-entry context + audit pnl) with bulk OHLCV
features (momentum, volatility, volume, candle quality, market context)
to produce a single parquet with one row per audit trade + ~40 features
+ outcome variables (pnl_pct, hold_days, is_loser, MFE/MAE).

Output:
    results/<strategy>/audit_drill_*/trade_features.parquet

Usage:
    python3 scripts/build_trade_features.py --strategy eod_breakout
    python3 scripts/build_trade_features.py --strategy eod_technical
    python3 scripts/build_trade_features.py --all
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402

from engine.constants import SECONDS_IN_ONE_DAY  # noqa: E402
from engine.data_provider import NseChartingDataProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRILL_DIRS = {
    "eod_breakout": "results/eod_breakout/audit_drill_20260428T124754Z",
    "eod_technical": "results/eod_technical/audit_drill_20260428T124832Z",
}

NIFTY_SYMBOL = "NIFTYBEES"  # Proxy for Nifty-50 regime / market context


# ---------------------------------------------------------------------------
# Step 1: Load audit
# ---------------------------------------------------------------------------

def load_audit(drill_dir: str) -> pl.DataFrame:
    tla = pl.read_parquet(os.path.join(drill_dir, "trade_log_audit.parquet"))
    # Compute basic outcome variables from audit-level exit.
    tla = tla.with_columns([
        ((pl.col("exit_price") - pl.col("entry_price")) / pl.col("entry_price"))
            .alias("pnl_pct"),
        ((pl.col("exit_epoch") - pl.col("entry_epoch")) / SECONDS_IN_ONE_DAY)
            .cast(pl.Int64).alias("hold_days"),
    ])
    tla = tla.with_columns([
        (pl.col("pnl_pct") < 0).alias("is_loser"),
        (pl.col("pnl_pct") < -0.10).alias("is_big_loser"),
        (pl.col("pnl_pct") > 0.10).alias("is_big_winner"),
    ])
    # signal_epoch is the bar BEFORE entry (MOC convention: signal at close,
    # entry at next-day open). entry_epoch is the next-day open epoch.
    # For eod_breakout, entry_epoch = next_epoch (signal day + 1 trading day).
    # We need to match features on the signal-day bar.
    # signal_epoch is NOT directly in audit, but entry_close_signal is the
    # close on the signal day. We'll join on (instrument, close_epoch) where
    # close_epoch is the bar whose close == entry_close_signal. Simpler
    # approach: use the OHLCV row 1 trading day before entry_epoch.
    # We'll handle this at join time.
    return tla


# ---------------------------------------------------------------------------
# Step 2: Fetch bulk OHLCV + compute rolling features
# ---------------------------------------------------------------------------

def fetch_bulk_ohlcv() -> pl.DataFrame:
    """Fetch full-period NSE charting OHLCV (same as pipeline)."""
    provider = NseChartingDataProvider()
    start_epoch = 1262304000 - 500 * SECONDS_IN_ONE_DAY  # prefetch for rolling
    end_epoch = 1773878400  # 2026-03-30
    df = provider.fetch_ohlcv(
        exchanges=["NSE"],
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        prefetch_days=1,
    )
    return df


def compute_ohlcv_features(df: pl.DataFrame) -> pl.DataFrame:
    """Compute per-(instrument, date_epoch) rolling features."""
    df = df.sort(["instrument", "date_epoch"])

    # Daily return
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("instrument") - 1.0)
            .alias("daily_ret")
    )

    # --- Trend / momentum (backward-looking from each bar) ---
    for w in [1, 5, 20, 60]:
        df = df.with_columns(
            (pl.col("close") / pl.col("close").shift(w).over("instrument") - 1.0)
                .alias(f"ret_{w}d")
        )

    for w in [20, 50, 200]:
        df = df.with_columns(
            pl.col("close").rolling_mean(window_size=w, min_samples=1)
                .over("instrument").alias(f"ma{w}")
        )
        df = df.with_columns(
            ((pl.col("close") - pl.col(f"ma{w}")) / pl.col(f"ma{w}"))
                .alias(f"dist_ma{w}")
        )

    # MA20 slope (5-day change in MA20)
    df = df.with_columns(
        (pl.col("ma20") / pl.col("ma20").shift(5).over("instrument") - 1.0)
            .alias("ma20_slope_5d")
    )

    # --- Volatility ---
    for w in [5, 20, 60]:
        df = df.with_columns(
            pl.col("daily_ret").rolling_std(window_size=w, min_samples=max(3, w // 2))
                .over("instrument").alias(f"vol_{w}d")
        )

    # ATR(14) normalized
    df = df.with_columns([
        (pl.col("high") - pl.col("low")).alias("_hl"),
        (pl.col("high") - pl.col("close").shift(1).over("instrument")).abs().alias("_hc"),
        (pl.col("low") - pl.col("close").shift(1).over("instrument")).abs().alias("_lc"),
    ])
    df = df.with_columns(
        pl.max_horizontal("_hl", "_hc", "_lc").alias("_tr")
    )
    df = df.with_columns(
        (pl.col("_tr").rolling_mean(window_size=14, min_samples=5).over("instrument")
         / pl.col("close")).alias("atr14_pct")
    )
    df = df.drop(["_hl", "_hc", "_lc", "_tr"])

    # --- Volume ---
    df = df.with_columns([
        pl.col("volume").rolling_mean(window_size=20, min_samples=5)
            .over("instrument").alias("avg_vol_20d"),
        pl.col("volume").rolling_mean(window_size=60, min_samples=10)
            .over("instrument").alias("avg_vol_60d"),
        pl.col("volume").rolling_std(window_size=60, min_samples=10)
            .over("instrument").alias("std_vol_60d"),
    ])
    df = df.with_columns([
        (pl.col("volume") / pl.col("avg_vol_20d")).alias("vol_spike_20d"),
        pl.when(pl.col("std_vol_60d") > 0)
            .then((pl.col("volume") - pl.col("avg_vol_60d")) / pl.col("std_vol_60d"))
            .otherwise(0.0)
            .alias("volume_zscore_60d"),
    ])

    # --- Candle quality (signal-day) ---
    df = df.with_columns([
        pl.when((pl.col("high") - pl.col("low")) > 0)
            .then((pl.col("close") - pl.col("open")) / (pl.col("high") - pl.col("low")))
            .otherwise(0.0)
            .alias("body_ratio"),
        pl.when((pl.col("high") - pl.col("low")) > 0)
            .then((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .otherwise(0.5)
            .alias("close_in_range"),
        pl.when((pl.col("high") - pl.col("low")) > 0)
            .then((pl.col("high") - pl.max_horizontal("close", "open"))
                  / (pl.col("high") - pl.col("low")))
            .otherwise(0.0)
            .alias("upper_wick_ratio"),
    ])

    # --- Consecutive up days ---
    df = df.with_columns(
        (pl.col("close") > pl.col("close").shift(1).over("instrument"))
            .cast(pl.Int8).alias("_up")
    )
    # Simple rolling sum of up-days in last 5 bars (proxy for streak)
    df = df.with_columns(
        pl.col("_up").rolling_sum(window_size=5, min_samples=1)
            .over("instrument").alias("up_days_5")
    )
    df = df.drop("_up")

    return df


def compute_nifty_features(df_ohlcv: pl.DataFrame) -> pl.DataFrame:
    """Extract Nifty-level features for market context."""
    nifty = df_ohlcv.filter(pl.col("symbol") == NIFTY_SYMBOL).sort("date_epoch")
    if nifty.is_empty():
        return pl.DataFrame()

    nifty = nifty.with_columns([
        (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("nifty_ret_1d"),
        (pl.col("close") / pl.col("close").shift(5) - 1.0).alias("nifty_ret_5d"),
        (pl.col("close") / pl.col("close").shift(20) - 1.0).alias("nifty_ret_20d"),
        pl.col("close").rolling_mean(window_size=100, min_samples=50).alias("nifty_sma100"),
    ])
    nifty = nifty.with_columns([
        (pl.col("close") > pl.col("nifty_sma100")).alias("nifty_above_sma100"),
        ((pl.col("close") - pl.col("nifty_sma100")) / pl.col("nifty_sma100"))
            .alias("nifty_dist_sma100"),
    ])
    return nifty.select([
        "date_epoch", "nifty_ret_1d", "nifty_ret_5d", "nifty_ret_20d",
        "nifty_above_sma100", "nifty_dist_sma100",
    ])


# ---------------------------------------------------------------------------
# Step 3: Join audit to features
# ---------------------------------------------------------------------------

def find_signal_epoch(
    audit: pl.DataFrame, ohlcv: pl.DataFrame
) -> pl.DataFrame:
    """For each audit row, find the signal-day epoch (1 trading day before
    entry_epoch for that instrument). The signal day is the bar whose
    close = entry_close_signal; or equivalently the last trading day
    before entry_epoch.

    Strategy: for each (instrument, entry_epoch) in audit, look up the
    OHLCV row where instrument matches and date_epoch is the max epoch
    < entry_epoch.
    """
    # Get all (instrument, date_epoch) from ohlcv.
    ohlcv_keys = ohlcv.select(["instrument", "date_epoch"]).unique()

    # Cross-join audit entries with ohlcv dates for same instrument, then
    # filter date < entry_epoch and take max. This sounds expensive but
    # we can do it via a sort + group_by approach.
    # The asof join with strategy="backward" finds signal_epoch ≤ left_on.
    # Since entry_epoch IS a trading day in the OHLCV (entry at open of
    # that bar), a naive join picks that SAME bar — but its close is AFTER
    # entry (leakage). We need the bar BEFORE entry: subtract 1 second
    # from entry_epoch so the asof picks the prior trading day (the signal
    # bar whose close triggered the entry decision).
    audit_shifted = audit.with_columns(
        (pl.col("entry_epoch") - 1).alias("_entry_minus_1")
    )
    audit_with_signal = audit_shifted.sort("_entry_minus_1").join_asof(
        ohlcv_keys.sort("date_epoch").rename({"date_epoch": "signal_epoch"}),
        left_on="_entry_minus_1",
        right_on="signal_epoch",
        by="instrument",
        strategy="backward",
    ).drop("_entry_minus_1")
    return audit_with_signal


def join_features(
    audit: pl.DataFrame,
    ohlcv_feat: pl.DataFrame,
    nifty_feat: pl.DataFrame,
) -> pl.DataFrame:
    """Join signal-day features + nifty features to audit."""
    # First find signal epoch per audit row.
    audit = find_signal_epoch(audit, ohlcv_feat)

    # Join OHLCV features on (instrument, signal_epoch = date_epoch).
    feature_cols = [c for c in ohlcv_feat.columns if c not in
                    ("open", "high", "low", "close", "volume",
                     "average_price", "symbol", "exchange", "daily_ret",
                     "avg_vol_20d", "avg_vol_60d", "std_vol_60d",
                     "ma20", "ma50", "ma200")]
    feat_subset = ohlcv_feat.select(
        ["instrument", "date_epoch", "open", "high", "low", "close", "volume"]
        + [c for c in feature_cols if c not in ("instrument", "date_epoch")]
    ).rename({"date_epoch": "signal_epoch"})

    # Prefix OHLCV signal-day values for clarity
    rename_map = {
        "open": "signal_open", "high": "signal_high",
        "low": "signal_low", "close": "signal_close",
        "volume": "signal_volume",
    }
    feat_subset = feat_subset.rename(rename_map)

    audit = audit.join(feat_subset, on=["instrument", "signal_epoch"], how="left")

    # Join nifty features on signal_epoch = date_epoch.
    if not nifty_feat.is_empty():
        nifty_feat = nifty_feat.rename({"date_epoch": "signal_epoch"})
        audit = audit.join(nifty_feat, on="signal_epoch", how="left")

    # Derived at-entry features.
    audit = audit.with_columns([
        # Breakout extension: how far above n_day_high was the signal close?
        pl.when(pl.col("entry_n_day_high") > 0)
            .then((pl.col("entry_close_signal") - pl.col("entry_n_day_high"))
                  / pl.col("entry_n_day_high"))
            .otherwise(0.0)
            .alias("breakout_extension"),
        # Entry gap: next-day open vs signal close (slippage indicator)
        pl.when(pl.col("entry_close_signal") > 0)
            .then((pl.col("entry_price") - pl.col("entry_close_signal"))
                  / pl.col("entry_close_signal"))
            .otherwise(0.0)
            .alias("entry_gap"),
        # Calendar features
        pl.from_epoch(pl.col("entry_epoch"), time_unit="s").dt.weekday().alias("dow"),
        pl.from_epoch(pl.col("entry_epoch"), time_unit="s").dt.month().alias("month"),
        pl.from_epoch(pl.col("entry_epoch"), time_unit="s").dt.year().alias("year"),
    ])

    return audit


# ---------------------------------------------------------------------------
# Step 4: MFE / MAE
# ---------------------------------------------------------------------------

def compute_mfe_mae(
    audit: pl.DataFrame, ohlcv: pl.DataFrame
) -> pl.DataFrame:
    """For each trade, compute max favorable excursion and max adverse
    excursion using the OHLCV bars between entry_epoch and exit_epoch.

    Uses high/low for intraday extremes. MFE = max(high)/entry - 1.
    MAE = min(low)/entry - 1.
    """
    # Prepare a lean frame: (instrument, date_epoch, high, low)
    bars = ohlcv.select(["instrument", "date_epoch", "high", "low"]).sort(
        ["instrument", "date_epoch"]
    )

    # For each audit trade, we need bars where
    #   instrument matches AND entry_epoch <= date_epoch <= exit_epoch.
    # With 207k trades × avg ~30 bars per hold, that's ~6M lookups.
    # Most efficient: group bars by instrument, then for each trade
    # slice. Polars doesn't have great per-row range-join, so we use
    # a semi-join + group approach.

    # Approach: explode trade into (instrument, entry_epoch, exit_epoch),
    # then join_asof won't work (need range). Use a filter-join trick:
    # add a row_id to audit, stack all bars, filter by range, aggregate.

    audit = audit.with_row_index("_trade_idx")

    # Build trade key frame for range matching.
    trade_keys = audit.select([
        "_trade_idx", "instrument", "entry_epoch", "exit_epoch", "entry_price"
    ])

    # For performance: do this per-instrument group.
    results = []
    instruments = trade_keys["instrument"].unique().to_list()
    n_inst = len(instruments)

    for i, inst in enumerate(instruments):
        if (i + 1) % 500 == 0:
            print(f"    MFE/MAE: {i+1}/{n_inst} instruments...")
        tk = trade_keys.filter(pl.col("instrument") == inst)
        if tk.is_empty():
            continue
        bk = bars.filter(pl.col("instrument") == inst)
        if bk.is_empty():
            continue

        # For each trade in this instrument, filter bars in range.
        for row in tk.iter_rows(named=True):
            idx = row["_trade_idx"]
            ep = row["entry_price"]
            entry_e = row["entry_epoch"]
            exit_e = row["exit_epoch"]
            sub = bk.filter(
                (pl.col("date_epoch") >= entry_e) & (pl.col("date_epoch") <= exit_e)
            )
            if sub.is_empty() or ep <= 0:
                results.append((idx, None, None))
                continue
            max_high = sub["high"].max()
            min_low = sub["low"].min()
            mfe = (max_high / ep) - 1.0
            mae = (min_low / ep) - 1.0
            results.append((idx, mfe, mae))

    mfe_df = pl.DataFrame(
        {"_trade_idx": [r[0] for r in results],
         "mfe_pct": [r[1] for r in results],
         "mae_pct": [r[2] for r in results]},
        schema={"_trade_idx": pl.UInt32, "mfe_pct": pl.Float64, "mae_pct": pl.Float64},
    )
    audit = audit.join(mfe_df, on="_trade_idx", how="left")

    # Derived exit-quality metrics
    audit = audit.with_columns([
        (pl.col("mfe_pct") - pl.col("pnl_pct")).alias("give_back"),
        pl.when(pl.col("mfe_pct") > 0)
            .then(pl.col("pnl_pct") / pl.col("mfe_pct"))
            .otherwise(0.0)
            .alias("tsl_efficiency"),
    ])

    audit = audit.drop("_trade_idx")
    return audit


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_for_strategy(strategy: str) -> str:
    drill_dir = os.path.join(REPO_ROOT, DRILL_DIRS[strategy])
    print(f"\n{'='*60}")
    print(f"  BUILDING FEATURES: {strategy}")
    print(f"{'='*60}")
    t0 = time.time()

    # Load audit
    print("  Loading audit...")
    audit = load_audit(drill_dir)
    print(f"    {audit.height:,} audit trades loaded")

    # Fetch bulk OHLCV
    print("  Fetching bulk OHLCV...")
    ohlcv = fetch_bulk_ohlcv()
    print(f"    {ohlcv.height:,} OHLCV rows fetched")

    # Compute features on OHLCV
    print("  Computing OHLCV features...")
    ohlcv_feat = compute_ohlcv_features(ohlcv)
    print(f"    Features computed on {ohlcv_feat.height:,} rows")

    # Compute Nifty features
    print("  Computing Nifty features...")
    nifty_feat = compute_nifty_features(ohlcv_feat)
    print(f"    Nifty features: {nifty_feat.height:,} rows")

    # Join
    print("  Joining features to audit...")
    audit = join_features(audit, ohlcv_feat, nifty_feat)
    print(f"    Joined: {audit.height:,} rows, {len(audit.columns)} columns")

    # MFE / MAE
    print("  Computing MFE/MAE (per-trade walk)...")
    audit = compute_mfe_mae(audit, ohlcv)
    print(f"    MFE/MAE done")

    # Write
    out_path = os.path.join(drill_dir, "trade_features.parquet")
    audit.write_parquet(out_path, compression="zstd")
    elapsed = time.time() - t0
    print(f"\n  DONE: {out_path}")
    print(f"    Rows: {audit.height:,}, Columns: {len(audit.columns)}")
    print(f"    Elapsed: {elapsed:.1f}s")

    # Spot check
    print("\n  Spot check (first 3 rows, key columns):")
    key_cols = [c for c in [
        "instrument", "entry_epoch", "pnl_pct", "hold_days",
        "breakout_extension", "entry_gap", "ret_5d", "vol_20d",
        "volume_zscore_60d", "body_ratio", "nifty_ret_5d",
        "mfe_pct", "mae_pct", "give_back",
    ] if c in audit.columns]
    print(audit.select(key_cols).head(3))

    return out_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", choices=list(DRILL_DIRS.keys()))
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    strategies = list(DRILL_DIRS.keys()) if args.all else [args.strategy]
    if not strategies or strategies == [None]:
        p.error("Provide --strategy or --all")

    for s in strategies:
        build_for_strategy(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())

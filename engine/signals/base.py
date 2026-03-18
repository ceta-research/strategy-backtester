"""Base protocol and shared utilities for signal generators.

Each strategy implements SignalGenerator to produce orders from OHLCV data.
The pipeline dispatches to the correct generator based on strategy_type in config.
"""

from typing import Protocol

import polars as pl

from engine.config_loader import get_scanner_config_iterator


# ---------------------------------------------------------------------------
# SignalGenerator protocol
# ---------------------------------------------------------------------------

class SignalGenerator(Protocol):
    """Interface that every EOD strategy must implement."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        """Produce orders from raw OHLCV data.

        Must return DataFrame with columns:
            instrument, entry_epoch, exit_epoch,
            entry_price, exit_price, entry_volume, exit_volume,
            scanner_config_ids, entry_config_ids, exit_config_ids
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict[str, type] = {}


def register_strategy(name: str, cls: type):
    _STRATEGY_REGISTRY[name] = cls


def get_signal_generator(strategy_type: str) -> SignalGenerator:
    """Look up and instantiate the signal generator for a strategy type."""
    if strategy_type not in _STRATEGY_REGISTRY:
        available = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
        raise ValueError(f"Unknown strategy_type '{strategy_type}'. Available: {available}")
    return _STRATEGY_REGISTRY[strategy_type]()


# ---------------------------------------------------------------------------
# Shared utilities (extracted from scanner.py / order_generator.py)
# ---------------------------------------------------------------------------

def fill_missing_dates(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Fill missing trading dates per instrument so rolling windows work correctly."""
    min_epoch = df_tick_data["date_epoch"].min()
    max_epoch = df_tick_data["date_epoch"].max()

    all_epochs = list(range(min_epoch, max_epoch + 86400, 86400))
    epoch_df = pl.DataFrame({"date_epoch": all_epochs}).cast({"date_epoch": pl.Int64})

    instruments = df_tick_data.select("instrument").unique()
    full_grid = instruments.join(epoch_df, how="cross")

    existing = df_tick_data.select("instrument", "date_epoch")
    missing = full_grid.join(existing, on=["instrument", "date_epoch"], how="anti")

    if missing.height > 0:
        missing = missing.with_columns([
            pl.col("instrument").str.split(":").list.get(0).alias("exchange"),
            pl.col("instrument").str.split(":").list.get(1).alias("symbol"),
        ])
        df_tick_data = pl.concat([df_tick_data, missing], how="diagonal")
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])

    return df_tick_data


def backfill_close(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Backward-fill close prices within each instrument group."""
    return df_tick_data.with_columns(
        pl.col("close").backward_fill().over("instrument").alias("close")
    )


def add_next_day_values(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Add next-day epoch, open, and volume columns. Drop last row per instrument."""
    df_tick_data = df_tick_data.sort(["instrument", "date_epoch"]).with_columns([
        pl.col("date_epoch").shift(-1).over("instrument").alias("next_epoch"),
        pl.col("open").shift(-1).over("instrument").alias("next_open"),
        pl.col("volume").shift(-1).over("instrument").alias("next_volume"),
    ])
    return df_tick_data.filter(pl.col("next_epoch").is_not_null())


def apply_liquidity_filter(df_tick_data: pl.DataFrame, context: dict) -> pl.DataFrame:
    """Apply scanner-phase liquidity/price filters and tag scanner_config_ids.

    Runs the standard scanner logic: exchange/symbol filter, rolling avg turnover,
    n-day gain, price threshold. Returns DataFrame with scanner_config_ids column.
    """
    df_tick_data = fill_missing_dates(df_tick_data)
    df_tick_data = backfill_close(df_tick_data)
    shortlist_tracker = {}

    for scanner_config in get_scanner_config_iterator(context):
        df = df_tick_data.clone()

        # Exchange/symbol filter
        filter_exprs = []
        for instrument in scanner_config["instruments"]:
            if instrument["symbols"]:
                filter_exprs.append(
                    (pl.col("exchange") == instrument["exchange"])
                    & (pl.col("symbol").is_in(instrument["symbols"]))
                )
            else:
                filter_exprs.append(pl.col("exchange") == instrument["exchange"])

        if filter_exprs:
            combined = filter_exprs[0]
            for expr in filter_exprs[1:]:
                combined = combined | expr
            df = df.filter(combined)

        # Rolling avg turnover
        atc = scanner_config["avg_day_transaction_threshold"]
        df = df.with_columns(
            (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
        )
        df = df.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("avg_txn_turnover")
            .rolling_mean(window_size=atc["period"], min_samples=1)
            .over("instrument")
            .alias("avg_txn_turnover")
        )

        # N-day gain
        ngc = scanner_config["n_day_gain_threshold"]
        df = df.with_columns(
            pl.col("close").shift(ngc["n"] - 1).over("instrument").alias("shifted_close")
        )
        df = df.with_columns(
            ((pl.col("close") - pl.col("shifted_close")) * 100.0 / pl.col("shifted_close")).alias("gain")
        )

        df = df.drop_nulls()
        df = df.filter(pl.col("close") > scanner_config["price_threshold"])
        df = df.filter(pl.col("avg_txn_turnover") > atc["threshold"])
        df = df.filter(pl.col("gain") > ngc["threshold"])

        uid_series = df.select(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )["uid"]
        shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

    # Trim prefetch, add UIDs, tag scanner signals
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
    df_tick_data = df_tick_data.filter(pl.col("date_epoch") >= start_epoch)
    df_tick_data = df_tick_data.drop_nulls()
    df_tick_data = df_tick_data.with_columns(
        (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
    )

    # Build scanner_config_ids column
    signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
    uids = df_tick_data["uid"].to_list()
    uid_to_signals = {}
    for uid in uids:
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None

    signal_series = pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
    df_tick_data = df_tick_data.with_columns(signal_series)

    return df_tick_data

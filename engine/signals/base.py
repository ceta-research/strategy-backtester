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


def sanitize_orders(df_orders: pl.DataFrame, min_entry_price: float = 0.10,
                     max_return_mult: float = 5.0) -> pl.DataFrame:
    """Remove bad orders caused by data quality issues (splits, zero prices, etc).

    Applied in pipeline after signal generation, before simulation.

    Filters:
        1. entry_price <= min_entry_price (sub-penny, zero, or near-zero)
        2. exit_price <= 0
        3. Per-trade return > max_return_mult (caps exit_price; catches reverse splits,
           price unit mismatches, and other data errors)

    Args:
        df_orders: DataFrame with entry_price, exit_price columns.
        min_entry_price: Minimum valid entry price (default $0.10).
        max_return_mult: Maximum allowed exit/entry ratio (default 5.0 = 500%).

    Returns:
        Sanitized DataFrame with bad rows removed and extreme exits capped.
    """
    if df_orders.is_empty():
        return df_orders

    before = df_orders.height

    # Filter out zero / near-zero entry prices
    df_orders = df_orders.filter(pl.col("entry_price") > min_entry_price)

    # Filter out zero / negative exit prices
    df_orders = df_orders.filter(pl.col("exit_price") > 0)

    # Cap extreme returns (reverse splits, data errors)
    max_exit = pl.col("entry_price") * max_return_mult
    df_orders = df_orders.with_columns(
        pl.when(pl.col("exit_price") > max_exit)
        .then(max_exit)
        .otherwise(pl.col("exit_price"))
        .alias("exit_price")
    )

    removed = before - df_orders.height
    if removed > 0:
        print(f"  Sanitized: removed {removed} bad orders ({removed/before*100:.1f}%), "
              f"capped returns at {max_return_mult:.0f}x")

    return df_orders


def run_scanner(context: dict, df_tick_data: pl.DataFrame) -> tuple[dict[str, set], "pl.DataFrame"]:
    """Run the standard scanner phase: liquidity + price filter, return tagged df.

    This is the shared scanner used by all dip-buy / momentum / breakout generators.
    Skips fill_missing_dates / backfill_close / n_day_gain (use apply_liquidity_filter
    for strategies that need those).

    Returns:
        (shortlist_tracker, df_trimmed)
        - shortlist_tracker: {scanner_config_id: set(uid_strings)}
        - df_trimmed: df filtered to start_epoch with scanner_config_ids column
    """
    import time as _time

    t0 = _time.time()
    df = df_tick_data.clone()
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

    shortlist_tracker = {}
    for scanner_config in get_scanner_config_iterator(context):
        df_scan = df.clone()

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
            df_scan = df_scan.filter(combined)

        atc = scanner_config["avg_day_transaction_threshold"]
        df_scan = df_scan.with_columns(
            (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
        )
        df_scan = df_scan.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("avg_txn_turnover")
            .rolling_mean(window_size=atc["period"], min_samples=1)
            .over("instrument")
            .alias("avg_txn_turnover")
        )
        df_scan = df_scan.drop_nulls()
        df_scan = df_scan.filter(pl.col("close") > scanner_config["price_threshold"])
        df_scan = df_scan.filter(pl.col("avg_txn_turnover") > atc["threshold"])

        uid_series = df_scan.select(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )["uid"]
        shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

    df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
    df_trimmed = df_trimmed.with_columns(
        (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
    )
    signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
    uids = df_trimmed["uid"].to_list()
    uid_to_signals = {}
    for uid in uids:
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
    df_trimmed = df_trimmed.with_columns(
        pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
    )

    elapsed = round(_time.time() - t0, 2)
    print(f"  Scanner: {elapsed}s, {df_trimmed.height} rows")

    return shortlist_tracker, df_trimmed


def walk_forward_exit(
    epochs: list, closes: list, start_idx: int,
    entry_epoch: int, entry_price: float, peak_price: float,
    tsl_pct: float, max_hold_days: int,
) -> tuple:
    """Walk forward from entry to find exit via peak recovery + TSL.

    Args:
        epochs: list of date_epoch values for the instrument
        closes: list of close prices for the instrument
        start_idx: index in epochs/closes where entry_epoch lives
        entry_epoch: entry date epoch
        entry_price: entry price
        peak_price: rolling peak price at entry (target for recovery)
        tsl_pct: trailing stop-loss percentage (0 = no TSL, just peak recovery)
        max_hold_days: max holding period in calendar days (0 = no limit)

    Returns:
        (exit_epoch, exit_price) or (None, None) if no exit found and no data remains
    """
    exit_epoch = None
    exit_price = None
    trail_high = entry_price

    if tsl_pct == 0:
        for j in range(start_idx, len(epochs)):
            c = closes[j]
            if c is None:
                continue
            hold_days = (epochs[j] - entry_epoch) / 86400
            if max_hold_days > 0 and hold_days >= max_hold_days:
                return epochs[j], c
            if c >= peak_price:
                return epochs[j], c
    else:
        reached_peak = False
        for j in range(start_idx, len(epochs)):
            c = closes[j]
            if c is None:
                continue
            if c > trail_high:
                trail_high = c
            hold_days = (epochs[j] - entry_epoch) / 86400
            if max_hold_days > 0 and hold_days >= max_hold_days:
                return epochs[j], c
            if c >= peak_price:
                reached_peak = True
            if reached_peak and c <= trail_high * (1 - tsl_pct):
                return epochs[j], c

    # No exit trigger found - exit at last available bar
    if len(epochs) > start_idx:
        return epochs[-1], closes[-1]

    return None, None


EMPTY_ORDERS_SCHEMA = {
    "instrument": pl.Utf8,
    "entry_epoch": pl.Float64,
    "exit_epoch": pl.Float64,
    "entry_price": pl.Float64,
    "exit_price": pl.Float64,
    "entry_volume": pl.Float64,
    "exit_volume": pl.Float64,
    "scanner_config_ids": pl.Utf8,
    "entry_config_ids": pl.Utf8,
    "exit_config_ids": pl.Utf8,
}

ORDER_COLUMNS = [
    "instrument", "entry_epoch", "exit_epoch",
    "entry_price", "exit_price", "entry_volume", "exit_volume",
    "scanner_config_ids", "entry_config_ids", "exit_config_ids",
]


def empty_orders() -> pl.DataFrame:
    """Return an empty DataFrame with the standard order schema."""
    return pl.DataFrame(schema=EMPTY_ORDERS_SCHEMA)


def finalize_orders(all_order_rows: list[dict], elapsed: float) -> pl.DataFrame:
    """Convert order rows to sorted DataFrame, or return empty if none."""
    if not all_order_rows:
        print(f"  Signal gen: {elapsed}s, 0 orders")
        return empty_orders()

    df_orders = pl.DataFrame(all_order_rows)
    df_orders = df_orders.select(ORDER_COLUMNS).sort(["instrument", "entry_epoch", "exit_epoch"])
    print(f"  Signal gen: {elapsed}s, {df_orders.height} orders")
    return df_orders


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

"""Scanner step: filter instruments by price, avg turnover, and n-day gain.

Ported from ATO_Simulator/simulator/steps/scanner_step/process_step.py.

Instrument format: ``{exchange}:{symbol}`` (e.g. ``NSE:RELIANCE``).
Scanner config ``instruments`` field is a list of dicts::

    [{"exchange": "NSE", "symbols": ["RELIANCE", "TCS"]}]

When ``symbols`` is an empty list ``[]``, all symbols for that exchange
present in the tick-data DataFrame are included.
"""

import polars as pl

from engine.config_loader import get_scanner_config_iterator


def fill_missing_dates(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Fill missing trading dates per instrument so rolling windows work correctly."""
    min_epoch = df_tick_data["date_epoch"].min()
    max_epoch = df_tick_data["date_epoch"].max()

    # Generate all daily epochs
    all_epochs = list(range(min_epoch, max_epoch + 86400, 86400))
    epoch_df = pl.DataFrame({"date_epoch": all_epochs}).cast({"date_epoch": pl.Int64})

    instruments = df_tick_data.select("instrument").unique()

    # Cross join: all instruments x all dates
    full_grid = instruments.join(epoch_df, how="cross")

    # Anti-join to find missing combos
    existing = df_tick_data.select("instrument", "date_epoch")
    missing = full_grid.join(existing, on=["instrument", "date_epoch"], how="anti")

    if missing.height > 0:
        # Add exchange/symbol from instrument
        missing = missing.with_columns([
            pl.col("instrument").str.split(":").list.get(0).alias("exchange"),
            pl.col("instrument").str.split(":").list.get(1).alias("symbol"),
        ])
        df_tick_data = pl.concat([df_tick_data, missing], how="diagonal")
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])

    return df_tick_data


def create_scanner_signals(df: pl.DataFrame, signal_dict: dict) -> pl.DataFrame:
    """Add scanner_config_ids column based on which scanner configs shortlisted each row."""
    signal_sets = {k: set(v) for k, v in signal_dict.items()}

    # Build uid -> config_ids mapping
    uid_to_signals = {}
    uids = df["uid"].to_list()
    for uid in uids:
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None

    signal_series = pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
    df = df.with_columns(signal_series)
    return df


def process(context: dict, df_tick_data_original: pl.DataFrame) -> pl.DataFrame:
    """Run scanner step: filter by price/turnover/gain, produce scanner_config_ids column.

    Args:
        context: dict with scanner_config_input, start_epoch, etc.
        df_tick_data_original: pl.DataFrame with columns:
            date_epoch, open, high, low, close, average_price, volume, symbol, instrument, exchange

    Returns:
        pl.DataFrame with scanner_config_ids column added.
    """
    df_tick_data_original = fill_missing_dates(df_tick_data_original)
    df_tick_data_original = df_tick_data_original.with_columns(
        pl.col("close").backward_fill().over("instrument").alias("close")
    )
    shortlist_tracker = {}

    for scanner_config in get_scanner_config_iterator(context):
        df_tick_data = df_tick_data_original.clone()

        # Filter by exchange/symbol from scanner instruments config
        filter_exprs = []
        for instrument in scanner_config["instruments"]:
            if instrument["symbols"]:
                filter_exprs.append(
                    (pl.col("exchange") == instrument["exchange"]) & (pl.col("symbol").is_in(instrument["symbols"]))
                )
            else:
                filter_exprs.append(pl.col("exchange") == instrument["exchange"])

        if filter_exprs:
            combined = filter_exprs[0]
            for expr in filter_exprs[1:]:
                combined = combined | expr
            df_tick_data = df_tick_data.filter(combined)

        avg_day_transaction_threshold_config = scanner_config["avg_day_transaction_threshold"]
        avg_day_transaction_period = avg_day_transaction_threshold_config["period"]
        avg_day_transaction_threshold = avg_day_transaction_threshold_config["threshold"]

        # avg_txn_turnover: SAME-DAY volume × average_price, rolled.
        # Scanner applies this as a UNIVERSE FILTER at bar close — a known
        # bar includes its own volume, so same-day is correct here.
        # Contrast with engine/ranking.py::sort_orders_by_highest_avg_txn
        # which uses PREV-DAY (shifted by 1) because it ranks order entries
        # and must be look-ahead-safe. Both conventions match ATO_Simulator
        # (util.py:186 uses same-day for stats; util.py:251 uses prev-day
        # for ranking). Audit P3.1 / P3.6 — 2026-04-21.
        df_tick_data = df_tick_data.with_columns(
            (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
        )
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("avg_txn_turnover")
            .rolling_mean(window_size=avg_day_transaction_period, min_samples=1)
            .over("instrument")
            .alias("avg_txn_turnover")
        )

        n_day_gain_threshold_config = scanner_config["n_day_gain_threshold"]
        n_day_gain_period = n_day_gain_threshold_config["n"]
        n_day_gain_threshold = n_day_gain_threshold_config["threshold"]

        df_tick_data = df_tick_data.with_columns(
            pl.col("close").shift(n_day_gain_period - 1).over("instrument").alias("shifted_close")
        )
        df_tick_data = df_tick_data.with_columns(
            ((pl.col("close") - pl.col("shifted_close")) * 100.0 / pl.col("shifted_close")).alias("gain")
        )

        df_tick_data = df_tick_data.drop_nulls()
        # price_threshold is a PER-BAR filter, not a "stock ever exceeded X"
        # filter. A stock trading at ₹55 today and ₹45 last week will have
        # last week's row dropped and today's retained. The universe is
        # day-by-day, which is what we want for strategies that trade on
        # liquidity/price criteria that vary over time. Audit P3.7 —
        # 2026-04-21.
        df_tick_data = df_tick_data.filter(pl.col("close") > scanner_config["price_threshold"])
        df_tick_data = df_tick_data.filter(pl.col("avg_txn_turnover") > avg_day_transaction_threshold)
        df_tick_data = df_tick_data.filter(pl.col("gain") > n_day_gain_threshold)

        uid_series = df_tick_data.select(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )["uid"]
        shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

    # Remove prefetch data - keep only data within simulation range
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
    df_tick_data_original = df_tick_data_original.filter(pl.col("date_epoch") >= start_epoch)
    # subset=["open"] drops fill_missing_dates rows (null open,
    # backward-filled close) while keeping real rows with null volume
    # or average_price.
    df_tick_data_original = df_tick_data_original.drop_nulls(subset=["open"])

    df_tick_data_original = df_tick_data_original.with_columns(
        (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
    )
    df_tick_data_with_scanner_signals = create_scanner_signals(df_tick_data_original, shortlist_tracker)
    return df_tick_data_with_scanner_signals

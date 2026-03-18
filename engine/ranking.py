"""Order ranking functions: top_performer, top_gainer, top_average_txn.

Ported from ATO_Simulator/simulator/steps/simulate_step/util.py (lines 232-399)
and simulate_step_loader.py sort_orders() dispatcher.
"""

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.utils import create_epoch_wise_instrument_stats


def sort_orders(df_config_orders: pl.DataFrame, sim_config: dict, df_tick_data: pl.DataFrame, epoch_wise_instrument_stats=None) -> pl.DataFrame:
    """Dispatch to the correct ranking function based on sim_config.

    Ported from simulate_step_loader.py lines 167-186.
    """
    if df_config_orders.is_empty():
        return df_config_orders

    order_ranking_window_days = sim_config["order_ranking_window_days"]
    order_sorting_type = sim_config["order_sorting_type"]
    default_sorting_type = sim_config.get("default_sorting_type")

    if order_sorting_type == "top_average_txn" or default_sorting_type == "top_average_txn":
        df_config_orders = sort_orders_by_highest_avg_txn(df_config_orders, df_tick_data, order_ranking_window_days)
    elif order_sorting_type == "top_gainer" or default_sorting_type == "top_gainer":
        df_config_orders = sort_orders_by_highest_gainer(df_config_orders, df_tick_data, order_ranking_window_days)

    if order_sorting_type == "top_performer":
        if epoch_wise_instrument_stats is None:
            epoch_wise_instrument_stats = create_epoch_wise_instrument_stats(df_tick_data)
        df_config_orders = sort_orders_by_top_performer(
            df_config_orders, epoch_wise_instrument_stats, order_ranking_window_days
        )

    return df_config_orders


def sort_orders_by_highest_avg_txn(df_orders: pl.DataFrame, df_tick_data: pl.DataFrame, order_ranking_window_days: int) -> pl.DataFrame:
    """Rank orders by rolling average transaction volume."""
    df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
    df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])

    df_tick_data = df_tick_data.with_columns([
        pl.col("volume").shift(1).over("instrument").alias("prev_volume"),
        pl.col("average_price").shift(1).over("instrument").alias("prev_average_price"),
    ])

    df_tick_data = df_tick_data.with_columns(
        (pl.col("prev_volume") * pl.col("prev_average_price")).alias("avg_txn")
    )
    df_tick_data = df_tick_data.with_columns(
        pl.col("avg_txn")
        .rolling_mean(window_size=order_ranking_window_days, min_samples=1)
        .over("instrument")
        .alias("avg_txn")
    )

    df_tick_data = df_tick_data.with_columns(
        pl.col("avg_txn").rank(descending=True).over("date_epoch").alias("rank")
    )

    rank_df = df_tick_data.select([
        pl.col("date_epoch").alias("entry_epoch"),
        "instrument",
        "rank",
    ])

    df_orders = df_orders.join(rank_df, on=["instrument", "entry_epoch"], how="inner")
    df_orders = df_orders.sort(["entry_epoch", "rank"])
    return df_orders


def sort_orders_by_highest_gainer(df_orders: pl.DataFrame, df_tick_data: pl.DataFrame, order_ranking_window_days: int) -> pl.DataFrame:
    """Rank orders by n-day return percentage."""
    df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
    _df = df_tick_data.select(["date_epoch", "instrument", "close"])
    _df = _df.sort(["instrument", "date_epoch"])

    _df = _df.with_columns(
        pl.col("close").shift(1).over("instrument").alias("prev_close")
    )
    _df = _df.with_columns(
        pl.col("prev_close").shift(order_ranking_window_days).over("instrument").alias("ref_close")
    )
    _df = _df.with_columns(
        ((pl.col("prev_close") - pl.col("ref_close")) / pl.col("ref_close")).alias("gain")
    )
    _df = _df.with_columns(
        pl.col("gain").rank(descending=True).over("date_epoch").alias("rank")
    )

    rank_df = _df.select([
        pl.col("date_epoch").alias("entry_epoch"),
        "instrument",
        "rank",
    ])

    df_orders = df_orders.join(rank_df, on=["instrument", "entry_epoch"], how="inner")
    df_orders = df_orders.sort(["entry_epoch", "rank"])
    return df_orders


def calculate_daywise_instrument_score(df_orders: pl.DataFrame, instrument_day_wise_close: dict, window_size: int) -> pl.DataFrame:
    """Compute per-instrument performance scores using realized + unrealized P&L.

    IMPORTANT: Internally calls remove_overlapping_orders() which is load-bearing
    for correct top_performer scoring.
    """
    entry_epochs = sorted(df_orders["entry_epoch"].unique().to_list())
    df_orders = df_orders.sort(["instrument", "entry_epoch", "exit_epoch"])

    def remove_overlapping_orders(_df_orders: pl.DataFrame) -> pl.DataFrame:
        idx_to_keep = []
        for instrument_tuple, group in _df_orders.group_by("instrument"):
            group = group.sort(["entry_epoch", "exit_epoch"])
            current_end = None
            for row in group.iter_rows(named=True):
                if current_end and row["exit_epoch"] <= current_end:
                    continue
                current_end = row["exit_epoch"]
                idx_to_keep.append(row)
        if idx_to_keep:
            return pl.DataFrame(idx_to_keep)
        return _df_orders.clear()

    df_orders = remove_overlapping_orders(df_orders)

    df_orders = df_orders.with_columns(
        ((pl.col("exit_price") - pl.col("entry_price")) * 100.0 / pl.col("entry_price")).alias("profit")
    )

    full_scoreboard = {}
    # Convert to lists for fast iteration in the scoring loop
    order_instruments = df_orders["instrument"].to_list()
    order_entry_epochs = df_orders["entry_epoch"].to_list()
    order_exit_epochs = df_orders["exit_epoch"].to_list()
    order_entry_prices = df_orders["entry_price"].to_list()
    order_profits = df_orders["profit"].to_list()

    for epoch in entry_epochs:
        window_start = epoch - window_size
        score_map = {}

        for i in range(len(order_entry_epochs)):
            oe = order_entry_epochs[i]
            if oe >= epoch or oe < window_start:
                continue

            inst = order_instruments[i]
            # Realized P&L (sold orders)
            if order_exit_epochs[i] < epoch:
                score_map[inst] = score_map.get(inst, 0) + order_profits[i]
            else:
                # Unrealized P&L (open orders)
                entry_price = order_entry_prices[i]
                if entry_price == 0:
                    continue
                prev_epoch = epoch - SECONDS_IN_ONE_DAY
                if prev_epoch in instrument_day_wise_close and inst in instrument_day_wise_close[prev_epoch]:
                    last_close_price = instrument_day_wise_close[prev_epoch][inst]["close"]
                    score_map[inst] = score_map.get(inst, 0) + (
                        (last_close_price - entry_price) * 100 / entry_price
                    )

        full_scoreboard[epoch] = score_map

    rows = []
    for epoch, instruments in full_scoreboard.items():
        for inst, score in instruments.items():
            rows.append({"entry_epoch": epoch, "instrument": inst, "score": score})

    if not rows:
        return pl.DataFrame(schema={"entry_epoch": pl.Float64, "instrument": pl.Utf8, "score": pl.Float64, "rank": pl.Float64})

    df_score = pl.DataFrame(rows)
    df_score = df_score.with_columns(
        pl.col("score").rank(descending=True).over("entry_epoch").alias("rank")
    )
    df_score = df_score.sort(["entry_epoch", "rank"])
    return df_score


def sort_orders_by_top_performer(df_orders: pl.DataFrame, instrument_day_wise_close: dict, order_ranking_window_days: int) -> pl.DataFrame:
    """Walk-forward adaptive ranking using realized + unrealized P&L."""
    df_rank = calculate_daywise_instrument_score(
        df_orders, instrument_day_wise_close, order_ranking_window_days * SECONDS_IN_ONE_DAY
    )
    if "rank" in df_orders.columns:
        df_orders = df_orders.rename({"rank": "previous_rank"})
    else:
        df_orders = df_orders.with_columns(pl.lit(None).cast(pl.Float64).alias("previous_rank"))

    df_orders = df_orders.join(df_rank, on=["instrument", "entry_epoch"], how="left")

    # Keep +ve scores first, then nans, then -ve scores
    df_orders = df_orders.with_columns(
        pl.when(pl.col("score") > 0).then(0)
        .when(pl.col("score") <= 0).then(1)
        .otherwise(2)
        .alias("score_priority")
    )
    df_orders = df_orders.sort(["entry_epoch", "score_priority", "rank", "previous_rank"])
    df_orders = df_orders.drop("score_priority")
    return df_orders

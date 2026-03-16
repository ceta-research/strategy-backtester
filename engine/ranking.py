"""Order ranking functions: top_performer, top_gainer, top_average_txn.

Ported from ATO_Simulator/simulator/steps/simulate_step/util.py (lines 232-399)
and simulate_step_loader.py sort_orders() dispatcher.
"""

import numpy as np
import pandas as pd

from engine.constants import SECONDS_IN_ONE_DAY
from engine.utils import create_epoch_wise_instrument_stats


def sort_orders(df_config_orders, sim_config, df_tick_data, epoch_wise_instrument_stats=None):
    """Dispatch to the correct ranking function based on sim_config.

    Ported from simulate_step_loader.py lines 167-186.
    """
    if df_config_orders.empty:
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


def sort_orders_by_highest_avg_txn(df_orders, df_tick_data, order_ranking_window_days):
    """Rank orders by rolling average transaction volume."""
    df_tick_data = df_tick_data.copy()
    df_tick_data["instrument"] = df_tick_data["instrument"].astype("str")

    df_tick_data = df_tick_data.sort_values(["instrument", "date_epoch"])
    df_tick_data["prev_volume"] = df_tick_data.groupby("instrument")["volume"].shift(1)
    df_tick_data["prev_average_price"] = df_tick_data.groupby("instrument")["average_price"].shift(1)

    df_tick_data["avg_txn"] = df_tick_data["prev_volume"] * df_tick_data["prev_average_price"]
    df_tick_data["avg_txn"] = (
        df_tick_data.groupby(["instrument"])["avg_txn"]
        .rolling(order_ranking_window_days, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    df_tick_data["rank"] = df_tick_data.groupby("date_epoch")["avg_txn"].rank(ascending=False)
    df_tick_data = df_tick_data[["date_epoch", "instrument", "rank"]]
    df_orders = pd.merge(
        df_orders, df_tick_data.rename(columns={"date_epoch": "entry_epoch"}), on=["instrument", "entry_epoch"]
    )

    df_orders.sort_values(["entry_epoch", "rank"], ascending=[True, True], inplace=True)
    return df_orders


def sort_orders_by_highest_gainer(df_orders, df_tick_data, order_ranking_window_days):
    """Rank orders by n-day return percentage."""
    df_tick_data = df_tick_data.copy()
    df_tick_data["instrument"] = df_tick_data["instrument"].astype("str")
    _df_tick_data = df_tick_data[["date_epoch", "instrument", "close"]].copy()

    _df_tick_data = _df_tick_data.sort_values(["instrument", "date_epoch"])
    _df_tick_data["prev_close"] = _df_tick_data.groupby("instrument")["close"].shift(1)
    _df_tick_data["rank"] = _df_tick_data.groupby("instrument")["prev_close"].shift(order_ranking_window_days)
    _df_tick_data["rank"] = (_df_tick_data["prev_close"] - _df_tick_data["rank"]) / _df_tick_data["rank"]

    _df_tick_data["rank"] = _df_tick_data.groupby("date_epoch")["rank"].rank(ascending=False)

    df_orders = pd.merge(
        df_orders,
        _df_tick_data[["instrument", "date_epoch", "rank"]].rename(columns={"date_epoch": "entry_epoch"}),
        on=["instrument", "entry_epoch"],
    )

    df_orders.sort_values(["entry_epoch", "rank"], ascending=[True, True], inplace=True)
    return df_orders.reset_index(drop=True)


def calculate_daywise_instrument_score(df_orders, instrument_day_wise_close, window_size):
    """Compute per-instrument performance scores using realized + unrealized P&L.

    IMPORTANT: Internally calls remove_overlapping_orders() which is load-bearing
    for correct top_performer scoring.
    """
    entry_epochs = sorted(set(df_orders["entry_epoch"].unique()))
    df_orders = df_orders.copy()
    df_orders.sort_values(["instrument", "entry_epoch", "exit_epoch"], inplace=True)

    def remove_overlapping_orders(_df_orders):
        idx_to_keep = []
        for _, group in _df_orders.groupby("instrument"):
            current_end = None
            for idx, entry_epoch, exit_epoch in zip(group.index, group["entry_epoch"], group["exit_epoch"]):
                if current_end and exit_epoch <= current_end:
                    continue
                current_end = exit_epoch
                idx_to_keep.append(idx)
        return _df_orders.loc[idx_to_keep, :]

    df_orders = remove_overlapping_orders(df_orders)

    full_scoreboard = {}
    df_orders["profit"] = (df_orders["exit_price"] - df_orders["entry_price"]) * 100 / df_orders["entry_price"]
    for epoch in entry_epochs:
        window_start = epoch - window_size
        _df = df_orders[df_orders["entry_epoch"] < epoch]
        _df = _df[_df["entry_epoch"] >= window_start]

        # Realized P&L (sold orders)
        score_map = _df[_df["exit_epoch"] < epoch].groupby("instrument")["profit"].sum().to_dict()

        # Unrealized P&L (open orders)
        __df = _df[_df["exit_epoch"] >= epoch]
        for inst_instrument, inst_entry_price in zip(__df["instrument"], __df["entry_price"]):
            if inst_entry_price == 0:
                continue
            prev_epoch = epoch - SECONDS_IN_ONE_DAY
            if prev_epoch in instrument_day_wise_close and inst_instrument in instrument_day_wise_close[prev_epoch]:
                last_close_price = instrument_day_wise_close[prev_epoch][inst_instrument]["close"]
                score_map[inst_instrument] = score_map.get(inst_instrument, 0) + (
                    (last_close_price - inst_entry_price) * 100 / inst_entry_price
                )

        full_scoreboard[epoch] = score_map

    rows = []
    for epoch, instruments in full_scoreboard.items():
        for inst, score in instruments.items():
            rows.append([epoch, inst, score])

    df_score = pd.DataFrame(rows, columns=["entry_epoch", "instrument", "score"])
    df_score["rank"] = df_score.groupby(["entry_epoch"])["score"].rank(ascending=False)
    df_score.sort_values(["entry_epoch", "rank"], inplace=True)
    return df_score.reset_index(drop=True)


def sort_orders_by_top_performer(df_orders, instrument_day_wise_close, order_ranking_window_days):
    """Walk-forward adaptive ranking using realized + unrealized P&L."""
    df_rank = calculate_daywise_instrument_score(
        df_orders, instrument_day_wise_close, order_ranking_window_days * SECONDS_IN_ONE_DAY
    )
    if "rank" in df_orders.columns:
        df_orders = df_orders.rename(columns={"rank": "previous_rank"})
    else:
        df_orders = df_orders.copy()
        df_orders["previous_rank"] = np.nan

    df_orders = pd.merge(df_orders, df_rank, on=["instrument", "entry_epoch"], how="left")

    # Keep +ve scores first, then nans, then -ve scores
    df_orders["score_priority"] = np.select(
        [df_orders["score"] > 0, df_orders["score"] <= 0, df_orders["score"].isna()], [0, 1, 2], default=2
    )
    df_orders.sort_values(
        ["entry_epoch", "score_priority", "rank", "previous_rank"], ascending=[True, True, True, True], inplace=True
    )
    df_orders.drop(["score_priority"], axis=1, inplace=True)
    return df_orders.reset_index(drop=True)

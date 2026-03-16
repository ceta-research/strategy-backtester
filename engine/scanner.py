"""Scanner step: filter instruments by price, avg turnover, and n-day gain.

Ported from ATO_Simulator/simulator/steps/scanner_step/process_step.py.
"""

from itertools import product

import numpy as np
import pandas as pd

from engine.config_loader import get_scanner_config_iterator


def fill_missing_dates(df_tick_data):
    """Fill missing trading dates per instrument so rolling windows work correctly."""
    date_range = pd.date_range(
        start=pd.Timestamp(df_tick_data["date_epoch"].min(), unit="s"),
        end=pd.Timestamp(df_tick_data["date_epoch"].max(), unit="s"),
        freq="1D",
    )
    epochs = date_range.astype(np.int64) // 10**9

    df_tick_data["combo_id"] = df_tick_data["instrument"].astype(str) + "_" + df_tick_data["date_epoch"].astype(str)

    symbols = df_tick_data["instrument"].unique()
    required_combos = {f"{symbol}_{epoch}" for symbol, epoch in product(symbols, epochs)}
    missing_combos = required_combos - set(df_tick_data["combo_id"])

    df_tick_data.drop("combo_id", axis=1, inplace=True)

    if missing_combos:
        df_new_rows = pd.DataFrame(
            [{"instrument": combo.split("_")[0], "date_epoch": int(combo.split("_")[1])} for combo in missing_combos]
        )
        df_new_rows[["exchange", "symbol"]] = df_new_rows["instrument"].str.split(":", expand=True)

        df_tick_data = pd.concat([df_tick_data, df_new_rows], ignore_index=True, copy=False)
        df_tick_data.sort_values(["instrument", "date_epoch"], inplace=True)
        df_tick_data.reset_index(drop=True, inplace=True)

    return df_tick_data


def create_scanner_signals(df, signal_dict):
    """Add scanner_config_ids column based on which scanner configs shortlisted each row."""
    signal_sets = {k: set(v) for k, v in signal_dict.items()}

    def get_signal_string(uid):
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        return ",".join(sorted(signals)) if signals else pd.NA

    df["scanner_config_ids"] = df["uid"].apply(get_signal_string)
    return df


def process(context, df_tick_data_original):
    """Run scanner step: filter by price/turnover/gain, produce scanner_config_ids column.

    Args:
        context: dict with scanner_config_input, start_epoch, etc.
        df_tick_data_original: DataFrame with columns:
            date_epoch, open, high, low, close, average_price, volume, symbol, instrument, exchange

    Returns:
        DataFrame with scanner_config_ids column added.
    """
    df_tick_data_original = fill_missing_dates(df_tick_data_original)
    df_tick_data_original["close"] = df_tick_data_original.groupby("instrument")["close"].bfill()
    shortlist_tracker = {}

    for scanner_config in get_scanner_config_iterator(context):
        df_tick_data = df_tick_data_original.copy()
        idx_to_keep = set()
        for instrument in scanner_config["instruments"]:
            _df = df_tick_data[df_tick_data["exchange"] == instrument["exchange"]]
            if instrument["symbols"]:
                _df = _df[_df["symbol"].isin(instrument["symbols"])]
            idx_to_keep.update(_df.index)

        df_tick_data = df_tick_data.iloc[list(idx_to_keep)]

        avg_day_transaction_threshold_config = scanner_config["avg_day_transaction_threshold"]
        avg_day_transaction_period = avg_day_transaction_threshold_config["period"]
        avg_day_transaction_threshold = avg_day_transaction_threshold_config["threshold"]
        df_tick_data["avg_txn_turnover"] = df_tick_data["volume"] * df_tick_data["average_price"]
        df_tick_data["avg_txn_turnover"] = df_tick_data.groupby("instrument")["avg_txn_turnover"].transform(
            lambda x: x.rolling(window=avg_day_transaction_period, min_periods=1).mean()
        )

        n_day_gain_threshold_config = scanner_config["n_day_gain_threshold"]
        n_day_gain_period = n_day_gain_threshold_config["n"]
        n_day_gain_threshold = n_day_gain_threshold_config["threshold"]
        shifted_close = df_tick_data.groupby(["instrument"])["close"].shift(n_day_gain_period - 1)
        df_tick_data["gain"] = (df_tick_data["close"] - shifted_close) * 100 / shifted_close

        df_tick_data.dropna(inplace=True)
        df_tick_data = df_tick_data[df_tick_data["close"] > scanner_config["price_threshold"]]
        df_tick_data = df_tick_data[df_tick_data["avg_txn_turnover"] > avg_day_transaction_threshold]
        df_tick_data = df_tick_data[df_tick_data["gain"] > n_day_gain_threshold]

        shortlist_tracker[scanner_config["id"]] = set(
            (df_tick_data["instrument"].astype("str") + ":" + df_tick_data["date_epoch"].astype("str")).unique()
        )

    # Remove prefetch data - keep only data within simulation range
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
    df_tick_data_original = df_tick_data_original[df_tick_data_original["date_epoch"] >= start_epoch]
    df_tick_data_original.dropna(inplace=True)

    df_tick_data_original["uid"] = (
        df_tick_data_original["instrument"].astype("str") + ":" + df_tick_data_original["date_epoch"].astype("str")
    )
    df_tick_data_with_scanner_signals = create_scanner_signals(df_tick_data_original, shortlist_tracker)
    return df_tick_data_with_scanner_signals

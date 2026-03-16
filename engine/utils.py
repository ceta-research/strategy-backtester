"""Utility functions for the simulation engine.

Ported from ATO_Simulator/simulator/steps/simulate_step/util.py (lines 1-230).
Only the functions needed for in-memory pipeline are included.
"""

from collections import defaultdict
from typing import Dict, Set, Tuple

import pandas as pd


def create_config_df_loc_lookup(
    df: pd.DataFrame,
) -> Tuple[Dict[int, Set[int]], Dict[int, Set[int]], Dict[int, Set[int]]]:
    """Precompute lookup dictionaries for 3-way config ID intersection filtering.

    Returns tuple of (scanner_indices, entry_indices, exit_indices) where each
    maps config_id -> set of DataFrame row indices.
    """
    scanner_config_id_df_idx_map = defaultdict(set)
    entry_config_id_df_idx_map = defaultdict(set)
    exit_config_id_df_idx_map = defaultdict(set)

    for idx, scanner_ids, entry_ids, exit_ids in zip(
        df.index, df["scanner_config_ids"], df["entry_config_ids"], df["exit_config_ids"]
    ):
        for scanner_config_id in str(scanner_ids).split(","):
            if scanner_config_id:
                scanner_config_id_df_idx_map[int(scanner_config_id)].add(idx)

        for entry_config_id in str(entry_ids).split(","):
            if entry_config_id:
                entry_config_id_df_idx_map[int(entry_config_id)].add(idx)

        for exit_config_id in str(exit_ids).split(","):
            if exit_config_id:
                exit_config_id_df_idx_map[int(exit_config_id)].add(idx)

    return scanner_config_id_df_idx_map, entry_config_id_df_idx_map, exit_config_id_df_idx_map


def create_epoch_wise_instrument_stats(df_tick_data):
    """Build {epoch: {instrument: {close, avg_txn}}} lookup with forward-fill.

    Used by simulator for MTM and by ranking for instrument scores.
    """
    df_tick_data = df_tick_data.copy()
    df_tick_data["instrument"] = df_tick_data["instrument"].astype("str")
    df_tick_data["avg_txn"] = df_tick_data["volume"] * df_tick_data["average_price"]
    df_tick_data["avg_txn"] = (
        df_tick_data.groupby(["instrument"])["avg_txn"]
        .rolling(30, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    epoch_wise_data = {}
    one_day = 60 * 60 * 24

    for instrument, group in df_tick_data.groupby("instrument"):
        instrument_data = list(zip(group["date_epoch"], group["close"], group["avg_txn"]))
        data_dict = {epoch: {"close": close, "avg_txn": avg_txn} for epoch, close, avg_txn in instrument_data}

        start_epoch = group["date_epoch"].min()
        end_epoch = group["date_epoch"].max()

        # Forward-fill missing dates
        last_known_data = {}
        for epoch in range(start_epoch, end_epoch + one_day, one_day):
            if epoch in data_dict:
                last_known_data[instrument] = data_dict[epoch]
            elif last_known_data:
                data_dict[epoch] = last_known_data.get(instrument, {"close": None, "avg_txn": None})

        for epoch, values in data_dict.items():
            if epoch not in epoch_wise_data:
                epoch_wise_data[epoch] = {}
            epoch_wise_data[epoch][instrument] = values

    return epoch_wise_data

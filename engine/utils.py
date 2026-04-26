"""Utility functions for the simulation engine.

Ported from ATO_Simulator/simulator/steps/simulate_step/util.py (lines 1-230).
Only the functions needed for in-memory pipeline are included.
"""

from collections import defaultdict
from typing import Dict, Set, Tuple

import polars as pl


def create_config_df_loc_lookup(
    df: pl.DataFrame,
) -> Tuple[Dict[int, Set[int]], Dict[int, Set[int]], Dict[int, Set[int]]]:
    """Precompute lookup dictionaries for 3-way config ID intersection filtering.

    Returns tuple of (scanner_indices, entry_indices, exit_indices) where each
    maps config_id -> set of DataFrame row indices.
    """
    scanner_config_id_df_idx_map = defaultdict(set)
    entry_config_id_df_idx_map = defaultdict(set)
    exit_config_id_df_idx_map = defaultdict(set)

    scanner_ids_list = df["scanner_config_ids"].to_list()
    entry_ids_list = df["entry_config_ids"].to_list()
    exit_ids_list = df["exit_config_ids"].to_list()

    for idx, (scanner_ids, entry_ids, exit_ids) in enumerate(
        zip(scanner_ids_list, entry_ids_list, exit_ids_list)
    ):
        for scanner_config_id in str(scanner_ids).split(","):
            if scanner_config_id:
                scanner_config_id_df_idx_map[int(scanner_config_id)].add(idx)

        for entry_config_id in str(entry_ids).split(","):
            if entry_config_id:
                # Strip tier suffixes (e.g., "1_t1" -> "1") for tiered strategies.
                # This is intentional at the PIPELINE layer: all tiers of a
                # tiered strategy belong to the same base entry_config and
                # should be simulated together. Per-tier uniqueness is handled
                # at the SIMULATOR layer via engine.order_key.OrderKey, which
                # carries the full tier-suffixed string through. See
                # docs/archive/audit-2026-04/AUDIT_FINDINGS.md (Layer 2) for the historical bug.
                base_id = entry_config_id.split("_t")[0] if "_t" in entry_config_id else entry_config_id
                entry_config_id_df_idx_map[int(base_id)].add(idx)

        for exit_config_id in str(exit_ids).split(","):
            if exit_config_id:
                exit_config_id_df_idx_map[int(exit_config_id)].add(idx)

    return scanner_config_id_df_idx_map, entry_config_id_df_idx_map, exit_config_id_df_idx_map


def create_epoch_wise_instrument_stats(df_tick_data: pl.DataFrame) -> dict:
    """Build {epoch: {instrument: {close, avg_txn}}} lookup with forward-fill.

    Used by simulator for MTM and by ranking for instrument scores.
    """
    df_tick_data = df_tick_data.select(["instrument", "date_epoch", "close", "volume", "average_price"])

    df_tick_data = df_tick_data.with_columns(
        (pl.col("volume") * pl.col("average_price")).alias("avg_txn")
    )
    df_tick_data = df_tick_data.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("avg_txn")
        .rolling_mean(window_size=30, min_samples=1)
        .over("instrument")
        .alias("avg_txn")
    )

    epoch_wise_data = {}
    one_day = 60 * 60 * 24

    for instrument, group in df_tick_data.group_by("instrument", maintain_order=True):
        instrument_name = instrument[0]
        epochs = group["date_epoch"].to_list()
        closes = group["close"].to_list()
        avg_txns = group["avg_txn"].to_list()

        data_dict = {e: {"close": c, "avg_txn": a} for e, c, a in zip(epochs, closes, avg_txns)}

        start_epoch = min(epochs)
        end_epoch = max(epochs)

        # Forward-fill missing dates
        last_known_data = {}
        for epoch in range(start_epoch, end_epoch + one_day, one_day):
            if epoch in data_dict:
                last_known_data[instrument_name] = data_dict[epoch]
            elif last_known_data:
                data_dict[epoch] = last_known_data.get(instrument_name, {"close": None, "avg_txn": None})

        for epoch, values in data_dict.items():
            if epoch not in epoch_wise_data:
                epoch_wise_data[epoch] = {}
            epoch_wise_data[epoch][instrument_name] = values

    return epoch_wise_data

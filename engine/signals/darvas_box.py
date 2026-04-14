"""Darvas Box Breakout signal generator.

Entry: Close breaks above box high with volume confirmation.
Exit: Trailing stop based on box floor or fixed trailing percentage.

Based on Nicolas Darvas "How I Made $2,000,000 in the Stock Market".
Momentum breakout strategy with adaptive trailing stops.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, run_scanner, add_next_day_values


class DarvasBoxSignalGenerator:
    """Darvas Box breakout strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Darvas Box Signal Generation ---")
        t0 = time.time()

        # Phase 1: Scanner (liquidity filter)
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
        _, df = run_scanner(context, df_tick_data)

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            box_min_days = entry_config["box_min_days"]
            volume_breakout_mult = entry_config["volume_breakout_mult"]

            # Compute box indicators (shifted by 1 so box_high/low = prior N-day range)
            df_signals = df_ind.clone().sort(["instrument", "date_epoch"])
            df_signals = df_signals.with_columns([
                pl.col("high")
                .rolling_max(window_size=box_min_days, min_samples=box_min_days)
                .over("instrument")
                .shift(1)
                .over("instrument")
                .alias("box_high"),
                pl.col("low")
                .rolling_min(window_size=box_min_days, min_samples=box_min_days)
                .over("instrument")
                .shift(1)
                .over("instrument")
                .alias("box_low"),
                pl.col("volume")
                .rolling_mean(window_size=20, min_samples=1)
                .over("instrument")
                .alias("vol_avg_20"),
            ])

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )

            scanner_ids_df = df.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry condition: close > box_high AND volume > vol_avg * mult AND scanner passed
            df_entries = df_signals.filter(
                (pl.col("close") > pl.col("box_high"))
                & (pl.col("volume") > pl.col("vol_avg_20") * volume_breakout_mult)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("box_high").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]

                entry_rows = df_entries.select([
                    "instrument", "date_epoch", "next_epoch", "next_open", "next_volume",
                    "scanner_config_ids", "box_low",
                ]).to_dicts()

                # Build per-instrument data for exit walk
                exit_data = {}
                for inst_tuple, group in df_ind.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "highs": g["high"].to_list(),
                        "lows": g["low"].to_list(),
                    }

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]
                    initial_box_low = entry["box_low"]

                    if entry_price is None or initial_box_low is None:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    # Walk forward: trailing stop = max(box_low, highest_close * (1 - pct))
                    trailing_stop = initial_box_low
                    highest_close = entry_price
                    exit_epoch = None
                    exit_price = None

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        if c is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Update highest close and fixed trailing
                        if c > highest_close:
                            highest_close = c
                        fixed_trail = highest_close * (1 - trailing_stop_pct)

                        # Update box-based trailing: if current low > trailing_stop, raise it
                        low_j = ed["lows"][j]
                        if low_j is not None and low_j > trailing_stop:
                            trailing_stop = low_j

                        # Use whichever stop is higher
                        effective_stop = max(trailing_stop, fixed_trail)

                        if c < effective_stop or hold_days >= max_hold_days:
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

                    if exit_epoch is None and len(ed["epochs"]) > start_idx:
                        last_idx = len(ed["epochs"]) - 1
                        exit_epoch = ed["epochs"][last_idx]
                        exit_price = ed["closes"][last_idx]

                    if exit_epoch is None or exit_price is None or entry_price is None:
                        continue

                    all_order_rows.append({
                        "instrument": inst,
                        "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "entry_volume": entry["next_volume"] or 0,
                        "exit_volume": 0,
                        "scanner_config_ids": entry["scanner_config_ids"],
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                    })

        entry_elapsed = round(time.time() - t1, 2)

        if not all_order_rows:
            print(f"  Signal gen: {entry_elapsed}s, 0 orders")
            column_order = [
                "instrument", "entry_epoch", "exit_epoch",
                "entry_price", "exit_price", "entry_volume", "exit_volume",
                "scanner_config_ids", "entry_config_ids", "exit_config_ids",
            ]
            return pl.DataFrame(schema={
                c: pl.Utf8 if c in ("instrument", "scanner_config_ids", "entry_config_ids", "exit_config_ids")
                else pl.Float64 for c in column_order
            })

        df_orders = pl.DataFrame(all_order_rows)
        df_orders = df_orders.select([
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids",
        ]).sort(["instrument", "entry_epoch", "exit_epoch"])

        print(f"  Signal gen: {entry_elapsed}s, {df_orders.height} orders")
        return df_orders

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "box_min_days": entry_cfg.get("box_min_days", [10]),
            "volume_breakout_mult": entry_cfg.get("volume_breakout_mult", [1.5]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [8]),
            "max_hold_days": exit_cfg.get("max_hold_days", [30]),
        }

register_strategy("darvas_box", DarvasBoxSignalGenerator)

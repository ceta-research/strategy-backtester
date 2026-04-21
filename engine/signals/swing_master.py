"""Swing Master Plan signal generator.

Entry: Uptrend (close > SMA10 > SMA20) with 3-day pullback,
       short-term Force Index < 0, long-term Force Index > 0.
Exit: Target profit, stop loss, or trailing stop.

Based on Larry Swing "The Practical Guide to Swing Trading".
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, run_scanner


class SwingMasterSignalGenerator:
    """Swing Master Plan trend pullback strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Swing Master Signal Generation ---")
        t0 = time.time()

        # Phase 1: Scanner (liquidity filter)
        _, df = run_scanner(context, df_tick_data)
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            sma_short_period = entry_config["sma_short"]
            sma_long_period = entry_config["sma_long"]
            pullback_days = entry_config["pullback_days"]

            df_signals = df_ind.clone().sort(["instrument", "date_epoch"])

            # SMAs
            df_signals = df_signals.with_columns([
                pl.col("close")
                .rolling_mean(window_size=sma_short_period, min_samples=sma_short_period)
                .over("instrument")
                .alias("sma_short"),
                pl.col("close")
                .rolling_mean(window_size=sma_long_period, min_samples=sma_long_period)
                .over("instrument")
                .alias("sma_long"),
            ])

            # Force Index = (close - prev_close) * volume
            df_signals = df_signals.with_columns(
                ((pl.col("close") - pl.col("close").shift(1).over("instrument")) * pl.col("volume"))
                .alias("force_index")
            )

            # EMA of Force Index (3 and 13)
            df_signals = df_signals.with_columns([
                pl.col("force_index")
                .ewm_mean(span=3, adjust=False, min_samples=3)
                .over("instrument")
                .alias("fi_ema_3"),
                pl.col("force_index")
                .ewm_mean(span=13, adjust=False, min_samples=13)
                .over("instrument")
                .alias("fi_ema_13"),
            ])

            # Pullback: monotonically declining highs for N consecutive days
            # Check high[t-i+1] < high[t-i] for each step (consecutive pairs)
            for i in range(1, pullback_days + 1):
                df_signals = df_signals.with_columns(
                    (pl.col("high").shift(i - 1).over("instrument")
                     < pl.col("high").shift(i).over("instrument"))
                    .alias(f"_decline_{i}")
                )
            decline_cols = [f"_decline_{i}" for i in range(1, pullback_days + 1)]
            df_signals = df_signals.with_columns(
                pl.all_horizontal(*[pl.col(c) for c in decline_cols]).alias("pullback_n")
            )
            df_signals = df_signals.drop(decline_cols)

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )

            scanner_ids_df = df.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry: uptrend + pullback + force index divergence + scanner passed
            df_entries = df_signals.filter(
                (pl.col("close") > pl.col("sma_short"))
                & (pl.col("sma_short") > pl.col("sma_long"))
                & (pl.col("pullback_n") == True)
                & (pl.col("fi_ema_3") < 0)
                & (pl.col("fi_ema_13") > 0)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                target_pct = exit_config["target_pct"]
                stop_loss_pct = exit_config["stop_loss_pct"]
                max_hold_days = exit_config["max_hold_days"]
                trailing_buffer_pct = exit_config.get("trailing_buffer_pct", 0.002)

                entry_rows = df_entries.select([
                    "instrument", "date_epoch", "next_epoch", "next_open", "next_volume",
                    "scanner_config_ids",
                ]).to_dicts()

                # Build per-instrument exit data
                exit_data = {}
                for inst_tuple, group in df_ind.group_by("instrument", maintain_order=True):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "lows": g["low"].to_list(),
                    }

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    if entry_price is None:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    target_price = entry_price * (1 + target_pct)
                    stop_price = entry_price * (1 - stop_loss_pct)
                    exit_epoch = None
                    exit_price = None

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        low_j = ed["lows"][j]
                        if c is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Trailing stop: raise stop if low * (1 - buffer) > current stop
                        if low_j is not None:
                            candidate_stop = low_j * (1 - trailing_buffer_pct)
                            if candidate_stop > stop_price:
                                stop_price = candidate_stop

                        if c >= target_price or c <= stop_price or hold_days >= max_hold_days:
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
            "sma_short": entry_cfg.get("sma_short", [10]),
            "sma_long": entry_cfg.get("sma_long", [20]),
            "pullback_days": entry_cfg.get("pullback_days", [3]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "target_pct": exit_cfg.get("target_pct", [0.07]),
            "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0.04]),
            "max_hold_days": exit_cfg.get("max_hold_days", [20]),
            "trailing_buffer_pct": exit_cfg.get("trailing_buffer_pct", [0.002]),
        }

register_strategy("swing_master", SwingMasterSignalGenerator)

"""Index Green Candle Momentum signal generator.

Entry: N consecutive green candles (close > open), buy at next open.
Exit:  M consecutive red candles (close < open) OR take-profit % OR stop-loss %.

Designed for single-instrument index ETFs (NIFTYBEES, SPY, QQQ).
Uses the standard pipeline: scanner -> signal gen -> simulator.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, run_scanner, add_next_day_values


class IndexGreenCandleSignalGenerator:
    """Buy after N green candles, exit on M red candles or TP/SL."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Index Green Candle Momentum Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (lightweight for single-instrument, but keeps pipeline compat)
        _, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Green candle: close > open
        df_ind = df_ind.with_columns(
            (pl.col("close") > pl.col("open")).cast(pl.Int32).alias("is_green")
        )

        # Phase 3: Generate orders per entry/exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            green_candles = entry_config["green_candles"]

            # Compute rolling sum of green candles
            df_signals = df_ind.clone().with_columns(
                pl.col("is_green")
                .rolling_sum(window_size=green_candles, min_samples=green_candles)
                .over("instrument")
                .alias("green_streak")
            )

            # Trim to simulation range and merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            for exit_config in get_exit_config_iterator(context):
                red_candles_exit = exit_config["red_candles_exit"]
                take_profit_pct = exit_config["take_profit_pct"]
                stop_loss_pct = exit_config["stop_loss_pct"]

                # Build per-instrument data for forward exit iteration
                for inst_tuple, group in df_signals.group_by("instrument", maintain_order=True):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")

                    epochs = g["date_epoch"].to_list()
                    opens = g["open"].to_list()
                    closes = g["close"].to_list()
                    greens = g["is_green"].to_list()
                    green_streaks = g["green_streak"].to_list()
                    next_epochs = g["next_epoch"].to_list()
                    next_opens = g["next_open"].to_list()
                    next_volumes = g["next_volume"].to_list()
                    scanner_ids = g["scanner_config_ids"].to_list()

                    i = 0
                    while i < len(epochs):
                        # Check entry: N consecutive green candles + scanner pass
                        streak = green_streaks[i]
                        if streak is None or streak < green_candles:
                            i += 1
                            continue
                        if scanner_ids[i] is None:
                            i += 1
                            continue
                        if next_epochs[i] is None or next_opens[i] is None:
                            i += 1
                            continue

                        entry_epoch = next_epochs[i]
                        entry_price = next_opens[i]
                        entry_volume = next_volumes[i] or 0

                        if entry_price is None or entry_price <= 0:
                            i += 1
                            continue

                        # Forward iterate to find exit
                        # Start from i+2: i is signal day, i+1 is entry day.
                        # Don't exit on entry day itself (we just bought at open).
                        exit_epoch = None
                        exit_price = None
                        red_count = 0

                        for j in range(i + 2, len(epochs)):
                            c = closes[j]
                            o = opens[j]
                            if c is None or o is None:
                                continue

                            # Check take profit
                            if take_profit_pct > 0 and c >= entry_price * (1 + take_profit_pct):
                                exit_epoch = epochs[j]
                                exit_price = c
                                break

                            # Check stop loss
                            if stop_loss_pct > 0 and c <= entry_price * (1 - stop_loss_pct):
                                exit_epoch = epochs[j]
                                exit_price = c
                                break

                            # Check red candle count
                            is_red = c < o
                            if is_red:
                                red_count += 1
                            else:
                                red_count = 0

                            if red_count >= red_candles_exit:
                                exit_epoch = epochs[j]
                                exit_price = c
                                break

                        # If no exit found, use last bar
                        if exit_epoch is None and len(epochs) > i + 1:
                            exit_epoch = epochs[-1]
                            exit_price = closes[-1]

                        if exit_epoch is None or exit_price is None:
                            i += 1
                            continue

                        all_order_rows.append({
                            "instrument": inst_name,
                            "entry_epoch": entry_epoch,
                            "exit_epoch": exit_epoch,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "entry_volume": entry_volume,
                            "exit_volume": 0,
                            "scanner_config_ids": scanner_ids[i],
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

                        # Skip to exit bar to avoid overlapping trades
                        # Find the index of exit_epoch
                        try:
                            exit_idx = epochs.index(exit_epoch, i + 1)
                            i = exit_idx + 1
                        except ValueError:
                            i += 1

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
            "green_candles": entry_cfg.get("green_candles", [2]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "red_candles_exit": exit_cfg.get("red_candles_exit", [1]),
            "take_profit_pct": exit_cfg.get("take_profit_pct", [0]),
            "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
        }

register_strategy("index_green_candle", IndexGreenCandleSignalGenerator)

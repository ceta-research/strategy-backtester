"""Index SMA Crossover signal generator.

Entry: SMA(short) crosses above SMA(long), buy at next open.
Exit:  SMA(short) crosses below SMA(long), sell at close. Optional stop loss.

Designed for single-instrument index ETFs (NIFTYBEES, SPY, QQQ).
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, run_scanner


class IndexSmaCrossoverSignalGenerator:
    """Classic SMA crossover trend-following on index."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Index SMA Crossover Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner
        _, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute indicators
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            sma_short_period = entry_config["sma_short"]
            sma_long_period = entry_config["sma_long"]

            # Compute SMAs
            df_signals = df_ind.clone().with_columns([
                pl.col("close").rolling_mean(window_size=sma_short_period, min_samples=sma_short_period)
                .over("instrument").alias("sma_short"),
                pl.col("close").rolling_mean(window_size=sma_long_period, min_samples=sma_long_period)
                .over("instrument").alias("sma_long"),
            ])

            # Crossover detection: sma_short > sma_long today AND sma_short <= sma_long yesterday
            df_signals = df_signals.with_columns([
                (pl.col("sma_short") > pl.col("sma_long")).alias("above"),
                (pl.col("sma_short").shift(1).over("instrument") <= pl.col("sma_long").shift(1).over("instrument")).alias("was_below"),
            ])
            df_signals = df_signals.with_columns(
                (pl.col("above") & pl.col("was_below")).alias("crossover_up")
            )

            # Trim to simulation range and merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            for exit_config in get_exit_config_iterator(context):
                stop_loss_pct = exit_config["stop_loss_pct"]
                max_hold_days = exit_config["max_hold_days"]

                for inst_tuple, group in df_signals.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")

                    epochs = g["date_epoch"].to_list()
                    closes = g["close"].to_list()
                    sma_shorts = g["sma_short"].to_list()
                    sma_longs = g["sma_long"].to_list()
                    crossovers = g["crossover_up"].to_list()
                    next_epochs = g["next_epoch"].to_list()
                    next_opens = g["next_open"].to_list()
                    next_volumes = g["next_volume"].to_list()
                    scanner_ids = g["scanner_config_ids"].to_list()

                    i = 0
                    while i < len(epochs):
                        if not crossovers[i] or scanner_ids[i] is None:
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

                        # Forward iterate to find exit: SMA cross down, stop loss, or max hold
                        exit_epoch = None
                        exit_price = None

                        for j in range(i + 1, len(epochs)):
                            c = closes[j]
                            ss = sma_shorts[j]
                            sl = sma_longs[j]

                            if c is None:
                                continue

                            # Stop loss
                            if stop_loss_pct > 0 and c <= entry_price * (1 - stop_loss_pct):
                                exit_epoch = epochs[j]
                                exit_price = c
                                break

                            # Max hold days
                            if max_hold_days > 0:
                                hold_days = (epochs[j] - entry_epoch) / 86400
                                if hold_days >= max_hold_days:
                                    exit_epoch = epochs[j]
                                    exit_price = c
                                    break

                            # SMA cross down: sma_short < sma_long
                            if ss is not None and sl is not None and ss < sl:
                                exit_epoch = epochs[j]
                                exit_price = c
                                break

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

                        # Skip to exit bar
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
            "sma_short": entry_cfg.get("sma_short", [10]),
            "sma_long": entry_cfg.get("sma_long", [50]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
            "max_hold_days": exit_cfg.get("max_hold_days", [0]),
        }

register_strategy("index_sma_crossover", IndexSmaCrossoverSignalGenerator)

"""Connors RSI(2) mean reversion signal generator.

Entry: RSI(n) < threshold AND close > SMA(trend_period)
Exit: Close > SMA(exit_period) OR cumulative RSI > exit_threshold

Based on Larry Connors' "Short Term Trading Strategies That Work".
Mean reversion on oversold pullbacks within an uptrend.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, fill_missing_dates, backfill_close, add_next_day_values, run_scanner


def compute_rsi(df: pl.DataFrame, period: int, col: str = "close") -> pl.DataFrame:
    """Compute RSI using Wilder's smoothing (exponential moving average of gains/losses).

    Returns DataFrame with 'rsi' column added.
    """
    df = df.sort(["instrument", "date_epoch"]).with_columns(
        (pl.col(col) - pl.col(col).shift(1).over("instrument")).alias("_price_change")
    )
    df = df.with_columns([
        pl.col("_price_change").clip(lower_bound=0.0).alias("_gain"),
        (-pl.col("_price_change").clip(upper_bound=0.0)).alias("_loss"),
    ])
    df = df.with_columns([
        pl.col("_gain")
        .ewm_mean(span=period, adjust=False, min_samples=period)
        .over("instrument")
        .alias("_avg_gain"),
        pl.col("_loss")
        .ewm_mean(span=period, adjust=False, min_samples=period)
        .over("instrument")
        .alias("_avg_loss"),
    ])
    df = df.with_columns(
        pl.when(pl.col("_avg_loss") == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / pl.col("_avg_loss")))
        .alias("rsi")
    )
    df = df.drop(["_price_change", "_gain", "_loss", "_avg_gain", "_avg_loss"])
    return df


class ConnorsRsiSignalGenerator:
    """Connors RSI(2) mean reversion strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Connors RSI Signal Generation ---")
        t0 = time.time()

        # Phase 1: Scanner (liquidity filter)
        # No fill_missing_dates needed - RSI/SMA handle gaps naturally.
        # fill_missing_dates would OOM on large universes (16K+ instruments).
        _, df = run_scanner(context, df_tick_data)
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 2: Compute indicators on full data (need prefetch for SMA/RSI warmup)
        df_ind = df_tick_data.clone()

        # Add next-day values for entry/exit at next open
        df_ind = add_next_day_values(df_ind)

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            rsi_period = entry_config["rsi_period"]
            rsi_threshold = entry_config["rsi_entry_threshold"]
            sma_trend_period = entry_config["sma_trend_period"]

            # Compute RSI and trend SMA
            df_signals = compute_rsi(df_ind.clone(), rsi_period)
            df_signals = df_signals.sort(["instrument", "date_epoch"]).with_columns(
                pl.col("close")
                .rolling_mean(window_size=sma_trend_period, min_samples=sma_trend_period)
                .over("instrument")
                .alias("sma_trend")
            )

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )

            # Join scanner_config_ids from scanner phase
            scanner_ids_df = df.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry condition: RSI < threshold AND close > SMA AND scanner passed
            df_entries = df_signals.filter(
                (pl.col("rsi") < rsi_threshold)
                & (pl.col("close") > pl.col("sma_trend"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                exit_sma_period = exit_config.get("exit_sma_period", 5)

                # Precompute exit SMA on full signals data
                df_exit = df_signals.sort(["instrument", "date_epoch"]).with_columns(
                    pl.col("close")
                    .rolling_mean(window_size=exit_sma_period, min_samples=1)
                    .over("instrument")
                    .alias("exit_sma")
                )

                # For each entry, find first exit date where close > exit_sma
                entry_rows = df_entries.select([
                    "instrument", "date_epoch", "next_epoch", "next_open", "next_volume",
                    "scanner_config_ids",
                ]).to_dicts()

                # Also compute the trend SMA for safety exit (close < trend SMA = trend break)
                df_exit = df_exit.sort(["instrument", "date_epoch"]).with_columns(
                    pl.col("close")
                    .rolling_mean(window_size=entry_config["sma_trend_period"], min_samples=entry_config["sma_trend_period"])
                    .over("instrument")
                    .alias("trend_sma")
                )

                max_hold_days = exit_config.get("max_hold_days", 20)

                # Build per-instrument exit lookup
                exit_data = {}
                for inst_tuple, group in df_exit.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "exit_smas": g["exit_sma"].to_list(),
                        "trend_smas": g["trend_sma"].to_list(),
                        "next_opens": g["next_open"].to_list() if "next_open" in g.columns else [],
                        "next_volumes": g["next_volume"].to_list() if "next_volume" in g.columns else [],
                        "next_epochs": g["next_epoch"].to_list() if "next_epoch" in g.columns else [],
                    }

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]

                    # Find index of entry epoch in exit data
                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    # Walk forward to find exit
                    exit_epoch = None
                    exit_price = None
                    exit_volume = None

                    for j in range(start_idx, len(ed["epochs"])):
                        if ed["closes"][j] is None or ed["exit_smas"][j] is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Exit 1: Close > exit SMA (profit target / mean reversion complete)
                        # Exit 2: Close < trend SMA (trend break safety)
                        # Exit 3: Held > max_hold_days (time stop)
                        should_exit = (
                            ed["closes"][j] > ed["exit_smas"][j]
                            or (ed["trend_smas"][j] is not None and ed["closes"][j] < ed["trend_smas"][j])
                            or hold_days >= max_hold_days
                        )

                        if should_exit:
                            if j < len(ed["next_epochs"]) and ed["next_epochs"][j] is not None:
                                exit_epoch = ed["next_epochs"][j]
                                exit_price = ed["next_opens"][j]
                                exit_volume = ed["next_volumes"][j]
                            else:
                                exit_epoch = ed["epochs"][j]
                                exit_price = ed["closes"][j]
                                exit_volume = 0
                            break

                    # If no exit signal, exit at last available close
                    if exit_epoch is None and len(ed["epochs"]) > start_idx:
                        last_idx = len(ed["epochs"]) - 1
                        exit_epoch = ed["epochs"][last_idx]
                        exit_price = ed["closes"][last_idx]
                        exit_volume = 0

                    if exit_epoch is None or exit_price is None or entry["next_open"] is None:
                        continue

                    all_order_rows.append({
                        "instrument": inst,
                        "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch,
                        "entry_price": entry["next_open"],
                        "exit_price": exit_price,
                        "entry_volume": entry["next_volume"],
                        "exit_volume": exit_volume,
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
            "rsi_period": entry_cfg.get("rsi_period", [2]),
            "rsi_entry_threshold": entry_cfg.get("rsi_entry_threshold", [5]),
            "sma_trend_period": entry_cfg.get("sma_trend_period", [200]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "exit_sma_period": exit_cfg.get("exit_sma_period", [5]),
            "max_hold_days": exit_cfg.get("max_hold_days", [20]),
        }

register_strategy("connors_rsi", ConnorsRsiSignalGenerator)

"""Overnight Hold signal generator.

Entry: Buy at close.
Exit:  Sell at next day's open.

Literature: Since 1993, all S&P 500 gains came from overnight holds.
Buy-close/sell-open generated +1,100% cumulative (1993-present).
Overnight risk premium compensates holders for uncertainty.
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values


class OvernightHoldSignalGenerator:
    """Overnight hold strategy: buy at close, sell at next open."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Overnight Hold Signal Generation ---")
        t0 = time.time()

        df = df_tick_data.clone()
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker = {}
        for scanner_config in get_scanner_config_iterator(context):
            df_scan = df.clone()

            filter_exprs = []
            for instrument in scanner_config["instruments"]:
                if instrument["symbols"]:
                    filter_exprs.append(
                        (pl.col("exchange") == instrument["exchange"])
                        & (pl.col("symbol").is_in(instrument["symbols"]))
                    )
                else:
                    filter_exprs.append(pl.col("exchange") == instrument["exchange"])
            if filter_exprs:
                combined = filter_exprs[0]
                for expr in filter_exprs[1:]:
                    combined = combined | expr
                df_scan = df_scan.filter(combined)

            atc = scanner_config["avg_day_transaction_threshold"]
            df_scan = df_scan.with_columns(
                (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
            )
            df_scan = df_scan.sort(["instrument", "date_epoch"]).with_columns(
                pl.col("avg_txn_turnover")
                .rolling_mean(window_size=atc["period"], min_samples=1)
                .over("instrument")
                .alias("avg_txn_turnover")
            )
            df_scan = df_scan.drop_nulls()
            df_scan = df_scan.filter(pl.col("close") > scanner_config["price_threshold"])
            df_scan = df_scan.filter(pl.col("avg_txn_turnover") > atc["threshold"])

            uid_series = df_scan.select(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )["uid"]
            shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

        # Trim prefetch, tag scanner signals
        df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
        df_trimmed = df_trimmed.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )
        signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
        uids = df_trimmed["uid"].to_list()
        uid_to_signals = {}
        for uid in uids:
            signals = [str(k) for k, v in signal_sets.items() if uid in v]
            uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
        df_trimmed = df_trimmed.with_columns(
            pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
        )

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df_trimmed.height} rows")

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)

        # RSI(14) for optional filter
        delta = pl.col("close") - pl.col("close").shift(1).over("instrument")
        df_ind = df_ind.sort(["instrument", "date_epoch"]).with_columns(
            delta.alias("price_change")
        )
        df_ind = df_ind.with_columns([
            pl.when(pl.col("price_change") > 0).then(pl.col("price_change")).otherwise(0.0).alias("gain"),
            pl.when(pl.col("price_change") < 0).then(-pl.col("price_change")).otherwise(0.0).alias("loss"),
        ])
        df_ind = df_ind.sort(["instrument", "date_epoch"]).with_columns([
            pl.col("gain").rolling_mean(window_size=14, min_samples=14).over("instrument").alias("avg_gain"),
            pl.col("loss").rolling_mean(window_size=14, min_samples=14).over("instrument").alias("avg_loss"),
        ])
        df_ind = df_ind.with_columns(
            pl.when(pl.col("avg_loss") > 0)
            .then(100.0 - (100.0 / (1.0 + pl.col("avg_gain") / pl.col("avg_loss"))))
            .otherwise(100.0)
            .alias("rsi_14")
        )

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            buy_on_down_day = entry_config.get("buy_on_down_day", False)
            min_rsi = entry_config.get("min_rsi_14", 0)

            df_signals = df_ind.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry filters
            filter_expr = (
                (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("close") > 0)
            )

            if buy_on_down_day:
                filter_expr = filter_expr & (pl.col("close") < pl.col("open"))

            if min_rsi > 0:
                filter_expr = filter_expr & (
                    (pl.col("rsi_14") >= min_rsi) | pl.col("rsi_14").is_null()
                )

            df_entries = df_signals.filter(filter_expr)

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                entry_rows = df_entries.to_dicts()

                for row in entry_rows:
                    if row["close"] is None or row["next_open"] is None:
                        continue
                    if row["close"] <= 0 or row["next_open"] <= 0:
                        continue

                    all_order_rows.append({
                        "instrument": row["instrument"],
                        "entry_epoch": row["date_epoch"],         # entry day (buy at close)
                        "exit_epoch": row["next_epoch"],          # next day (sell at open)
                        "entry_price": row["close"],              # buy at close
                        "exit_price": row["next_open"],           # sell at next open
                        "entry_volume": row.get("volume", 0),
                        "exit_volume": row.get("next_volume", 0),
                        "scanner_config_ids": row["scanner_config_ids"],
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


register_strategy("overnight_hold", OvernightHoldSignalGenerator)

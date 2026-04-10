"""Bollinger Band Mean Reversion + SMA200 trend filter signal generator.

Entry: close < lower BB(bb_period, bb_std) AND close > SMA(sma_trend_period)
Exit:  close > upper BB OR held > max_hold_days

Based on highly-upvoted r/algotrading backtests:
- SPY 20y: 10% CAGR, 10.7% DD, Calmar ~1.0, 85% WR (multi-ETF)
- Key: SMA200 filter avoids crash drawdowns; upper BB exit lets winners run.
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values


class BbMeanReversionSignalGenerator:
    """Bollinger Band mean reversion with SMA trend filter."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- BB Mean Reversion + SMA200 Signal Generation ---")
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

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            bb_period = entry_config["bb_period"]
            bb_std = entry_config["bb_std"]
            sma_trend_period = entry_config["sma_trend_period"]

            df_signals = df_ind.sort(["instrument", "date_epoch"])

            # Bollinger Bands: mid = SMA(bb_period), upper/lower = mid +/- bb_std * rolling_std
            df_signals = df_signals.with_columns([
                pl.col("close")
                .rolling_mean(window_size=bb_period, min_samples=bb_period)
                .over("instrument")
                .alias("bb_mid"),
                pl.col("close")
                .rolling_std(window_size=bb_period, min_samples=bb_period)
                .over("instrument")
                .alias("bb_std_val"),
            ])
            df_signals = df_signals.with_columns([
                (pl.col("bb_mid") + bb_std * pl.col("bb_std_val")).alias("bb_upper"),
                (pl.col("bb_mid") - bb_std * pl.col("bb_std_val")).alias("bb_lower"),
            ])

            # SMA200 trend filter
            df_signals = df_signals.with_columns(
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
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry: close < lower BB AND close > SMA200 AND scanner passed
            df_entries = df_signals.filter(
                (pl.col("close") < pl.col("bb_lower"))
                & (pl.col("close") > pl.col("sma_trend"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                max_hold_days = exit_config["max_hold_days"]

                # Build per-instrument exit lookup
                exit_data = {}
                for inst_tuple, group in df_signals.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "bb_uppers": g["bb_upper"].to_list(),
                    }

                entry_rows = df_entries.select([
                    "instrument", "date_epoch", "next_epoch", "next_open", "next_volume",
                    "scanner_config_ids",
                ]).to_dicts()

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data or entry["next_open"] is None:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch = None
                    exit_price = None

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        bb_up = ed["bb_uppers"][j]

                        if c is None or bb_up is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Exit: close > upper BB (mean reversion complete) OR max hold days
                        if c > bb_up or hold_days >= max_hold_days:
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

                    # Fallback: exit at last available bar
                    if exit_epoch is None and len(ed["epochs"]) > start_idx:
                        last_idx = len(ed["epochs"]) - 1
                        exit_epoch = ed["epochs"][last_idx]
                        exit_price = ed["closes"][last_idx]

                    if exit_epoch is None or exit_price is None:
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
            "bb_period": entry_cfg.get("bb_period", [20]),
            "bb_std": entry_cfg.get("bb_std", [2.0]),
            "sma_trend_period": entry_cfg.get("sma_trend_period", [200]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "max_hold_days": exit_cfg.get("max_hold_days", [400]),
        }

register_strategy("bb_mean_reversion", BbMeanReversionSignalGenerator)

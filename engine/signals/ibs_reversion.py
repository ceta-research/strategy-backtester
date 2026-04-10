"""IBS (Internal Bar Strength) mean reversion signal generator.

Entry: IBS < entry_threshold AND close > SMA(trend_period)
Exit: IBS > exit_threshold OR held > max_hold_days

IBS = (close - low) / (high - low)
Range: 0 (closed at low) to 1 (closed at high).
Low IBS = selling pressure exhaustion, mean reversion buy signal.

Literature: 13-40% CAGR on indices/ETFs, 74% win rate, 15-22% max DD.
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values


class IbsReversionSignalGenerator:
    """IBS mean reversion strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- IBS Mean Reversion Signal Generation ---")
        t0 = time.time()

        df = df_tick_data.clone()
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker = {}
        for scanner_config in get_scanner_config_iterator(context):
            df_scan = df.clone()

            # Exchange/symbol filter
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

            # Avg turnover filter
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

        # IBS = (close - low) / (high - low)
        df_ind = df_ind.with_columns(
            pl.when((pl.col("high") - pl.col("low")).abs() < 1e-10)
            .then(0.5)
            .otherwise((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .alias("ibs")
        )

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            ibs_entry_threshold = entry_config["ibs_entry_threshold"]
            sma_trend_period = entry_config["sma_trend_period"]

            # Compute trend SMA
            df_signals = df_ind.sort(["instrument", "date_epoch"]).with_columns(
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

            # Entry: IBS < threshold AND close > SMA AND scanner passed
            df_entries = df_signals.filter(
                (pl.col("ibs") < ibs_entry_threshold)
                & (pl.col("close") > pl.col("sma_trend"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                ibs_exit_threshold = exit_config["ibs_exit_threshold"]
                max_hold_days = exit_config["max_hold_days"]

                # Build per-instrument exit lookup
                exit_data = {}
                for inst_tuple, group in df_signals.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "ibs_vals": g["ibs"].to_list(),
                        "closes": g["close"].to_list(),
                        "next_opens": g["next_open"].to_list() if "next_open" in g.columns else [],
                        "next_volumes": g["next_volume"].to_list() if "next_volume" in g.columns else [],
                        "next_epochs": g["next_epoch"].to_list() if "next_epoch" in g.columns else [],
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

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch = None
                    exit_price = None
                    exit_volume = None

                    for j in range(start_idx, len(ed["epochs"])):
                        if ed["closes"][j] is None or ed["ibs_vals"][j] is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Exit: IBS > threshold (mean reversion complete) OR max hold
                        if ed["ibs_vals"][j] > ibs_exit_threshold or hold_days >= max_hold_days:
                            if j < len(ed["next_epochs"]) and ed["next_epochs"][j] is not None:
                                exit_epoch = ed["next_epochs"][j]
                                exit_price = ed["next_opens"][j]
                                exit_volume = ed["next_volumes"][j]
                            else:
                                exit_epoch = ed["epochs"][j]
                                exit_price = ed["closes"][j]
                                exit_volume = 0
                            break

                    if exit_epoch is None and len(ed["epochs"]) > start_idx:
                        last_idx = len(ed["epochs"]) - 1
                        exit_epoch = ed["epochs"][last_idx]
                        exit_price = ed["closes"][last_idx]
                        exit_volume = 0

                    if exit_epoch is None or exit_price is None:
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
            "ibs_entry_threshold": entry_cfg.get("ibs_entry_threshold", [0.2]),
            "sma_trend_period": entry_cfg.get("sma_trend_period", [200]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "ibs_exit_threshold": exit_cfg.get("ibs_exit_threshold", [0.8]),
            "max_hold_days": exit_cfg.get("max_hold_days", [10]),
        }

register_strategy("ibs_mean_reversion", IbsReversionSignalGenerator)

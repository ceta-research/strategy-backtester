"""Extended IBS Mean Reversion signal generator.

Entry: close < (10d_high - 2.5 * (25d_avg_high - 25d_avg_low))
       AND IBS < ibs_threshold (default 0.3)
       AND (optionally) close > SMA(sma_trend_period)
Exit:  close > yesterday's high OR held > max_hold_days

IBS = (close - low) / (high - low)

Based on r/algotrading post (213 upvotes, full backtest):
- SPY 20y: 7.75% CAGR, 15.26% DD, 75% WR, 21% time in market
- QQQ 15y: 9.18% CAGR, 11.92% DD, 70.7% WR, 16.4% time in market
- Avg hold: 5.4 days (short exposure reduces crash risk)
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values


class ExtendedIbsSignalGenerator:
    """Extended IBS mean reversion with fast exit."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Extended IBS Mean Reversion Signal Generation ---")
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
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # IBS = (close - low) / (high - low)
        df_ind = df_ind.with_columns(
            pl.when((pl.col("high") - pl.col("low")).abs() < 1e-10)
            .then(0.5)
            .otherwise((pl.col("close") - pl.col("low")) / (pl.col("high") - pl.col("low")))
            .alias("ibs")
        )

        # 10-day rolling max high
        df_ind = df_ind.with_columns(
            pl.col("high")
            .rolling_max(window_size=10, min_samples=10)
            .over("instrument")
            .alias("high_10d")
        )

        # 25-day average high and low (average of daily highs/lows)
        df_ind = df_ind.with_columns([
            pl.col("high")
            .rolling_mean(window_size=25, min_samples=25)
            .over("instrument")
            .alias("avg_high_25d"),
            pl.col("low")
            .rolling_mean(window_size=25, min_samples=25)
            .over("instrument")
            .alias("avg_low_25d"),
        ])

        # Entry threshold: 10d_high - 2.5 * (25d_avg_high - 25d_avg_low)
        df_ind = df_ind.with_columns(
            (pl.col("high_10d") - 2.5 * (pl.col("avg_high_25d") - pl.col("avg_low_25d")))
            .alias("entry_threshold")
        )

        # Previous day's high (for exit condition)
        df_ind = df_ind.with_columns(
            pl.col("high").shift(1).over("instrument").alias("prev_high")
        )

        # VEI (Volatility Expansion Index) = ATR_short / ATR_long
        # True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
        df_ind = df_ind.with_columns(
            pl.col("close").shift(1).over("instrument").alias("_prev_close")
        )
        df_ind = df_ind.with_columns(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("_prev_close")).abs(),
                (pl.col("low") - pl.col("_prev_close")).abs(),
            ).alias("true_range")
        )
        df_ind = df_ind.with_columns([
            pl.col("true_range")
            .rolling_mean(window_size=5, min_samples=5)
            .over("instrument")
            .alias("atr_short"),
            pl.col("true_range")
            .rolling_mean(window_size=20, min_samples=20)
            .over("instrument")
            .alias("atr_long"),
        ])
        df_ind = df_ind.with_columns(
            (pl.col("atr_short") / pl.col("atr_long")).alias("vei")
        )
        df_ind = df_ind.drop(["_prev_close", "true_range"])

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            ibs_threshold = entry_config["ibs_threshold"]
            sma_trend_period = entry_config["sma_trend_period"]
            vei_max = entry_config.get("vei_max", 0)

            df_signals = df_ind.clone()

            # Optional SMA trend filter (0 = disabled)
            if sma_trend_period > 0:
                df_signals = df_signals.with_columns(
                    pl.col("close")
                    .rolling_mean(window_size=sma_trend_period, min_samples=sma_trend_period)
                    .over("instrument")
                    .alias("sma_trend")
                )
            else:
                df_signals = df_signals.with_columns(
                    pl.lit(0.0).alias("sma_trend")
                )

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry condition:
            # close < (10d_high - 2.5 * (25d_avg_high - 25d_avg_low))
            # AND IBS < threshold
            # AND close > SMA (if enabled)
            # AND scanner passed
            entry_filter = (
                (pl.col("close") < pl.col("entry_threshold"))
                & (pl.col("ibs") < ibs_threshold)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )
            if sma_trend_period > 0:
                entry_filter = entry_filter & (pl.col("close") > pl.col("sma_trend"))
            if vei_max > 0:
                entry_filter = entry_filter & (pl.col("vei") < vei_max)

            df_entries = df_signals.filter(entry_filter)

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                max_hold_days = exit_config["max_hold_days"]
                stop_loss_pct = exit_config.get("stop_loss_pct", 0)
                trailing_stop_pct = exit_config.get("trailing_stop_pct", 0)

                # Build per-instrument exit lookup
                exit_data = {}
                for inst_tuple, group in df_signals.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "prev_highs": g["prev_high"].to_list(),
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
                    highest_close = entry_price

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        prev_h = ed["prev_highs"][j]

                        if c is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Track highest close for trailing stop
                        if c > highest_close:
                            highest_close = c

                        # Fixed stop loss: exit if dropped too far from entry
                        if stop_loss_pct > 0 and c <= entry_price * (1 - stop_loss_pct):
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

                        # Trailing stop: exit if dropped too far from peak
                        if trailing_stop_pct > 0 and c <= highest_close * (1 - trailing_stop_pct):
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

                        # Normal exit: close > yesterday's high OR max hold days
                        if (prev_h is not None and c > prev_h) or hold_days >= max_hold_days:
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
            "ibs_threshold": entry_cfg.get("ibs_threshold", [0.3]),
            "sma_trend_period": entry_cfg.get("sma_trend_period", [0]),
            "vei_max": entry_cfg.get("vei_max", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "max_hold_days": exit_cfg.get("max_hold_days", [30]),
            "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [0]),
        }

register_strategy("extended_ibs", ExtendedIbsSignalGenerator)

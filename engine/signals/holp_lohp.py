"""HOLP/LOHP Reversal signal generator.

Entry (LOHP - long only): Stock makes new N-period low, then a subsequent
       bar closes above the high of that low bar (reversal confirmation).
Exit: Initial stop at low bar's low, then 2-bar trailing stop after day 3.

Based on John Carter "Mastering the Trade" Chapter 15.
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values


class HolpLohpSignalGenerator:
    """HOLP/LOHP reversal strategy (long only via LOHP)."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- HOLP/LOHP Signal Generation ---")
        t0 = time.time()

        # Phase 1: Scanner (liquidity filter)
        df = df_tick_data.clone()
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

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
        df = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
        df = df.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )

        signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
        uids = df["uid"].to_list()
        uid_to_signals = {}
        for uid in uids:
            signals = [str(k) for k, v in signal_sets.items() if uid in v]
            uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
        df = df.with_columns(
            pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
        )

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df.height} rows")

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            lookback_period = entry_config["lookback_period"]

            df_signals = df_ind.clone().sort(["instrument", "date_epoch"])

            # Rolling min low over lookback period
            df_signals = df_signals.with_columns(
                pl.col("low")
                .rolling_min(window_size=lookback_period, min_samples=lookback_period)
                .over("instrument")
                .alias("rolling_min_low")
            )

            # Identify "low bar": the bar where low == rolling_min_low (new N-period low)
            df_signals = df_signals.with_columns(
                (pl.col("low") == pl.col("rolling_min_low")).alias("is_low_bar")
            )

            # For LOHP: we need the high and low of the most recent low bar
            # We'll track this per instrument in the forward walk
            # For entry detection: find bars where close > high of the most recent low bar

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )

            scanner_ids_df = df.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Build per-instrument data for forward walk entry detection + exit
            inst_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                inst_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "opens": g["open"].to_list(),
                    "highs": g["high"].to_list(),
                    "lows": g["low"].to_list(),
                    "closes": g["close"].to_list(),
                    "volumes": g["volume"].to_list(),
                    "is_low_bar": g["is_low_bar"].to_list(),
                    "scanner_ids": g["scanner_config_ids"].to_list(),
                    "next_epochs": g["next_epoch"].to_list(),
                    "next_opens": g["next_open"].to_list(),
                    "next_volumes": g["next_volume"].to_list(),
                }

            for exit_config in get_exit_config_iterator(context):
                trailing_start_day = exit_config["trailing_start_day"]
                max_hold_days = exit_config["max_hold_days"]

                for inst_name, id_ in inst_data.items():
                    n = len(id_["epochs"])
                    last_low_bar_high = None
                    last_low_bar_low = None

                    i = 0
                    while i < n:
                        # Track most recent low bar
                        if id_["is_low_bar"][i]:
                            last_low_bar_high = id_["highs"][i]
                            last_low_bar_low = id_["lows"][i]

                        # Check LOHP entry: close > high of last low bar
                        if (last_low_bar_high is not None
                                and id_["closes"][i] is not None
                                and id_["closes"][i] > last_low_bar_high
                                and id_["scanner_ids"][i] is not None
                                and id_["next_epochs"][i] is not None
                                and id_["next_opens"][i] is not None):

                            entry_epoch = id_["next_epochs"][i]
                            entry_price = id_["next_opens"][i]
                            stop_price = last_low_bar_low

                            if entry_price is None or stop_price is None:
                                i += 1
                                continue

                            # Walk forward for exit
                            exit_epoch = None
                            exit_price = None

                            # Find entry index in full data
                            try:
                                entry_idx = id_["epochs"].index(entry_epoch)
                            except ValueError:
                                i += 1
                                continue

                            for j in range(entry_idx, n):
                                c = id_["closes"][j]
                                if c is None:
                                    continue

                                hold_days = (id_["epochs"][j] - entry_epoch) / 86400

                                # Days 1-2: only initial stop
                                if hold_days < trailing_start_day:
                                    if c <= stop_price:
                                        exit_epoch = id_["epochs"][j]
                                        exit_price = c
                                        break
                                else:
                                    # Day 3+: 2-bar trailing stop
                                    if j >= 2:
                                        low_1 = id_["lows"][j - 1]
                                        low_2 = id_["lows"][j - 2]
                                        if low_1 is not None and low_2 is not None:
                                            two_bar_stop = min(low_1, low_2)
                                            if two_bar_stop > stop_price:
                                                stop_price = two_bar_stop

                                    if c <= stop_price:
                                        exit_epoch = id_["epochs"][j]
                                        exit_price = c
                                        break

                                if hold_days >= max_hold_days:
                                    exit_epoch = id_["epochs"][j]
                                    exit_price = c
                                    break

                            if exit_epoch is None and n > entry_idx:
                                last_idx = n - 1
                                exit_epoch = id_["epochs"][last_idx]
                                exit_price = id_["closes"][last_idx]

                            if exit_epoch is not None and exit_price is not None:
                                all_order_rows.append({
                                    "instrument": inst_name,
                                    "entry_epoch": entry_epoch,
                                    "exit_epoch": exit_epoch,
                                    "entry_price": entry_price,
                                    "exit_price": exit_price,
                                    "entry_volume": id_["next_volumes"][i] or 0,
                                    "exit_volume": 0,
                                    "scanner_config_ids": id_["scanner_ids"][i],
                                    "entry_config_ids": str(entry_config["id"]),
                                    "exit_config_ids": str(exit_config["id"]),
                                })

                            # Skip forward past this trade and reset low bar refs
                            last_low_bar_high = None
                            last_low_bar_low = None
                            if exit_epoch is not None:
                                try:
                                    i = id_["epochs"].index(exit_epoch) + 1
                                except ValueError:
                                    i += 1
                            else:
                                i += 1
                            continue

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


register_strategy("holp_lohp", HolpLohpSignalGenerator)

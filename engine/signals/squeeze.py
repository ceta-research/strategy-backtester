"""The Squeeze signal generator.

Entry: Bollinger Bands squeeze inside Keltner Channels just released,
       with positive momentum (long only).
Exit: Momentum turns down, fixed stop, or max hold days.

Based on John Carter "Mastering the Trade" Chapter 10.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, run_scanner


class SqueezeSignalGenerator:
    """The Squeeze volatility expansion strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Squeeze Signal Generation ---")
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
            bb_period = entry_config["bb_period"]
            bb_std_mult = entry_config["bb_std"]
            kc_period = entry_config["kc_period"]
            kc_mult = entry_config["kc_mult"]
            mom_period = entry_config["mom_period"]

            df_signals = df_ind.clone().sort(["instrument", "date_epoch"])

            # Bollinger Bands
            df_signals = df_signals.with_columns([
                pl.col("close")
                .rolling_mean(window_size=bb_period, min_samples=bb_period)
                .over("instrument")
                .alias("bb_mid"),
                pl.col("close")
                .rolling_std(window_size=bb_period, min_samples=bb_period)
                .over("instrument")
                .alias("bb_std"),
            ])
            df_signals = df_signals.with_columns([
                (pl.col("bb_mid") + bb_std_mult * pl.col("bb_std")).alias("bb_upper"),
                (pl.col("bb_mid") - bb_std_mult * pl.col("bb_std")).alias("bb_lower"),
            ])

            # Keltner Channels: EMA + ATR
            df_signals = df_signals.with_columns(
                pl.col("close")
                .ewm_mean(span=kc_period, adjust=False, min_samples=kc_period)
                .over("instrument")
                .alias("kc_mid")
            )

            # True Range for ATR
            df_signals = df_signals.with_columns(
                pl.col("close").shift(1).over("instrument").alias("prev_close_tr")
            )
            df_signals = df_signals.with_columns(
                pl.max_horizontal(
                    pl.col("high") - pl.col("low"),
                    (pl.col("high") - pl.col("prev_close_tr")).abs(),
                    (pl.col("low") - pl.col("prev_close_tr")).abs(),
                ).alias("true_range")
            )
            df_signals = df_signals.with_columns(
                pl.col("true_range")
                .rolling_mean(window_size=kc_period, min_samples=kc_period)
                .over("instrument")
                .alias("atr")
            )
            df_signals = df_signals.with_columns([
                (pl.col("kc_mid") + kc_mult * pl.col("atr")).alias("kc_upper"),
                (pl.col("kc_mid") - kc_mult * pl.col("atr")).alias("kc_lower"),
            ])

            # Squeeze detection
            df_signals = df_signals.with_columns(
                ((pl.col("bb_upper") < pl.col("kc_upper")) & (pl.col("bb_lower") > pl.col("kc_lower")))
                .alias("squeeze_on")
            )

            # Momentum: close - close.shift(mom_period)
            df_signals = df_signals.with_columns(
                (pl.col("close") - pl.col("close").shift(mom_period).over("instrument"))
                .alias("momentum")
            )

            # Squeeze fired: was on yesterday, off today
            df_signals = df_signals.with_columns(
                (pl.col("squeeze_on").shift(1).over("instrument") & ~pl.col("squeeze_on"))
                .alias("squeeze_fired")
            )

            # Trim to simulation range and merge scanner signals
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )

            scanner_ids_df = df.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry: squeeze just fired + momentum > 0 + scanner passed
            df_entries = df_signals.filter(
                (pl.col("squeeze_fired") == True)
                & (pl.col("momentum") > 0)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                stop_loss_pct = exit_config["stop_loss_pct"]
                max_hold_days = exit_config["max_hold_days"]

                entry_rows = df_entries.select([
                    "instrument", "date_epoch", "next_epoch", "next_open", "next_volume",
                    "scanner_config_ids",
                ]).to_dicts()

                # Build per-instrument momentum data for exit
                mom_data = {}
                for inst_tuple, group in df_signals.filter(
                    pl.col("date_epoch") >= start_epoch
                ).group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    mom_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                        "momentums": g["momentum"].to_list(),
                    }

                # Also need raw epoch/close from full data for walking
                exit_data = {}
                for inst_tuple, group in df_ind.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                    }

                # Build momentum lookup per instrument: epoch -> momentum
                mom_lookup = {}
                for inst_name, md in mom_data.items():
                    lookup = {}
                    for k in range(len(md["epochs"])):
                        lookup[md["epochs"][k]] = md["momentums"][k]
                    mom_lookup[inst_name] = lookup

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data or inst not in mom_lookup:
                        continue

                    ed = exit_data[inst]
                    ml = mom_lookup[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    if entry_price is None:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    stop_price = entry_price * (1 - stop_loss_pct)
                    exit_epoch = None
                    exit_price = None
                    prev_mom = None

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        if c is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400
                        curr_mom = ml.get(ed["epochs"][j])

                        # Exit 1: Momentum turning down (was rising, now declining)
                        mom_exit = False
                        if curr_mom is not None and prev_mom is not None:
                            if curr_mom < prev_mom and prev_mom > 0:
                                mom_exit = True

                        # Exit 2: Stop loss
                        # Exit 3: Max hold days
                        if mom_exit or c <= stop_price or hold_days >= max_hold_days:
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

                        if curr_mom is not None:
                            prev_mom = curr_mom

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
            "bb_period": entry_cfg.get("bb_period", [20]),
            "bb_std": entry_cfg.get("bb_std", [2.0]),
            "kc_period": entry_cfg.get("kc_period", [20]),
            "kc_mult": entry_cfg.get("kc_mult", [1.5]),
            "mom_period": entry_cfg.get("mom_period", [12]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0.05]),
            "max_hold_days": exit_cfg.get("max_hold_days", [20]),
        }

register_strategy("squeeze", SqueezeSignalGenerator)

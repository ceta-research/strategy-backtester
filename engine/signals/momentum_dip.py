"""Momentum + RSI Oversold Dip-Buy signal generator.

Universe: Top N stocks by trailing return (momentum winners).
Entry: RSI(14) < rsi_threshold (oversold dip in a momentum winner)
Exit:  Profit target hit OR max hold days

Based on r/algotrading post (202 upvotes, live traded):
- Universe of top 135 stocks by 3/6/12 month performance
- RSI<30 on daily = buy the dip in winners
- 3% profit target, cut at 10 days
- Claimed 81.6% WR, 31% in 2 months live
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, compute_rsi, run_scanner


class MomentumDipSignalGenerator:
    """Buy oversold dips in momentum winners."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Momentum Dip-Buy Signal Generation ---")
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (liquidity filter)
        _, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # RSI(14)
        df_ind = df_ind.with_columns(
            compute_rsi(pl.col("close"), 14).over("instrument").alias("rsi_14")
        )

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            rsi_threshold = entry_config["rsi_threshold"]
            momentum_lookback_days = entry_config["momentum_lookback_days"]
            top_n = entry_config["top_n"]
            rerank_interval_days = entry_config.get("rerank_interval_days", 21)

            # Compute trailing return for momentum ranking
            lookback_bars = momentum_lookback_days  # approx 1 bar = 1 trading day
            df_signals = df_ind.clone().with_columns(
                (pl.col("close") / pl.col("close").shift(lookback_bars).over("instrument") - 1.0)
                .alias("momentum_return")
            )

            # Trim to simulation range and merge scanner
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Build per-epoch momentum ranking to identify top_n stocks
            # Group by date_epoch, rank by momentum_return, keep top_n
            epochs = sorted(df_signals["date_epoch"].unique().to_list())

            # Build momentum universe: re-rank every rerank_interval_days
            rerank_interval = rerank_interval_days * 86400
            momentum_universe = {}  # epoch -> set of top_n instruments
            last_rank_epoch = None

            for epoch in epochs:
                if last_rank_epoch is not None and (epoch - last_rank_epoch) < rerank_interval:
                    momentum_universe[epoch] = momentum_universe[last_rank_epoch]
                    continue

                day_data = df_signals.filter(
                    (pl.col("date_epoch") == epoch)
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("momentum_return").is_not_null())
                ).sort("momentum_return", descending=True)

                top_instruments = set(day_data["instrument"].head(top_n).to_list())
                momentum_universe[epoch] = top_instruments
                last_rank_epoch = epoch

            print(f"  Momentum universe built: {len(epochs)} epochs, "
                  f"rerank every {rerank_interval_days}d, top {top_n}")

            # Filter entries: RSI < threshold AND in momentum universe
            for exit_config in get_exit_config_iterator(context):
                profit_target_pct = exit_config["profit_target_pct"]
                max_hold_days = exit_config["max_hold_days"]

                # Build per-instrument exit data
                exit_data = {}
                for inst_tuple, group in df_signals.group_by("instrument"):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")
                    exit_data[inst_name] = {
                        "epochs": g["date_epoch"].to_list(),
                        "closes": g["close"].to_list(),
                    }

                # Walk through each day looking for entry signals
                entry_rows = df_signals.filter(
                    (pl.col("rsi_14") < rsi_threshold)
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("next_epoch").is_not_null())
                    & (pl.col("next_open").is_not_null())
                ).select([
                    "instrument", "date_epoch", "next_epoch", "next_open",
                    "next_volume", "scanner_config_ids",
                ]).to_dicts()

                for entry in entry_rows:
                    inst = entry["instrument"]
                    epoch = entry["date_epoch"]

                    # Check if this instrument is in the momentum universe
                    universe = momentum_universe.get(epoch, set())
                    if inst not in universe:
                        continue

                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    if entry_price is None or entry_price <= 0:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    target_price = entry_price * (1 + profit_target_pct)
                    exit_epoch = None
                    exit_price = None

                    for j in range(start_idx, len(ed["epochs"])):
                        c = ed["closes"][j]
                        if c is None:
                            continue

                        hold_days = (ed["epochs"][j] - entry_epoch) / 86400

                        # Exit: profit target OR max hold
                        if c >= target_price or hold_days >= max_hold_days:
                            exit_epoch = ed["epochs"][j]
                            exit_price = c
                            break

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
            "rsi_threshold": entry_cfg.get("rsi_threshold", [30]),
            "momentum_lookback_days": entry_cfg.get("momentum_lookback_days", [126]),
            "top_n": entry_cfg.get("top_n", [50]),
            "rerank_interval_days": entry_cfg.get("rerank_interval_days", [21]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "profit_target_pct": exit_cfg.get("profit_target_pct", [0.03]),
            "max_hold_days": exit_cfg.get("max_hold_days", [10]),
        }

register_strategy("momentum_dip", MomentumDipSignalGenerator)

"""Gap Fill mean reversion signal generator.

Entry: Stock gaps down at open by min_gap_down_pct to max_gap_down_pct vs prev close.
       Buy at open.
Exit:  Sell at close same day (captures intraday gap fill).

Literature: Small gaps (0-0.25%) fill 89-93% of the time. Sharpe ~1.3.
Gap fill works because overnight gaps overshoot due to low-liquidity pre-market.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, run_scanner


class GapFillSignalGenerator:
    """Gap fill mean reversion strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Gap Fill Signal Generation ---")
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner (liquidity filter)
        _, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute gap on full data
        df_ind = df_tick_data.clone()
        df_ind = df_ind.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("close").shift(1).over("instrument").alias("prev_close"),
        )

        # gap_pct = (open - prev_close) / prev_close  (negative = gap down)
        df_ind = df_ind.with_columns(
            ((pl.col("open") - pl.col("prev_close")) / pl.col("prev_close")).alias("gap_pct")
        )

        # Phase 3: Generate orders for each entry x exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            min_gap = entry_config["min_gap_down_pct"]
            max_gap = entry_config["max_gap_down_pct"]

            # Filter: gap down between -max_gap and -min_gap
            df_signals = df_ind.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            df_entries = df_signals.filter(
                (pl.col("gap_pct") < -min_gap)
                & (pl.col("gap_pct") > -max_gap)
                & (pl.col("prev_close").is_not_null())
                & (pl.col("scanner_config_ids").is_not_null())
            )

            if df_entries.is_empty():
                continue

            for exit_config in get_exit_config_iterator(context):
                # Gap fill: buy at open, sell at close same day
                entry_rows = df_entries.to_dicts()

                for row in entry_rows:
                    if row["open"] is None or row["close"] is None or row["open"] <= 0:
                        continue

                    all_order_rows.append({
                        "instrument": row["instrument"],
                        "entry_epoch": row["date_epoch"],
                        "exit_epoch": row["date_epoch"],    # same day
                        "entry_price": row["open"],          # buy at open
                        "exit_price": row["close"],          # sell at close
                        "entry_volume": row.get("volume", 0),
                        "exit_volume": row.get("volume", 0),
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

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "min_gap_down_pct": entry_cfg.get("min_gap_down_pct", [0.01]),
            "max_gap_down_pct": entry_cfg.get("max_gap_down_pct", [0.04]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "exit_at": exit_cfg.get("exit_at", ["close"]),
            "max_hold_days": exit_cfg.get("max_hold_days", [1]),
        }

register_strategy("gap_fill", GapFillSignalGenerator)

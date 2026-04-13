"""EOD Technical signal generator.

Wraps the existing scanner + order_generator logic as a SignalGenerator.
This is the original strategy: MA crossover + n-day high + direction score
entry, trailing stop-loss exit.
"""

import polars as pl

from engine import scanner, order_generator
from engine.signals.base import register_strategy


class EodTechnicalSignalGenerator:
    """Original ATO_Simulator-ported EOD technical strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        import time

        print("\n--- Scanner Step ---")
        t0 = time.time()
        df_scanned = scanner.process(context, df_tick_data)
        print(f"  Scanner: {round(time.time() - t0, 2)}s, {df_scanned.height} rows")

        print("\n--- Order Generation Step ---")
        t0 = time.time()
        df_orders = order_generator.process(context, df_scanned)
        print(f"  Order gen total: {round(time.time() - t0, 2)}s")

        return df_orders

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "n_day_ma": entry_cfg.get("n_day_ma", [3]),
            "n_day_high": entry_cfg.get("n_day_high", [2]),
            "direction_score": entry_cfg.get("direction_score", [
                {"n_day_ma": 3, "score": 0.54}
            ]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [15]),
        }

register_strategy("eod_technical", EodTechnicalSignalGenerator)

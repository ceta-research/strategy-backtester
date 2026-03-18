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


register_strategy("eod_technical", EodTechnicalSignalGenerator)

"""
Strategy Template - Extend Our Backtester
==========================================
Implement your custom signal generator by following this template.
Your strategy must implement the generate_orders() method that returns
entry/exit signals as a Polars DataFrame.

Usage:
  1. Copy this file to engine/signals/my_strategy.py
  2. Implement generate_orders()
  3. Register in your config YAML with strategy_type: my_strategy
  4. Run: python scripts/backtest_main.py strategies/my_strategy/config.yaml
"""

import polars as pl

from engine.signals.base import SignalGenerator, register_strategy


class MyStrategy(SignalGenerator):
    """
    Your custom strategy. Implement generate_orders() to define entry/exit logic.

    The pipeline calls this method for each instrument after fetching price data.
    You receive the full price history and must return a DataFrame of orders.
    """

    def generate_orders(
        self,
        context: dict,
        df_tick_data: pl.DataFrame,
    ) -> pl.DataFrame:
        """
        Generate entry/exit orders from price data.

        Args:
            context: Dict with keys:
                - entry_config: dict of your entry parameters
                - exit_config: dict of your exit parameters
                - scanner_config: dict with instrument info
                - simulation_config: dict with position sizing
                - static_config: dict with dates, capital, etc.

            df_tick_data: Polars DataFrame with columns:
                - epoch (i64): Unix timestamp in seconds
                - open, high, low, close (f64): OHLC prices
                - volume (i64): Trading volume
                - adj_close (f64): Split-adjusted close (if available)

        Returns:
            Polars DataFrame with columns:
                - instrument (str): Stock symbol
                - entry_epoch (i64): Unix timestamp for entry
                - exit_epoch (i64): Unix timestamp for exit
                - entry_price (f64): Entry price
                - exit_price (f64): Exit price
                - entry_volume (i64): Number of shares to buy
                - exit_volume (i64): Number of shares to sell
                - scanner_config_ids (str): Scanner config ID
                - entry_config_ids (str): Entry config ID
                - exit_config_ids (str): Exit config ID

            Return an empty DataFrame with the correct schema if no signals.
        """
        # Extract parameters from context
        entry = context["entry_config"]
        exit_cfg = context["exit_config"]
        instrument = context["scanner_config"]["instrument"]

        # Example: Simple SMA crossover
        sma_fast = entry.get("sma_fast", 10)
        sma_slow = entry.get("sma_slow", 50)
        stop_loss_pct = exit_cfg.get("stop_loss_pct", 0.05)

        # Compute indicators
        df = df_tick_data.with_columns([
            pl.col("close").rolling_mean(window_size=sma_fast).alias("sma_fast"),
            pl.col("close").rolling_mean(window_size=sma_slow).alias("sma_slow"),
        ]).drop_nulls()

        # Generate signals: buy when fast crosses above slow
        df = df.with_columns([
            (pl.col("sma_fast") > pl.col("sma_slow")).alias("signal"),
            (pl.col("sma_fast").shift(1) <= pl.col("sma_slow").shift(1)).alias("prev_no_signal"),
        ])

        # Find crossover points (signal transitions)
        entries = df.filter(pl.col("signal") & pl.col("prev_no_signal"))

        if entries.is_empty():
            return self._empty_orders()

        # Build orders (simplified - real implementation should pair entries with exits)
        orders = []
        for row in entries.iter_rows(named=True):
            entry_epoch = row["epoch"]
            entry_price = row["close"]

            # Find exit: next bar where fast crosses below slow, or stop loss
            exit_bars = df.filter(
                (pl.col("epoch") > entry_epoch)
                & (
                    (~pl.col("signal"))  # Signal reversal
                    | (pl.col("close") < entry_price * (1 - stop_loss_pct))  # Stop loss
                )
            )

            if exit_bars.is_empty():
                continue

            exit_row = exit_bars.row(0, named=True)
            orders.append({
                "instrument": instrument,
                "entry_epoch": entry_epoch,
                "exit_epoch": exit_row["epoch"],
                "entry_price": entry_price,
                "exit_price": exit_row["close"],
                "entry_volume": 1,  # Pipeline handles actual sizing
                "exit_volume": 1,
                "scanner_config_ids": "0",
                "entry_config_ids": "0",
                "exit_config_ids": "0",
            })

        if not orders:
            return self._empty_orders()

        return pl.DataFrame(orders)

    def _empty_orders(self) -> pl.DataFrame:
        """Return empty DataFrame with correct schema."""
        return pl.DataFrame(
            schema={
                "instrument": pl.Utf8,
                "entry_epoch": pl.Int64,
                "exit_epoch": pl.Int64,
                "entry_price": pl.Float64,
                "exit_price": pl.Float64,
                "entry_volume": pl.Int64,
                "exit_volume": pl.Int64,
                "scanner_config_ids": pl.Utf8,
                "entry_config_ids": pl.Utf8,
                "exit_config_ids": pl.Utf8,
            }
        )


# Register the strategy so the pipeline can find it
register_strategy("my_strategy", MyStrategy)

"""Pure Jegadeesh-Titman cross-sectional momentum rebalance.

Ports the standalone scripts/momentum_rebalance.py strategy to the engine pipeline.

Every N trading days:
  - Rank all scanner-eligible stocks by trailing momentum return
  - Buy top K stocks (equal weight)
  - Hold until next rebalance, then sell non-top-K and buy new top-K
  - Always fully invested

Each rebalance generates one order per stock in top-K:
  entry_epoch = rebalance_date
  exit_epoch = next_rebalance_date (or end of sim)
  entry_price = close on rebalance_date
  exit_price = close on next_rebalance_date

Key differences from dip-buy strategies:
  - No waiting for dips (always invested)
  - Periodic rebalance (not event-driven)
  - Higher turnover but captures momentum premium directly
"""

import time

import polars as pl

from engine.config_loader import (
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import register_strategy, run_scanner, finalize_orders, build_regime_filter


class MomentumRebalanceSignalGenerator:
    """Pure cross-sectional momentum with periodic rebalance.

    At each rebalance date (every N trading days):
    1. Rank all scanner-eligible stocks by trailing momentum return
    2. Select top K stocks
    3. Generate one order per stock: entry at rebalance close, exit at next rebalance close
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- Momentum Rebalance Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # Collect regime configs across all entry configs
        regime_configs = set()
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0:
                regime_configs.add((ri, rp))

        # Pre-build regime filters
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # -- Phase 1: Scanner (per-day liquidity filter) --
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # -- Phase 2: Compute momentum returns on full data --
        df_ind = df_tick_data.clone()
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # -- Phase 3: Generate orders per entry/exit config --
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            momentum_lookback = entry_config["momentum_lookback_days"]
            rebalance_interval = entry_config["rebalance_interval_days"]
            num_positions = entry_config["num_positions"]
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

            # Compute trailing momentum return
            df_mom = df_ind.with_columns(
                (
                    pl.col("close")
                    / pl.col("close").shift(momentum_lookback).over("instrument")
                    - 1.0
                ).alias("momentum_return")
            )

            # Trim to sim range and merge scanner IDs
            df_mom = df_mom.filter(pl.col("date_epoch") >= start_epoch)
            df_mom = df_mom.with_columns(
                (
                    pl.col("instrument").cast(pl.Utf8)
                    + pl.lit(":")
                    + pl.col("date_epoch").cast(pl.Utf8)
                ).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(
                ["uid", "scanner_config_ids"]
            ).unique(subset=["uid"])
            df_mom = df_mom.join(scanner_ids_df, on="uid", how="left")

            # Get all unique trading epochs in sim range
            epochs = sorted(df_mom["date_epoch"].unique().to_list())

            extras = [f"mom={momentum_lookback}d", f"rebal={rebalance_interval}d",
                      f"top={num_positions}"]
            if use_regime:
                extras.append(f"regime={regime_instrument}>SMA{regime_sma_period}")
            print(f"  Entry config: {', '.join(extras)}")

            # Identify rebalance dates (every N trading days)
            rebalance_epochs = []
            for i, epoch in enumerate(epochs):
                if i % rebalance_interval == 0:
                    rebalance_epochs.append(epoch)

            print(f"  Rebalance dates: {len(rebalance_epochs)} over {len(epochs)} trading days")

            # At each rebalance, rank stocks and generate orders
            for exit_config in get_exit_config_iterator(context):
                max_hold_days = exit_config.get("max_hold_days", 0)
                orders_this_config = 0

                for rb_idx, rb_epoch in enumerate(rebalance_epochs):
                    # Regime filter: skip rebalance if bearish
                    if use_regime and rb_epoch not in bull_epochs:
                        continue

                    # Determine exit epoch (next rebalance or end of data)
                    if rb_idx + 1 < len(rebalance_epochs):
                        next_rb_epoch = rebalance_epochs[rb_idx + 1]
                    else:
                        next_rb_epoch = epochs[-1] if epochs else rb_epoch

                    # If max_hold_days set and would hit before next rebalance,
                    # use max_hold_days instead
                    if max_hold_days > 0:
                        max_exit_epoch = rb_epoch + max_hold_days * 86400
                        if max_exit_epoch < next_rb_epoch:
                            # Find the nearest actual trading epoch
                            candidates = [e for e in epochs if e <= max_exit_epoch and e > rb_epoch]
                            if candidates:
                                next_rb_epoch = candidates[-1]

                    if next_rb_epoch <= rb_epoch:
                        continue

                    # Get scanner-eligible stocks with momentum at this epoch
                    day_data = df_mom.filter(
                        (pl.col("date_epoch") == rb_epoch)
                        & (pl.col("scanner_config_ids").is_not_null())
                        & (pl.col("momentum_return").is_not_null())
                        & (pl.col("close") > 0)
                    ).sort("momentum_return", descending=True)

                    if day_data.is_empty():
                        continue

                    # Select top K stocks
                    top_k = day_data.head(num_positions)

                    # Get exit-day prices for these instruments
                    exit_day = df_mom.filter(
                        pl.col("date_epoch") == next_rb_epoch
                    ).select(["instrument", "close"]).rename({"close": "exit_close"})

                    top_k_with_exit = top_k.join(exit_day, on="instrument", how="left")

                    for row in top_k_with_exit.to_dicts():
                        exit_price = row.get("exit_close")
                        if exit_price is None or exit_price <= 0:
                            continue
                        entry_price = row["close"]
                        if entry_price is None or entry_price <= 0:
                            continue

                        all_order_rows.append({
                            "instrument": row["instrument"],
                            "entry_epoch": rb_epoch,
                            "exit_epoch": next_rb_epoch,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "entry_volume": row.get("volume") or 0,
                            "exit_volume": 0,
                            "scanner_config_ids": row["scanner_config_ids"],
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })
                        orders_this_config += 1

                print(
                    f"    Exit max_hold={max_hold_days}d: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)


register_strategy("momentum_rebalance", MomentumRebalanceSignalGenerator)

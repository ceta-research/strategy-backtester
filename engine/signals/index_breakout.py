"""Index Breakout signal generator.

Entry: Close >= N-day high -> buy at next open (MOC execution).
Exit:  Trailing stop-loss (position drops X% from peak) or max hold days.

Designed for single-instrument index ETFs (NIFTYBEES, SPY, QQQ).
Momentum/trend-following on index: ride breakouts, cut losers with TSL.

Ported from scripts/buy_2day_high.py to the engine pipeline.
"""

import time

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, run_scanner, finalize_orders, build_regime_filter


class IndexBreakoutSignalGenerator:
    """Buy N-day high breakouts on index/ETF with trailing stop-loss exit."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Index Breakout Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Check if any entry config needs regime filter
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

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute next-day values on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            lookback_days = entry_config["lookback_days"]
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            # Get regime filter
            bull_epochs = regime_cache.get((regime_instrument, regime_sma_period), set())
            use_regime = bool(bull_epochs)

            # Compute rolling N-day high (over the previous lookback_days closes, NOT including current bar)
            df_signals = df_ind.clone().with_columns(
                pl.col("close").shift(1)
                .rolling_max(window_size=lookback_days, min_samples=lookback_days)
                .over("instrument")
                .alias("n_day_high")
            )

            # Trim to simulation range and merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]

                for inst_tuple, group in df_signals.group_by("instrument", maintain_order=True):
                    inst_name = inst_tuple[0]
                    g = group.sort("date_epoch")

                    epochs = g["date_epoch"].to_list()
                    closes = g["close"].to_list()
                    n_day_highs = g["n_day_high"].to_list()
                    next_epochs = g["next_epoch"].to_list()
                    next_opens = g["next_open"].to_list()
                    next_volumes = g["next_volume"].to_list()
                    scanner_ids = g["scanner_config_ids"].to_list()

                    i = 0
                    while i < len(epochs):
                        c = closes[i]
                        ndh = n_day_highs[i]

                        if c is None or ndh is None:
                            i += 1
                            continue
                        if scanner_ids[i] is None:
                            i += 1
                            continue
                        if next_epochs[i] is None or next_opens[i] is None:
                            i += 1
                            continue

                        # Regime filter
                        if use_regime and epochs[i] not in bull_epochs:
                            i += 1
                            continue

                        # Entry: close >= N-day high (breakout)
                        if c < ndh:
                            i += 1
                            continue

                        entry_epoch = next_epochs[i]
                        entry_price = next_opens[i]
                        entry_volume = next_volumes[i] or 0

                        if entry_price is None or entry_price <= 0:
                            i += 1
                            continue

                        # Forward iterate to find exit
                        exit_epoch = None
                        exit_price = None
                        trail_high = entry_price

                        for j in range(i + 1, len(epochs)):
                            cj = closes[j]
                            if cj is None:
                                continue

                            # Track trailing high
                            if cj > trail_high:
                                trail_high = cj

                            # Max hold days
                            if max_hold_days > 0:
                                hold_days = (epochs[j] - entry_epoch) / 86400
                                if hold_days >= max_hold_days:
                                    exit_epoch = epochs[j]
                                    exit_price = cj
                                    break

                            # Trailing stop-loss: position drops X% from peak
                            if trailing_stop_pct > 0:
                                dd_pct = (trail_high - cj) / trail_high
                                if dd_pct >= trailing_stop_pct:
                                    exit_epoch = epochs[j]
                                    exit_price = cj
                                    break

                        # If no exit found, close at last bar
                        if exit_epoch is None and len(epochs) > i + 1:
                            exit_epoch = epochs[-1]
                            exit_price = closes[-1]

                        if exit_epoch is None or exit_price is None:
                            i += 1
                            continue

                        all_order_rows.append({
                            "instrument": inst_name,
                            "entry_epoch": entry_epoch,
                            "exit_epoch": exit_epoch,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "entry_volume": entry_volume,
                            "exit_volume": 0,
                            "scanner_config_ids": scanner_ids[i],
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

                        # Skip to exit bar (no re-entry while in position)
                        try:
                            exit_idx = epochs.index(exit_epoch, i + 1)
                            i = exit_idx + 1
                        except ValueError:
                            i += 1

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "lookback_days": entry_cfg.get("lookback_days", [3]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [5]),
            "max_hold_days": exit_cfg.get("max_hold_days", [0]),
        }

register_strategy("index_breakout", IndexBreakoutSignalGenerator)

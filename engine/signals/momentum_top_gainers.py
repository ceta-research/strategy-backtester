"""Momentum Top Gainers: buy trailing-period winners with TSL exits.

Periodically ranks all liquid stocks by trailing return (6/9/12 months),
buys the top N%, and exits via trailing stop-loss. Direction score filter
(market breadth) gates all entries.

Key differences from related strategies:
- momentum_rebalance: exits at next rebalance (fixed hold). We use TSL.
- momentum_dip_quality: waits for dips to enter. We buy strength at rebalance.
- momentum_cascade: requires accelerating momentum. We just rank by return.

Parameters:
  momentum_lookback_days: trailing return period (126/189/252 = 6/9/12 months)
  top_n_pct: fraction of universe to buy (0.10 = top 10%)
  rebalance_interval_days: how often to re-rank (trading days)
  direction_score_n_day_ma: MA period for breadth calculation
  direction_score_threshold: min fraction of stocks above their MA to allow entry
  min_momentum_pct: minimum trailing return % to qualify (avoid buying negative momentum)
  regime_instrument / regime_sma_period: optional benchmark regime filter
  tsl_pct: trailing stop-loss percentage
  max_hold_days: maximum holding period
"""

import time

import polars as pl

from engine.config_loader import (
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import (
    register_strategy,
    add_next_day_values,
    run_scanner,
    walk_forward_exit,
    finalize_orders,
    build_regime_filter,
    compute_direction_score,
)


class MomentumTopGainersSignalGenerator:
    """Buy trailing-period top gainers, exit via TSL.

    At each rebalance date:
    1. Rank all scanner-eligible stocks by trailing momentum return
    2. Select top N% with positive momentum above min threshold
    3. Gate by direction score (market breadth)
    4. Gate by regime filter (optional)
    5. Enter at next-day open, exit via walk_forward_exit (TSL)
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- Momentum Top Gainers Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # Collect what features are needed across all entry configs
        regime_configs = set()
        direction_ma_periods = set()
        exchanges = set()

        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0:
                regime_configs.add((ri, rp))
            ds_ma = entry_config.get("direction_score_n_day_ma", 0)
            if ds_ma > 0:
                direction_ma_periods.add(ds_ma)

        for scanner_config in get_scanner_config_iterator(context):
            for inst in scanner_config["instruments"]:
                exchanges.add(inst["exchange"])

        # Pre-build regime filters
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # Pre-compute direction scores
        direction_score_cache = {}
        for ds_ma in direction_ma_periods:
            direction_score_cache[ds_ma] = compute_direction_score(
                df_tick_data, n_day_ma=ds_ma
            )
            scores = list(direction_score_cache[ds_ma].values())
            avg_score = sum(scores) / len(scores) if scores else 0
            print(f"  Direction score ({ds_ma}d MA): avg {avg_score:.2f}")

        # Phase 1: Scanner (per-day liquidity filter)
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute indicators on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Period-average turnover filter (fixed universe, matches standalone approach)
        _turnover_threshold = 70_000_000
        for scanner_cfg in get_scanner_config_iterator(context):
            thresh_val = scanner_cfg.get("avg_day_transaction_threshold")
            if isinstance(thresh_val, dict):
                _turnover_threshold = thresh_val.get("threshold", _turnover_threshold)
            elif isinstance(thresh_val, (int, float)):
                _turnover_threshold = thresh_val
            break

        period_avg = (
            df_ind
            .group_by("instrument")
            .agg(
                (pl.col("close") * pl.col("volume")).mean().alias("avg_turnover"),
                pl.col("close").mean().alias("avg_close"),
            )
            .filter(
                (pl.col("avg_turnover") > _turnover_threshold)
                & (pl.col("avg_close") > 50)
            )
        )
        period_universe_set = set(period_avg["instrument"].to_list())
        print(f"  Period-avg turnover filter: {len(period_universe_set)} instruments")

        # Phase 3: Generate orders per entry/exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            momentum_lookback = entry_config["momentum_lookback_days"]
            top_n_pct = entry_config["top_n_pct"]
            rebalance_days = entry_config["rebalance_interval_days"]
            min_momentum = entry_config.get("min_momentum_pct", 0) / 100.0
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)
            direction_n_day_ma = entry_config.get("direction_score_n_day_ma", 0)
            direction_threshold = entry_config.get("direction_score_threshold", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

            direction_scores = direction_score_cache.get(direction_n_day_ma, {})
            use_direction = bool(direction_scores) and direction_threshold > 0

            # Compute trailing momentum return
            df_signals = df_ind.with_columns(
                (
                    pl.col("close")
                    / pl.col("close").shift(momentum_lookback).over("instrument")
                    - 1.0
                ).alias("momentum_return")
            )

            # Trim to sim range, merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (
                    pl.col("instrument").cast(pl.Utf8)
                    + pl.lit(":")
                    + pl.col("date_epoch").cast(pl.Utf8)
                ).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(
                ["uid", "scanner_config_ids"]
            ).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Get all unique trading epochs
            epochs = sorted(df_signals["date_epoch"].unique().to_list())

            # Identify rebalance dates
            rebalance_epochs = [epochs[i] for i in range(0, len(epochs), rebalance_days)]

            extras = [
                f"mom={momentum_lookback}d",
                f"top={top_n_pct * 100:.0f}%",
                f"rebal={rebalance_days}d",
            ]
            if min_momentum > 0:
                extras.append(f"min_mom={min_momentum * 100:.0f}%")
            if use_regime:
                extras.append(f"regime={regime_instrument}>SMA{regime_sma_period}")
            if use_direction:
                extras.append(f"direction>{direction_threshold}({direction_n_day_ma}d)")
            print(f"  Entry config: {', '.join(extras)}, {len(rebalance_epochs)} rebalances")

            # Build per-instrument exit data for walk-forward
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                    "opens": g["open"].to_list(),
                }

            # At each rebalance, rank and generate orders
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                orders_this_config = 0

                for rb_epoch in rebalance_epochs:
                    # Regime filter
                    if use_regime and rb_epoch not in bull_epochs:
                        continue

                    # Direction score gate
                    if use_direction:
                        ds = direction_scores.get(rb_epoch, 0)
                        if ds <= direction_threshold:
                            continue

                    # Get scanner-eligible stocks with momentum at this epoch
                    day_data = df_signals.filter(
                        (pl.col("date_epoch") == rb_epoch)
                        & (pl.col("instrument").is_in(list(period_universe_set)))
                        & (pl.col("momentum_return").is_not_null())
                        & (pl.col("close") > 0)
                        & (pl.col("next_epoch").is_not_null())
                        & (pl.col("next_open").is_not_null())
                    )

                    if day_data.is_empty():
                        continue

                    # Filter by minimum momentum threshold
                    if min_momentum > 0:
                        day_data = day_data.filter(
                            pl.col("momentum_return") >= min_momentum
                        )

                    if day_data.is_empty():
                        continue

                    # Rank by momentum (descending) and pick top N%
                    day_data = day_data.sort("momentum_return", descending=True)
                    total_stocks = day_data.height
                    top_n = max(1, int(total_stocks * top_n_pct))
                    top_gainers = day_data.head(top_n).to_dicts()

                    for row in top_gainers:
                        inst = row["instrument"]
                        if inst not in exit_data:
                            continue

                        ed = exit_data[inst]
                        entry_epoch = row["next_epoch"]
                        entry_price = row["next_open"]

                        if entry_price is None or entry_price <= 0:
                            continue

                        # For top-gainers (buying strength), TSL is active
                        # immediately (no peak recovery wait - we're at the top)
                        peak_price = entry_price

                        try:
                            start_idx = ed["epochs"].index(entry_epoch)
                        except ValueError:
                            continue

                        exit_epoch, exit_price = walk_forward_exit(
                            ed["epochs"], ed["closes"], start_idx,
                            entry_epoch, entry_price, peak_price,
                            tsl_pct, max_hold_days,
                            opens=ed["opens"],
                            require_peak_recovery=False,  # TSL active immediately
                        )

                        if exit_epoch is None or exit_price is None:
                            continue

                        all_order_rows.append({
                            "instrument": inst,
                            "entry_epoch": entry_epoch,
                            "exit_epoch": exit_epoch,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "entry_volume": row.get("next_volume") or 0,
                            "exit_volume": 0,
                            "scanner_config_ids": row.get("scanner_config_ids") or "1",
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })
                        orders_this_config += 1

                print(
                    f"    Exit TSL={tsl_pct * 100:.0f}% "
                    f"hold={max_hold_days}d: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "momentum_lookback_days": entry_cfg.get("momentum_lookback_days", [252]),
            "top_n_pct": entry_cfg.get("top_n_pct", [0.20]),
            "rebalance_interval_days": entry_cfg.get("rebalance_interval_days", [21]),
            "min_momentum_pct": entry_cfg.get("min_momentum_pct", [0]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
            "direction_score_n_day_ma": entry_cfg.get("direction_score_n_day_ma", [0]),
            "direction_score_threshold": entry_cfg.get("direction_score_threshold", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [15]),
            "max_hold_days": exit_cfg.get("max_hold_days", [252]),
        }

register_strategy("momentum_top_gainers", MomentumTopGainersSignalGenerator)

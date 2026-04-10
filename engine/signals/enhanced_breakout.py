"""Enhanced Breakout: Multi-layer confirmed breakout strategy.

Combines the best elements from eod_technical, momentum_dip_quality,
and momentum_cascade into a single high-conviction breakout strategy.

Entry layers (ALL must pass):
1. Breakout: close >= previous N-day high (from eod_technical)
2. Quality: N consecutive years of positive returns (from momentum_dip_quality)
3. Momentum: stock in top N% by trailing return (from momentum_dip_quality)
4. Volume: today's volume > K x 20-day average (new - reduces false breakouts)
5. Fundamentals: ROE, D/E, P/E filters (optional, from standalone champion)
6. Regime: benchmark > SMA (optional, from momentum_dip_quality)

Exit: Trailing stop-loss from entry (no peak recovery wait), or max hold days.
Unlike dip-buy strategies, breakout entries are AT the peak so TSL activates
immediately - we ride momentum and protect gains.

Designed for concentration (5-15 positions) with optional leverage via
order_value_multiplier in simulation config.

Target: 20% CAGR by combining signal quality + concentration + regime leverage.
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
    fetch_fundamentals,
    passes_fundamental_filter,
    build_regime_filter,
)

TRADING_DAYS_PER_YEAR = 252


class EnhancedBreakoutSignalGenerator:
    """Multi-layer confirmed breakout with quality + momentum + volume gates.

    Key differences from eod_technical:
    - Quality universe filter (2yr+ consecutive positive returns)
    - Momentum ranking filter (top N% by trailing return)
    - Volume confirmation (volume > K x average)
    - Optional fundamental overlay (ROE, D/E, P/E)
    - Optional regime filter
    - Designed for concentrated portfolios (5-15 positions)

    Key difference from momentum_dip_quality:
    - Entry on BREAKOUT (strength), not DIP (weakness)
    - TSL activates immediately (no peak recovery wait)
    - Higher signal frequency than dip-buy
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- Enhanced Breakout Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # Determine feature requirements across all entry configs
        needs_fundamentals = False
        regime_configs = set()
        exchanges = set()

        for entry_config in get_entry_config_iterator(context):
            if (
                entry_config.get("roe_threshold", 0) > 0
                or entry_config.get("pe_threshold", 0) > 0
                or entry_config.get("de_threshold", 0) > 0
            ):
                needs_fundamentals = True
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0:
                regime_configs.add((ri, rp))

        for scanner_config in get_scanner_config_iterator(context):
            for inst in scanner_config["instruments"]:
                exchanges.add(inst["exchange"])

        # Fetch fundamentals if any config needs them
        fundamentals = {}
        if needs_fundamentals:
            fundamentals = fetch_fundamentals(list(exchanges))

        # Pre-build regime filters
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # -- Phase 1: Scanner (per-day liquidity filter) --
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # -- Phase 2: Compute indicators --
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # -- Phase 3: Generate orders per entry/exit config --
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            breakout_window = entry_config["breakout_window"]
            consecutive_years = entry_config["consecutive_positive_years"]
            min_yearly_return = entry_config.get("min_yearly_return_pct", 0) / 100.0
            momentum_lookback = entry_config["momentum_lookback_days"]
            momentum_percentile = entry_config["momentum_percentile"]
            rerank_days = entry_config.get("rerank_interval_days", 63)
            rescreen_days = entry_config["rescreen_interval_days"]
            volume_multiplier = entry_config.get("volume_multiplier", 0)
            volume_avg_period = entry_config.get("volume_avg_period", 20)
            roe_threshold = entry_config.get("roe_threshold", 0)
            pe_threshold = entry_config.get("pe_threshold", 0)
            de_threshold = entry_config.get("de_threshold", 0)
            fundamental_missing = entry_config.get(
                "fundamental_missing_mode", "skip"
            )
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

            df_signals = df_ind.clone()

            # Breakout: previous N-day high (exclude today to avoid look-ahead)
            # shift(1) gets yesterday's close, then rolling_max over N days
            df_signals = df_signals.with_columns(
                pl.col("close")
                .shift(1)
                .rolling_max(
                    window_size=breakout_window,
                    min_samples=breakout_window,
                )
                .over("instrument")
                .alias("prev_n_day_high")
            )

            # Breakout signal: today's close >= highest close of previous N days
            df_signals = df_signals.with_columns(
                (
                    (pl.col("close") >= pl.col("prev_n_day_high"))
                    & pl.col("prev_n_day_high").is_not_null()
                ).alias("is_breakout")
            )

            # Volume confirmation: today's volume > K x avg of previous days
            if volume_multiplier > 0:
                df_signals = df_signals.with_columns(
                    pl.col("volume")
                    .shift(1)
                    .rolling_mean(
                        window_size=volume_avg_period,
                        min_samples=volume_avg_period,
                    )
                    .over("instrument")
                    .alias("prev_avg_volume")
                )
                df_signals = df_signals.with_columns(
                    (
                        pl.col("volume")
                        > pl.col("prev_avg_volume") * volume_multiplier
                    ).alias("volume_confirm")
                )
            else:
                df_signals = df_signals.with_columns(
                    pl.lit(True).alias("volume_confirm")
                )

            # Quality filter: trailing yearly returns
            yearly_return_cols = []
            for yr in range(consecutive_years):
                shift_recent = yr * TRADING_DAYS_PER_YEAR
                shift_older = (yr + 1) * TRADING_DAYS_PER_YEAR
                col_name = f"yr_return_{yr + 1}"
                df_signals = df_signals.with_columns(
                    (
                        pl.col("close").shift(shift_recent).over("instrument")
                        / pl.col("close")
                        .shift(shift_older)
                        .over("instrument")
                        - 1.0
                    ).alias(col_name)
                )
                yearly_return_cols.append(col_name)

            quality_expr = pl.lit(True)
            for col_name in yearly_return_cols:
                quality_expr = quality_expr & (
                    pl.col(col_name) > min_yearly_return
                )
            df_signals = df_signals.with_columns(
                quality_expr.alias("is_quality")
            )

            # Momentum ranking (trailing return)
            df_signals = df_signals.with_columns(
                (
                    pl.col("close")
                    / pl.col("close")
                    .shift(momentum_lookback)
                    .over("instrument")
                    - 1.0
                ).alias("momentum_return")
            )

            # Trim to sim range, merge scanner IDs
            df_signals = df_signals.filter(
                pl.col("date_epoch") >= start_epoch
            )
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
            df_signals = df_signals.join(
                scanner_ids_df, on="uid", how="left"
            )

            # Build quality universe (re-screen periodically)
            epochs = sorted(
                df_signals["date_epoch"].unique().to_list()
            )
            rescreen_interval = rescreen_days * 86400
            rerank_interval = rerank_days * 86400

            quality_universe = {}
            last_screen_epoch = None
            for epoch in epochs:
                if (
                    last_screen_epoch is not None
                    and (epoch - last_screen_epoch) < rescreen_interval
                ):
                    quality_universe[epoch] = quality_universe[
                        last_screen_epoch
                    ]
                    continue
                day_data = df_signals.filter(
                    (pl.col("date_epoch") == epoch)
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("is_quality") == True)  # noqa: E712
                )
                quality_universe[epoch] = set(
                    day_data["instrument"].to_list()
                )
                last_screen_epoch = epoch

            # Build momentum universe (top N% by trailing return)
            momentum_universe = {}
            last_rank_epoch = None
            for epoch in epochs:
                if (
                    last_rank_epoch is not None
                    and (epoch - last_rank_epoch) < rerank_interval
                ):
                    momentum_universe[epoch] = momentum_universe[
                        last_rank_epoch
                    ]
                    continue
                day_data = (
                    df_signals.filter(
                        (pl.col("date_epoch") == epoch)
                        & (pl.col("scanner_config_ids").is_not_null())
                        & (pl.col("momentum_return").is_not_null())
                    ).sort("momentum_return", descending=True)
                )
                total_stocks = day_data.height
                top_n = max(1, int(total_stocks * momentum_percentile))
                momentum_universe[epoch] = set(
                    day_data["instrument"].head(top_n).to_list()
                )
                last_rank_epoch = epoch

            # Intersect quality and momentum universes
            combined_universe = {}
            for epoch in epochs:
                q = quality_universe.get(epoch, set())
                m = momentum_universe.get(epoch, set())
                intersection = q & m
                if intersection:
                    combined_universe[epoch] = intersection

            pool_sizes = [
                len(v) for v in combined_universe.values() if v
            ]
            avg_pool = (
                sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
            )

            extras = [
                f"breakout={breakout_window}d",
                f"quality={consecutive_years}yr",
                f"mom={momentum_lookback}d top{momentum_percentile * 100:.0f}%",
            ]
            if volume_multiplier > 0:
                extras.append(f"vol>{volume_multiplier}x")
            if roe_threshold > 0:
                extras.append(f"ROE>{roe_threshold}%")
            if de_threshold > 0:
                extras.append(f"D/E<{de_threshold}")
            if pe_threshold > 0:
                extras.append(f"PE<{pe_threshold}")
            if use_regime:
                extras.append(
                    f"regime={regime_instrument}>SMA{regime_sma_period}"
                )
            print(
                f"  Universe: avg {avg_pool:.0f} stocks "
                f"({', '.join(extras)})"
            )

            # Build per-instrument exit data for walk-forward
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                }

            # Entry filter: breakout + quality + volume + scanner + regime
            entry_filter = (
                (pl.col("is_breakout") == True)  # noqa: E712
                & (pl.col("volume_confirm") == True)  # noqa: E712
                & (pl.col("is_quality") == True)  # noqa: E712
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("prev_n_day_high").is_not_null())
            )
            if use_regime:
                entry_filter = entry_filter & (
                    pl.col("date_epoch").is_in(list(bull_epochs))
                )

            entry_rows = (
                df_signals.filter(entry_filter)
                .select([
                    "instrument",
                    "date_epoch",
                    "next_epoch",
                    "next_open",
                    "next_volume",
                    "scanner_config_ids",
                    "prev_n_day_high",
                ])
                .to_dicts()
            )

            print(
                f"  Entry candidates (breakout+quality+vol): "
                f"{len(entry_rows)}"
            )

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                orders_this_config = 0

                for entry in entry_rows:
                    inst = entry["instrument"]
                    epoch = entry["date_epoch"]

                    # Must be in combined (quality AND momentum) universe
                    universe = combined_universe.get(epoch, set())
                    if inst not in universe:
                        continue

                    # Fundamental filter
                    if fundamentals and (
                        roe_threshold > 0
                        or pe_threshold > 0
                        or de_threshold > 0
                    ):
                        symbol = inst.split(":")[1]
                        if not passes_fundamental_filter(
                            fundamentals,
                            symbol,
                            epoch,
                            roe_threshold,
                            pe_threshold,
                            de_threshold,
                            fundamental_missing,
                        ):
                            continue

                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    if entry_price is None or entry_price <= 0:
                        continue

                    # For breakout: entry IS at the peak (by definition).
                    # Set peak_price = entry_price so TSL activates immediately.
                    # walk_forward_exit will set reached_peak=True on first bar
                    # where close >= entry_price, then trail from there.
                    peak_price = entry_price

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch, exit_price = walk_forward_exit(
                        ed["epochs"],
                        ed["closes"],
                        start_idx,
                        entry_epoch,
                        entry_price,
                        peak_price,
                        tsl_pct,
                        max_hold_days,
                    )

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
                        "scanner_config_ids": entry[
                            "scanner_config_ids"
                        ],
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
            "breakout_window": entry_cfg.get("breakout_window", [2]),
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [2]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "momentum_lookback_days": entry_cfg.get("momentum_lookback_days", [63]),
            "momentum_percentile": entry_cfg.get("momentum_percentile", [0.30]),
            "rerank_interval_days": entry_cfg.get("rerank_interval_days", [63]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
            "volume_multiplier": entry_cfg.get("volume_multiplier", [0]),
            "volume_avg_period": entry_cfg.get("volume_avg_period", [20]),
            "roe_threshold": entry_cfg.get("roe_threshold", [0]),
            "pe_threshold": entry_cfg.get("pe_threshold", [0]),
            "de_threshold": entry_cfg.get("de_threshold", [0]),
            "fundamental_missing_mode": entry_cfg.get("fundamental_missing_mode", ["skip"]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [12]),
            "max_hold_days": exit_cfg.get("max_hold_days", [252]),
        }

register_strategy("enhanced_breakout", EnhancedBreakoutSignalGenerator)

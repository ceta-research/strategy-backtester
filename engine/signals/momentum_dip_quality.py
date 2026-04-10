"""Momentum + Quality Dip-Buy with Fundamental Filters.

Ports the standalone champion strategies to the engine pipeline:
- momentum_dip_buy.py (Calmar 1.01 standalone)
- momentum_dip_de_positions.py (D/E + sector limits, Calmar 0.92 standalone)
- quality_dip_buy_fundamental.py (quality + fundamentals, Calmar 0.64 standalone)

Universe: Quality (N consecutive years positive returns) AND Momentum (top N% trailing return)
Entry: Price dips X% from rolling peak in universe stock
Exit: Peak recovery + trailing stop-loss, or max hold days
Filters: ROE, P/E, D/E (optional, with 45-day filing lag)
Regime: Benchmark > SMA (optional)

Key advantage over standalone: per-day liquidity filter (scanner) ensures
entries only happen on days with sufficient turnover for position sizes.
Standalone results are upper bounds; engine results are honest.
"""

import time

import polars as pl

from engine.config_loader import (
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import (
    register_strategy, add_next_day_values, run_scanner, walk_forward_exit, finalize_orders,
    build_regime_filter, fetch_fundamentals, passes_fundamental_filter,
    compute_direction_score,
)

TRADING_DAYS_PER_YEAR = 252


class MomentumDipQualitySignalGenerator:
    """Momentum + Quality Dip-Buy with optional fundamental filters.

    Covers the parameter space of:
    - momentum_dip_buy.py (champion, Calmar 1.01)
    - momentum_dip_de_positions.py (D/E + sector limits, Calmar 0.92)
    - quality_dip_buy_fundamental.py (quality + fundamentals, Calmar 0.64)
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- Momentum Dip Quality Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # Determine what features are needed across all entry configs
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

        # Pre-compute direction score (market breadth) if any config uses it
        direction_score_cache = {}
        direction_ma_periods = set()
        for entry_config in get_entry_config_iterator(context):
            ds_ma = entry_config.get("direction_score_n_day_ma", 0)
            if ds_ma > 0:
                direction_ma_periods.add(ds_ma)
        for ds_ma in direction_ma_periods:
            direction_score_cache[ds_ma] = compute_direction_score(
                df_tick_data, n_day_ma=ds_ma
            )
            scores = list(direction_score_cache[ds_ma].values())
            avg_score = sum(scores) / len(scores) if scores else 0
            print(f"  Direction score ({ds_ma}d MA): avg {avg_score:.2f}")

        # ── Phase 1: Scanner (per-day liquidity filter) ──
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # ── Phase 2: Compute indicators ──
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # ── Phase 3: Generate orders per entry/exit config ──
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            consecutive_years = entry_config["consecutive_positive_years"]
            min_yearly_return = entry_config["min_yearly_return_pct"] / 100.0
            momentum_lookback = entry_config["momentum_lookback_days"]
            momentum_percentile = entry_config["momentum_percentile"]
            rerank_days = entry_config.get("rerank_interval_days", 63)
            dip_threshold = entry_config["dip_threshold_pct"] / 100.0
            peak_lookback = entry_config["peak_lookback_days"]
            rescreen_days = entry_config["rescreen_interval_days"]
            roe_threshold = entry_config.get("roe_threshold", 0)
            pe_threshold = entry_config.get("pe_threshold", 0)
            de_threshold = entry_config.get("de_threshold", 0)
            fundamental_missing = entry_config.get(
                "fundamental_missing_mode", "skip"
            )
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)
            direction_n_day_ma = entry_config.get("direction_score_n_day_ma", 0)
            direction_threshold = entry_config.get("direction_score_threshold", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

            # Direction score (market breadth) gate
            direction_scores = direction_score_cache.get(direction_n_day_ma, {})
            use_direction = bool(direction_scores) and direction_threshold > 0

            df_signals = df_ind.clone()

            # Rolling peak (highest close in last peak_lookback days)
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(
                    window_size=peak_lookback, min_samples=peak_lookback
                )
                .over("instrument")
                .alias("rolling_peak")
            )

            # Dip percentage from peak
            df_signals = df_signals.with_columns(
                (
                    (pl.col("rolling_peak") - pl.col("close"))
                    / pl.col("rolling_peak")
                ).alias("dip_pct")
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
                quality_expr = quality_expr & (pl.col(col_name) > min_yearly_return)
            df_signals = df_signals.with_columns(
                quality_expr.alias("is_quality")
            )

            # Momentum ranking (trailing return)
            df_signals = df_signals.with_columns(
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

            # Period-average turnover filter: compute avg(close * volume) per
            # instrument across the ENTIRE sim range, keep only those above the
            # scanner threshold.  This produces a FIXED set of instruments
            # (matches standalone's fetch_universe SQL approach).
            _turnover_threshold = 70_000_000  # default NSE threshold
            for scanner_cfg in get_scanner_config_iterator(context):
                thresh_val = scanner_cfg.get("avg_day_transaction_threshold")
                if isinstance(thresh_val, dict):
                    _turnover_threshold = thresh_val.get("threshold", _turnover_threshold)
                elif isinstance(thresh_val, (int, float)):
                    _turnover_threshold = thresh_val
                break

            period_avg = (
                df_ind  # Use full data range (incl. prefetch) to match standalone
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

            # Build quality universe (re-screen periodically)
            epochs = sorted(df_signals["date_epoch"].unique().to_list())
            rescreen_interval = rescreen_days * 86400
            rerank_interval = rerank_days * 86400

            quality_universe = {}
            last_screen_epoch = None
            for epoch in epochs:
                if (
                    last_screen_epoch is not None
                    and (epoch - last_screen_epoch) < rescreen_interval
                ):
                    quality_universe[epoch] = quality_universe[last_screen_epoch]
                    continue
                day_data = df_signals.filter(
                    (pl.col("date_epoch") == epoch)
                    & (pl.col("instrument").is_in(list(period_universe_set)))
                    & (pl.col("is_quality") == True)  # noqa: E712
                )
                quality_universe[epoch] = set(day_data["instrument"].to_list())
                last_screen_epoch = epoch

            # Build momentum universe (top N% by trailing return)
            # Rank only period-universe instruments (fixed set, matches standalone)
            momentum_universe = {}
            last_rank_epoch = None
            for epoch in epochs:
                if (
                    last_rank_epoch is not None
                    and (epoch - last_rank_epoch) < rerank_interval
                ):
                    momentum_universe[epoch] = momentum_universe[last_rank_epoch]
                    continue
                day_data = (
                    df_signals.filter(
                        (pl.col("date_epoch") == epoch)
                        & (pl.col("instrument").is_in(list(period_universe_set)))
                        & (pl.col("momentum_return").is_not_null())
                    )
                    .sort("momentum_return", descending=True)
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

            pool_sizes = [len(v) for v in combined_universe.values() if v]
            avg_pool = (
                sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
            )

            extras = [
                f"quality={consecutive_years}yr",
                f"mom={momentum_lookback}d top{momentum_percentile * 100:.0f}%",
            ]
            if roe_threshold > 0:
                extras.append(f"ROE>{roe_threshold}%")
            if pe_threshold > 0:
                extras.append(f"PE<{pe_threshold}")
            if de_threshold > 0:
                extras.append(f"D/E<{de_threshold}")
            if use_regime:
                extras.append(
                    f"regime={regime_instrument}>SMA{regime_sma_period}"
                )
            if use_direction:
                extras.append(
                    f"direction>{direction_threshold}({direction_n_day_ma}d)"
                )
            print(
                f"  Universe: avg {avg_pool:.0f} stocks ({', '.join(extras)})"
            )

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

            # Entry filter: dip + period universe + optional regime
            # NOTE: quality is enforced via combined_universe membership
            # (rescreened every N days), NOT per-row. This matches standalone
            # behavior: screen periodically, trust the snapshot between screens.
            entry_filter = (
                (pl.col("dip_pct") >= dip_threshold)
                & (pl.col("instrument").is_in(list(period_universe_set)))
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("rolling_peak").is_not_null())
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
                    "rolling_peak",
                    "dip_pct",
                ])
                .to_dicts()
            )

            print(f"  Entry candidates (pre-filter): {len(entry_rows)}")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                require_peak_recovery = exit_config.get("require_peak_recovery", True)
                orders_this_config = 0

                for entry in entry_rows:
                    inst = entry["instrument"]
                    epoch = entry["date_epoch"]

                    # Must be in combined (quality AND momentum) universe
                    universe = combined_universe.get(epoch, set())
                    if inst not in universe:
                        continue

                    # Direction score (market breadth) gate
                    if use_direction:
                        ds = direction_scores.get(epoch, 0)
                        if ds <= direction_threshold:
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
                    peak_price = entry["rolling_peak"]

                    if entry_price is None or entry_price <= 0:
                        continue
                    if peak_price is None:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch, exit_price = walk_forward_exit(
                        ed["epochs"], ed["closes"], start_idx,
                        entry_epoch, entry_price, peak_price,
                        tsl_pct, max_hold_days,
                        opens=ed["opens"],
                        require_peak_recovery=require_peak_recovery,
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
                        "scanner_config_ids": entry.get("scanner_config_ids") or "1",
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                        "dip_pct": entry.get("dip_pct", 0),
                    })
                    orders_this_config += 1

                peak_tag = "" if require_peak_recovery else " (pure TSL)"
                print(
                    f"    Exit TSL={tsl_pct * 100:.0f}% "
                    f"hold={max_hold_days}d{peak_tag}: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [2]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "momentum_lookback_days": entry_cfg.get("momentum_lookback_days", [63]),
            "momentum_percentile": entry_cfg.get("momentum_percentile", [0.30]),
            "rerank_interval_days": entry_cfg.get("rerank_interval_days", [63]),
            "dip_threshold_pct": entry_cfg.get("dip_threshold_pct", [5]),
            "peak_lookback_days": entry_cfg.get("peak_lookback_days", [63]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
            "roe_threshold": entry_cfg.get("roe_threshold", [15]),
            "pe_threshold": entry_cfg.get("pe_threshold", [25]),
            "de_threshold": entry_cfg.get("de_threshold", [0]),
            "fundamental_missing_mode": entry_cfg.get("fundamental_missing_mode", ["skip"]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
            "direction_score_n_day_ma": entry_cfg.get("direction_score_n_day_ma", [0]),
            "direction_score_threshold": entry_cfg.get("direction_score_threshold", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [504]),
            "require_peak_recovery": exit_cfg.get("require_peak_recovery", [True]),
        }

register_strategy("momentum_dip_quality", MomentumDipQualitySignalGenerator)

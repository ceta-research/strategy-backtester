"""Quality Dip-Buy with Multi-Tier Averaging signal generator.

Ports the standalone scripts/quality_dip_buy_tiered.py strategy to the engine pipeline.

Universe: Stocks with N consecutive years of positive returns (quality filter).
Entry: Multi-tier averaging at progressively deeper dip levels from rolling peak.
  For n_tiers=3, base_dip=5%, tier_mult=1.5:
    Tier 1: 5% dip from peak
    Tier 2: 7.5% dip from peak
    Tier 3: 11.25% dip from peak
  Each tier generates a SEPARATE order with its own entry_config_id.
Exit: Peak recovery + trailing stop-loss, or max hold days. Each tier exits independently.

Thesis: DCA into dips lowers average entry price and improves risk-adjusted returns
compared to single-entry dip-buy.
"""

import time

import polars as pl

from engine.config_loader import (
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import register_strategy, add_next_day_values, run_scanner, walk_forward_exit, finalize_orders, build_regime_filter

TRADING_DAYS_PER_YEAR = 252


class QualityDipTieredSignalGenerator:
    """Buy dips in quality stocks at multiple tier levels."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Quality Dip-Buy Tiered Signal Generation ---")

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Pre-scan entry configs for regime filter requirements
        regime_configs = set()
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0:
                regime_configs.add((ri, rp))

        # Pre-build regime filters (one per unique instrument+period combo)
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Compute next-day values on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Phase 3: Generate orders (multi-tier)
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            consecutive_years = entry_config["consecutive_positive_years"]
            min_yearly_return = entry_config["min_yearly_return_pct"] / 100.0
            base_dip_threshold = entry_config["base_dip_threshold_pct"] / 100.0
            n_tiers = entry_config["n_tiers"]
            tier_multiplier = entry_config["tier_multiplier"]
            peak_lookback = entry_config["peak_lookback_days"]
            rescreen_days = entry_config["rescreen_interval_days"]
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            # Get regime filter
            bull_epochs = regime_cache.get((regime_instrument, regime_sma_period), set())
            use_regime = bool(bull_epochs)

            # Compute tier-specific dip thresholds
            tier_dip_thresholds = []
            for tier in range(n_tiers):
                tier_dip = base_dip_threshold * (tier_multiplier ** tier)
                tier_dip_thresholds.append(tier_dip)

            df_signals = df_ind.clone()

            # Compute rolling peak (highest close in last peak_lookback days)
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(window_size=peak_lookback, min_samples=peak_lookback)
                .over("instrument")
                .alias("rolling_peak")
            )

            # Compute dip percentage from peak
            df_signals = df_signals.with_columns(
                ((pl.col("rolling_peak") - pl.col("close")) / pl.col("rolling_peak")).alias("dip_pct")
            )

            # Compute trailing yearly returns for quality filter
            yearly_return_cols = []
            for yr in range(consecutive_years):
                shift_recent = yr * TRADING_DAYS_PER_YEAR
                shift_older = (yr + 1) * TRADING_DAYS_PER_YEAR
                col_name = f"yr_return_{yr + 1}"
                df_signals = df_signals.with_columns(
                    (pl.col("close").shift(shift_recent).over("instrument")
                     / pl.col("close").shift(shift_older).over("instrument") - 1.0)
                    .alias(col_name)
                )
                yearly_return_cols.append(col_name)

            # Quality filter: all N trailing years must have returns > min threshold
            quality_expr = pl.lit(True)
            for col_name in yearly_return_cols:
                quality_expr = quality_expr & (pl.col(col_name) > min_yearly_return)
            df_signals = df_signals.with_columns(quality_expr.alias("is_quality"))

            # Trim to simulation range and merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(["uid", "scanner_config_ids"]).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Build quality universe (re-screen periodically)
            epochs = sorted(df_signals["date_epoch"].unique().to_list())
            rescreen_interval = rescreen_days * 86400
            quality_universe = {}
            last_screen_epoch = None

            for epoch in epochs:
                if last_screen_epoch is not None and (epoch - last_screen_epoch) < rescreen_interval:
                    quality_universe[epoch] = quality_universe[last_screen_epoch]
                    continue

                day_data = df_signals.filter(
                    (pl.col("date_epoch") == epoch)
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("is_quality") == True)  # noqa: E712
                )
                quality_instruments = set(day_data["instrument"].to_list())
                quality_universe[epoch] = quality_instruments
                last_screen_epoch = epoch

            pool_sizes = [len(v) for v in quality_universe.values() if v]
            avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
            extras = []
            if use_regime:
                extras.append(f"regime={regime_instrument}>SMA{regime_sma_period}")
            if min_yearly_return > 0:
                extras.append(f"min_yr>{min_yearly_return * 100:.0f}%")
            tier_dip_strs = [f"{d * 100:.1f}%" for d in tier_dip_thresholds]
            extras.append(f"tiers=[{', '.join(tier_dip_strs)}]")
            extra_str = ", " + ", ".join(extras) if extras else ""
            print(f"  Quality pool: avg {avg_pool:.0f} stocks ({consecutive_years}yr filter){extra_str}")

            # Build per-instrument price data for exit walk
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument", maintain_order=True):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                }

            # Generate entries for each tier separately
            for tier_idx, tier_dip in enumerate(tier_dip_thresholds):
                tier_num = tier_idx + 1

                # Entry filter for this tier: quality + dip >= tier threshold
                entry_filter = (
                    (pl.col("dip_pct") >= tier_dip)
                    & (pl.col("is_quality") == True)  # noqa: E712
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("next_epoch").is_not_null())
                    & (pl.col("next_open").is_not_null())
                    & (pl.col("rolling_peak").is_not_null())
                )

                # Regime filter: only enter during bull market
                if use_regime:
                    entry_filter = entry_filter & (pl.col("date_epoch").is_in(list(bull_epochs)))

                entry_rows = df_signals.filter(entry_filter).select([
                    "instrument", "date_epoch", "next_epoch", "next_open",
                    "next_volume", "scanner_config_ids", "rolling_peak",
                ]).to_dicts()

                print(f"    Tier {tier_num} (dip>={tier_dip * 100:.1f}%): {len(entry_rows)} candidates")

                # Walk forward for each exit config
                for exit_config in get_exit_config_iterator(context):
                    trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                    max_hold_days = exit_config["max_hold_days"]

                    for entry in entry_rows:
                        inst = entry["instrument"]
                        epoch = entry["date_epoch"]

                        universe = quality_universe.get(epoch, set())
                        if inst not in universe:
                            continue

                        if inst not in exit_data:
                            continue

                        ed = exit_data[inst]
                        entry_epoch = entry["next_epoch"]
                        entry_price = entry["next_open"]
                        peak_price = entry["rolling_peak"]

                        if entry_price is None or entry_price <= 0:
                            continue
                        if peak_price is None or peak_price <= entry_price:
                            continue

                        try:
                            start_idx = ed["epochs"].index(entry_epoch)
                        except ValueError:
                            continue

                        exit_epoch, exit_price = walk_forward_exit(
                            ed["epochs"], ed["closes"], start_idx,
                            entry_epoch, entry_price, peak_price,
                            trailing_stop_pct, max_hold_days,
                            # Dip-buy: entry is below peak. Wait for recovery.
                            require_peak_recovery=True,
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
                            "scanner_config_ids": entry["scanner_config_ids"],
                            "entry_config_ids": f"{entry_config['id']}_t{tier_num}",
                            "exit_config_ids": str(exit_config["id"]),
                        })

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [2]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "n_tiers": entry_cfg.get("n_tiers", [1]),
            "tier_multiplier": entry_cfg.get("tier_multiplier", [1.5]),
            "base_dip_threshold_pct": entry_cfg.get("base_dip_threshold_pct", [5]),
            "peak_lookback_days": entry_cfg.get("peak_lookback_days", [63]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [504]),
        }

register_strategy("quality_dip_tiered", QualityDipTieredSignalGenerator)

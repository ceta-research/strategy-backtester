"""Forced-Selling Detection: Idiosyncratic Dip + Volume Spike.

Ports scripts/forced_selling_dip.py to the engine pipeline.

Entry signal:
- Quality stock (N consecutive years positive returns)
- Fundamental overlay (ROE, PE, D/E - optional)
- Stock drops X% MORE than its sector over N days (idiosyncratic dip)
- Volume on signal day > Y x 20-day average (abnormal selling pressure)
- Entry: next-day open (MOC execution)

Exit: peak recovery + trailing stop-loss, or max hold days

Standalone result: Calmar 0.64. Engine result expected lower (honest liquidity filter).
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
    fetch_fundamentals, passes_fundamental_filter, build_regime_filter,
)

TRADING_DAYS_PER_YEAR = 252


def _fetch_sector_map(exchanges):
    """Fetch symbol -> sector mapping from FMP profile."""
    from lib.cr_client import CetaResearch

    cr = CetaResearch()

    suffix_filters = []
    suffixes = []
    for exchange in exchanges:
        if exchange == "NSE":
            suffix_filters.append("symbol LIKE '%.NS'")
            suffixes.append(".NS")
        elif exchange == "US":
            suffix_filters.append(
                "symbol NOT LIKE '%.%' AND symbol NOT LIKE '%-%'"
            )

    if not suffix_filters:
        return {}

    where_clause = " OR ".join(f"({f})" for f in suffix_filters)
    sql = f"""
    SELECT symbol, sector FROM fmp.profile
    WHERE ({where_clause}) AND sector IS NOT NULL AND sector != ''
    """

    try:
        results = cr.query(sql, timeout=60, limit=100000, verbose=False)
    except Exception as e:
        print(f"  Warning: could not fetch sector data: {e}")
        return {}

    if not results:
        return {}

    sector_map = {}
    for row in results:
        sym = row.get("symbol", "")
        sector = row.get("sector") or "Unknown"
        for suffix in suffixes:
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
                break
        sector_map[sym] = sector

    print(
        f"  Sector data: {len(sector_map)} stocks, "
        f"{len(set(sector_map.values()))} sectors"
    )
    return sector_map


class ForcedSellingDipSignalGenerator:
    """Buy quality stocks with idiosyncratic dips and abnormal volume."""

    def generate_orders(self, context, df_tick_data):
        print("\n--- Forced Selling Dip Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # Check features needed
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

        fundamentals = {}
        if needs_fundamentals:
            fundamentals = fetch_fundamentals(list(exchanges))

        sector_map = _fetch_sector_map(list(exchanges))

        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # ── Phase 1: Scanner (per-day liquidity filter) ──
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # ── Phase 2: Compute indicators ──
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Volume ratio (volume / 20-day average)
        df_ind = df_ind.with_columns(
            pl.col("volume")
            .rolling_mean(window_size=20, min_samples=5)
            .over("instrument")
            .alias("avg_volume_20d")
        )
        df_ind = df_ind.with_columns(
            (pl.col("volume") / pl.col("avg_volume_20d")).alias("volume_ratio")
        )

        # ── Phase 3: Generate orders ──
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            consecutive_years = entry_config["consecutive_positive_years"]
            min_yearly_return = entry_config["min_yearly_return_pct"] / 100.0
            sector_lookback = entry_config["sector_lookback_days"]
            dip_threshold = entry_config["dip_threshold_pct"] / 100.0
            volume_multiplier = entry_config["volume_multiplier"]
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

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

            df_signals = df_ind.clone()

            # Rolling peak
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(
                    window_size=peak_lookback, min_samples=peak_lookback
                )
                .over("instrument")
                .alias("rolling_peak")
            )

            # Quality filter
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

            # Stock N-day return for idiosyncratic dip
            df_signals = df_signals.with_columns(
                (
                    pl.col("close")
                    / pl.col("close").shift(sector_lookback).over("instrument")
                    - 1.0
                ).alias("stock_return")
            )

            # Trim to sim range, merge scanner
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

            # Build quality universe
            epochs = sorted(df_signals["date_epoch"].unique().to_list())
            rescreen_interval = rescreen_days * 86400

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
                quality_universe[epoch] = set(day_data["instrument"].to_list())
                last_screen_epoch = epoch

            # Compute sector daily returns for idiosyncratic dip calculation
            # Group by (date_epoch, sector) -> mean daily return
            df_sector = df_signals.clone()
            df_sector = df_sector.with_columns(
                pl.col("instrument")
                .str.split(":")
                .list.get(1)
                .alias("bare_symbol")
            )
            # Map symbols to sectors
            sector_series = [
                sector_map.get(sym, "Unknown")
                for sym in df_sector["bare_symbol"].to_list()
            ]
            df_sector = df_sector.with_columns(
                pl.Series("sector", sector_series, dtype=pl.Utf8)
            )

            # Daily return per stock
            df_sector = df_sector.with_columns(
                (pl.col("close") / pl.col("close").shift(1).over("instrument") - 1.0)
                .alias("daily_return")
            )

            # Sector cumulative return over lookback period
            # For each stock on each day, sum sector daily returns over the past sector_lookback days
            sector_cum_returns = {}  # (epoch, sector) -> cumulative return
            epoch_list = sorted(df_sector["date_epoch"].unique().to_list())

            # Build sector daily return lookup
            sector_daily = {}  # epoch -> {sector -> mean_return}
            for epoch_row in (
                df_sector.group_by(["date_epoch", "sector"])
                .agg(pl.col("daily_return").mean().alias("mean_return"))
                .to_dicts()
            ):
                ep = epoch_row["date_epoch"]
                sec = epoch_row["sector"]
                ret = epoch_row["mean_return"]
                if ep not in sector_daily:
                    sector_daily[ep] = {}
                sector_daily[ep][sec] = ret if ret is not None else 0.0

            # Build per-instrument exit data
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                }

            # Entry filter: quality + scanner + volume + idiosyncratic dip
            entry_filter = (
                (pl.col("is_quality") == True)  # noqa: E712
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("stock_return").is_not_null())
                & (pl.col("volume_ratio").is_not_null())
                & (pl.col("volume_ratio") >= volume_multiplier)
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
                    "stock_return",
                    "volume_ratio",
                ])
                .to_dicts()
            )

            print(
                f"  Entry candidates (quality+volume): {len(entry_rows)} "
                f"(idio_dip>{dip_threshold * 100:.0f}%, vol>{volume_multiplier}x, "
                f"sec_lb={sector_lookback}d)"
            )

            # Filter by idiosyncratic dip (stock dropped more than sector)
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                orders_this_config = 0

                for entry in entry_rows:
                    inst = entry["instrument"]
                    epoch = entry["date_epoch"]

                    universe = quality_universe.get(epoch, set())
                    if inst not in universe:
                        continue

                    # Compute sector cumulative return over lookback
                    symbol = inst.split(":")[1]
                    sector = sector_map.get(symbol, "Unknown")

                    sector_cum = 1.0
                    # Find epochs in the lookback window
                    for lk_ep in epoch_list:
                        if lk_ep > epoch:
                            break
                        if lk_ep <= epoch - sector_lookback * 86400:
                            continue
                        day_sectors = sector_daily.get(lk_ep, {})
                        sector_cum *= (1.0 + day_sectors.get(sector, 0.0))
                    sector_cum -= 1.0  # compound return

                    # Idiosyncratic return
                    stock_ret = entry["stock_return"]
                    idio = stock_ret - sector_cum

                    if idio >= -dip_threshold:
                        continue

                    # Fundamental filter
                    if fundamentals and (
                        roe_threshold > 0
                        or pe_threshold > 0
                        or de_threshold > 0
                    ):
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
                    if peak_price is None or peak_price <= entry_price:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch, exit_price = walk_forward_exit(
                        ed["epochs"], ed["closes"], start_idx,
                        entry_epoch, entry_price, peak_price,
                        tsl_pct, max_hold_days,
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
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [2]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "sector_lookback_days": entry_cfg.get("sector_lookback_days", [20]),
            "dip_threshold_pct": entry_cfg.get("dip_threshold_pct", [5]),
            "volume_multiplier": entry_cfg.get("volume_multiplier", [2.0]),
            "peak_lookback_days": entry_cfg.get("peak_lookback_days", [63]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
            "roe_threshold": entry_cfg.get("roe_threshold", [15]),
            "pe_threshold": entry_cfg.get("pe_threshold", [25]),
            "de_threshold": entry_cfg.get("de_threshold", [0]),
            "fundamental_missing_mode": entry_cfg.get("fundamental_missing_mode", ["skip"]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [504]),
        }

register_strategy("forced_selling_dip", ForcedSellingDipSignalGenerator)

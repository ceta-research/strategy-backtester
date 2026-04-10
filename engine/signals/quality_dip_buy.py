"""Quality Dip-Buy signal generator.

Universe: Stocks with N consecutive years of positive returns (quality filter).
Entry: Price drops X% from rolling peak in a quality stock.
Exit: Price recovers to the pre-dip peak (hard) or TSL after reaching peak.

Thesis: Quality stocks with consistent positive returns recover from dips.

V2: Sector diversification (max_per_sector).
V3: Market regime filter (only buy in bull markets), RSI confirmation.
"""

import time

import polars as pl

from engine.config_loader import get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator
from engine.signals.base import register_strategy, add_next_day_values, compute_rsi, build_regime_filter

TRADING_DAYS_PER_YEAR = 252


def _fetch_sector_map():
    """Fetch symbol -> sector mapping from FMP profile for NSE stocks."""
    from lib.cr_client import CetaResearch
    cr = CetaResearch()
    try:
        results = cr.query(
            "SELECT symbol, sector FROM fmp.profile WHERE symbol LIKE '%.NS'",
            timeout=60, limit=100000, verbose=False, format="json"
        )
        if not results:
            return {}
        sector_map = {}
        for row in results:
            fmp_sym = row.get("symbol", "")
            sector = row.get("sector") or "Unknown"
            if fmp_sym.endswith(".NS"):
                bare = fmp_sym[:-3]
                sector_map[bare] = sector
        print(f"  Sector data: {len(sector_map)} stocks, "
              f"{len(set(sector_map.values()))} sectors")
        return sector_map
    except Exception as e:
        print(f"  Warning: could not fetch sector data: {e}")
        return {}


class QualityDipBuySignalGenerator:
    """Buy dips in stocks with consistent positive yearly returns."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Quality Dip-Buy Signal Generation ---")
        t0 = time.time()

        df = df_tick_data.clone()
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Check if any entry config needs sector data or regime filter
        needs_sectors = False
        regime_configs = set()
        for entry_config in get_entry_config_iterator(context):
            if entry_config.get("max_per_sector", 0) > 0:
                needs_sectors = True
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0:
                regime_configs.add((ri, rp))

        sector_map = _fetch_sector_map() if needs_sectors else {}

        # Pre-build regime filters (one per unique instrument+period combo)
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker = {}
        for scanner_config in get_scanner_config_iterator(context):
            df_scan = df.clone()

            filter_exprs = []
            for instrument in scanner_config["instruments"]:
                if instrument["symbols"]:
                    filter_exprs.append(
                        (pl.col("exchange") == instrument["exchange"])
                        & (pl.col("symbol").is_in(instrument["symbols"]))
                    )
                else:
                    filter_exprs.append(pl.col("exchange") == instrument["exchange"])
            if filter_exprs:
                combined = filter_exprs[0]
                for expr in filter_exprs[1:]:
                    combined = combined | expr
                df_scan = df_scan.filter(combined)

            atc = scanner_config["avg_day_transaction_threshold"]
            df_scan = df_scan.with_columns(
                (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
            )
            df_scan = df_scan.sort(["instrument", "date_epoch"]).with_columns(
                pl.col("avg_txn_turnover")
                .rolling_mean(window_size=atc["period"], min_samples=1)
                .over("instrument")
                .alias("avg_txn_turnover")
            )
            df_scan = df_scan.drop_nulls()
            df_scan = df_scan.filter(pl.col("close") > scanner_config["price_threshold"])
            df_scan = df_scan.filter(pl.col("avg_txn_turnover") > atc["threshold"])

            uid_series = df_scan.select(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )["uid"]
            shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

        df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
        df_trimmed = df_trimmed.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )
        signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
        uids = df_trimmed["uid"].to_list()
        uid_to_signals = {}
        for uid in uids:
            signals = [str(k) for k, v in signal_sets.items() if uid in v]
            uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
        df_trimmed = df_trimmed.with_columns(
            pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
        )

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df_trimmed.height} rows")

        # Phase 2: Compute next-day values and RSI on full data
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Compute RSI(14) once for all instruments
        df_ind = df_ind.with_columns(
            compute_rsi(pl.col("close"), 14).over("instrument").alias("rsi_14")
        )

        # Phase 3: Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            consecutive_years = entry_config["consecutive_positive_years"]
            min_yearly_return = entry_config["min_yearly_return_pct"] / 100.0
            dip_threshold = entry_config["dip_threshold_pct"] / 100.0
            peak_lookback = entry_config["peak_lookback_days"]
            rescreen_days = entry_config["rescreen_interval_days"]
            max_per_sector = entry_config.get("max_per_sector", 0)
            rsi_threshold = entry_config.get("rsi_threshold", 0)
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            # Get regime filter
            bull_epochs = regime_cache.get((regime_instrument, regime_sma_period), set())
            use_regime = bool(bull_epochs)

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
                    & (pl.col("is_quality") == True)
                )
                quality_instruments = set(day_data["instrument"].to_list())

                # Sector diversification: cap per sector
                if max_per_sector > 0 and sector_map:
                    sector_counts = {}
                    filtered = set()
                    for inst in sorted(quality_instruments):
                        symbol = inst.split(":")[1]
                        sector = sector_map.get(symbol, "Unknown")
                        count = sector_counts.get(sector, 0)
                        if count < max_per_sector:
                            filtered.add(inst)
                            sector_counts[sector] = count + 1
                    quality_instruments = filtered

                quality_universe[epoch] = quality_instruments
                last_screen_epoch = epoch

            pool_sizes = [len(v) for v in quality_universe.values() if v]
            avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
            extras = []
            if max_per_sector > 0:
                extras.append(f"max {max_per_sector}/sector")
            if use_regime:
                extras.append(f"regime={regime_instrument}>SMA{regime_sma_period}")
            if rsi_threshold > 0:
                extras.append(f"RSI<{rsi_threshold}")
            if min_yearly_return > 0:
                extras.append(f"min_yr>{min_yearly_return*100:.0f}%")
            extra_str = ", " + ", ".join(extras) if extras else ""
            print(f"  Quality pool: avg {avg_pool:.0f} stocks ({consecutive_years}yr filter), "
                  f"dip {dip_threshold*100:.0f}%{extra_str}")

            # Build per-instrument price data for exit walk
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                }

            # Entry signals: quality + dip + optional RSI + optional regime
            entry_filter = (
                (pl.col("dip_pct") >= dip_threshold)
                & (pl.col("is_quality") == True)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("rolling_peak").is_not_null())
            )

            # RSI filter: only enter when RSI < threshold (oversold confirmation)
            if rsi_threshold > 0:
                entry_filter = entry_filter & (pl.col("rsi_14") < rsi_threshold)

            # Regime filter: only enter during bull market
            if use_regime:
                entry_filter = entry_filter & (pl.col("date_epoch").is_in(list(bull_epochs)))

            entry_rows = df_signals.filter(entry_filter).select([
                "instrument", "date_epoch", "next_epoch", "next_open",
                "next_volume", "scanner_config_ids", "rolling_peak",
            ]).to_dicts()

            print(f"  Entry candidates: {len(entry_rows)}")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
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

                    # Walk forward to find exit
                    exit_epoch = None
                    exit_price = None
                    trail_high = entry_price

                    if tsl_pct == 0:
                        for j in range(start_idx, len(ed["epochs"])):
                            c = ed["closes"][j]
                            if c is None:
                                continue
                            hold_days = (ed["epochs"][j] - entry_epoch) / 86400
                            if max_hold_days > 0 and hold_days >= max_hold_days:
                                exit_epoch = ed["epochs"][j]
                                exit_price = c
                                break
                            if c >= peak_price:
                                exit_epoch = ed["epochs"][j]
                                exit_price = c
                                break
                    else:
                        reached_peak = False
                        for j in range(start_idx, len(ed["epochs"])):
                            c = ed["closes"][j]
                            if c is None:
                                continue
                            if c > trail_high:
                                trail_high = c
                            hold_days = (ed["epochs"][j] - entry_epoch) / 86400
                            if max_hold_days > 0 and hold_days >= max_hold_days:
                                exit_epoch = ed["epochs"][j]
                                exit_price = c
                                break
                            if c >= peak_price:
                                reached_peak = True
                            if reached_peak and c <= trail_high * (1 - tsl_pct):
                                exit_epoch = ed["epochs"][j]
                                exit_price = c
                                break

                    if exit_epoch is None and len(ed["epochs"]) > start_idx:
                        last_idx = len(ed["epochs"]) - 1
                        exit_epoch = ed["epochs"][last_idx]
                        exit_price = ed["closes"][last_idx]

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

        entry_elapsed = round(time.time() - t1, 2)

        if not all_order_rows:
            print(f"  Signal gen: {entry_elapsed}s, 0 orders")
            column_order = [
                "instrument", "entry_epoch", "exit_epoch",
                "entry_price", "exit_price", "entry_volume", "exit_volume",
                "scanner_config_ids", "entry_config_ids", "exit_config_ids",
            ]
            return pl.DataFrame(schema={
                c: pl.Utf8 if c in ("instrument", "scanner_config_ids", "entry_config_ids", "exit_config_ids")
                else pl.Float64 for c in column_order
            })

        df_orders = pl.DataFrame(all_order_rows)
        df_orders = df_orders.select([
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids",
        ]).sort(["instrument", "entry_epoch", "exit_epoch"])

        print(f"  Signal gen: {entry_elapsed}s, {df_orders.height} orders")
        return df_orders

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [3]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "dip_threshold_pct": entry_cfg.get("dip_threshold_pct", [10]),
            "peak_lookback_days": entry_cfg.get("peak_lookback_days", [252]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
            "max_per_sector": entry_cfg.get("max_per_sector", [0]),
            "rsi_threshold": entry_cfg.get("rsi_threshold", [0]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "tsl_pct": exit_cfg.get("tsl_pct", [0]),
            "max_hold_days": exit_cfg.get("max_hold_days", [0]),
        }

register_strategy("quality_dip_buy", QualityDipBuySignalGenerator)

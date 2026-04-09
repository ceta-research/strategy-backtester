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
from engine.signals.base import register_strategy, add_next_day_values, run_scanner, walk_forward_exit, finalize_orders

TRADING_DAYS_PER_YEAR = 252
FILING_LAG_DAYS = 45


def _fetch_fundamentals(exchanges):
    """Fetch FY fundamental ratios from fmp.financial_ratios.

    Returns dict[bare_symbol, list[{epoch, roe, de, pe}]] sorted by epoch.
    """
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
    SELECT symbol, CAST(dateEpoch AS BIGINT) AS dateEpoch,
           netIncomePerShare, shareholdersEquityPerShare,
           debtToEquityRatio, priceToEarningsRatio
    FROM fmp.financial_ratios
    WHERE ({where_clause})
      AND period = 'FY'
      AND shareholdersEquityPerShare IS NOT NULL
      AND shareholdersEquityPerShare > 0
    ORDER BY symbol, dateEpoch
    """

    print("  Fetching fundamental ratios (FY)...")
    try:
        results = cr.query(
            sql, timeout=600, limit=10000000, verbose=False,
            memory_mb=16384, threads=6,
        )
    except Exception as e:
        print(f"  WARNING: Could not fetch fundamentals: {e}")
        return {}

    if not results:
        print("  WARNING: No fundamental data fetched")
        return {}

    fundamentals = {}
    for r in results:
        sym = r["symbol"]
        for suffix in suffixes:
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
                break

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        ni = r.get("netIncomePerShare")
        eq = r.get("shareholdersEquityPerShare")
        roe = (
            (float(ni) / float(eq) * 100)
            if (ni is not None and eq and float(eq) > 0)
            else None
        )
        de = r.get("debtToEquityRatio")
        pe = r.get("priceToEarningsRatio")

        if sym not in fundamentals:
            fundamentals[sym] = []
        fundamentals[sym].append({
            "epoch": epoch,
            "roe": roe,
            "de": float(de) if de is not None else None,
            "pe": float(pe) if pe is not None else None,
        })

    for sym in fundamentals:
        fundamentals[sym].sort(key=lambda x: x["epoch"])

    print(
        f"  Loaded fundamentals for {len(fundamentals)} symbols, "
        f"{sum(len(v) for v in fundamentals.values())} data points"
    )
    return fundamentals


def _get_fundamental_at(fundamentals, symbol, epoch, lag_days=FILING_LAG_DAYS):
    """Get latest fundamental data available before lag-adjusted epoch."""
    records = fundamentals.get(symbol)
    if not records:
        return None
    lag_epoch = epoch - lag_days * 86400
    best = None
    for rec in records:
        if rec["epoch"] <= lag_epoch:
            best = rec
        else:
            break
    return best


def _build_regime_filter(df_tick_data, regime_instrument, regime_sma_period):
    """Build set of epochs where regime instrument is above its SMA."""
    if not regime_instrument or regime_sma_period <= 0:
        return set()

    df_regime = df_tick_data.filter(
        pl.col("instrument") == regime_instrument
    ).sort("date_epoch")

    if df_regime.is_empty():
        print(f"  Warning: regime instrument {regime_instrument} not found")
        return set()

    df_regime = df_regime.with_columns(
        pl.col("close")
        .rolling_mean(window_size=regime_sma_period, min_samples=regime_sma_period)
        .alias("regime_sma")
    )

    bull_epochs = set(
        df_regime.filter(
            (pl.col("close") > pl.col("regime_sma"))
            & (pl.col("regime_sma").is_not_null())
        )["date_epoch"].to_list()
    )

    total = df_regime.filter(pl.col("regime_sma").is_not_null()).height
    pct = len(bull_epochs) / total * 100 if total > 0 else 0
    print(
        f"  Regime: {regime_instrument} > SMA({regime_sma_period}), "
        f"{len(bull_epochs)}/{total} days bullish ({pct:.0f}%)"
    )
    return bull_epochs


def _passes_fundamental_filter(
    fundamentals, symbol, epoch, roe_threshold, pe_threshold, de_threshold,
    missing_mode,
):
    """Check if a stock passes fundamental filters at a given epoch.

    Returns True if passes, False if filtered out.
    """
    if roe_threshold <= 0 and pe_threshold <= 0 and de_threshold <= 0:
        return True

    fund = _get_fundamental_at(fundamentals, symbol, epoch)
    if fund is None:
        return missing_mode != "skip"

    if roe_threshold > 0:
        roe = fund.get("roe")
        if roe is not None and roe < roe_threshold:
            return False

    if de_threshold > 0:
        de = fund.get("de")
        if de is not None and de > de_threshold:
            return False

    if pe_threshold > 0:
        pe = fund.get("pe")
        if pe is not None and (pe <= 0 or pe > pe_threshold):
            return False

    return True


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
            fundamentals = _fetch_fundamentals(list(exchanges))

        # Pre-build regime filters
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = _build_regime_filter(df_tick_data, ri, rp)

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

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_regime = bool(bull_epochs)

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
                df_signals
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

            # Entry filter: quality + dip + period universe + optional regime
            entry_filter = (
                (pl.col("dip_pct") >= dip_threshold)
                & (pl.col("is_quality") == True)  # noqa: E712
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
                        if not _passes_fundamental_filter(
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
                        "scanner_config_ids": entry.get("scanner_config_ids") or "1",
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                        "dip_pct": entry.get("dip_pct", 0),
                    })
                    orders_this_config += 1

                print(
                    f"    Exit TSL={tsl_pct * 100:.0f}% "
                    f"hold={max_hold_days}d: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)


register_strategy("momentum_dip_quality", MomentumDipQualitySignalGenerator)

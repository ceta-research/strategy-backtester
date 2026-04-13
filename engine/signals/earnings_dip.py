"""Earnings Surprise + Post-Earnings Dip Strategy.

Ports scripts/earnings_surprise_dip.py to the engine pipeline.

Entry signal:
- Quality stock (N consecutive years positive returns)
- Earnings beat: epsActual > epsEstimated * (1 + surprise_threshold)
- Post-earnings dip: price drops X% from post-earnings peak within N days
- Optional fundamental filters (ROE, PE, D/E with 45-day lag)
- Optional regime filter (benchmark > SMA)
- Entry at next-day open (MOC execution)

Exit: Peak recovery + trailing stop-loss, or max hold days

Data: fmp.earnings_surprises (deduplicated with ROW_NUMBER, joined with
fmp.profile for exchange filtering).
"""

import bisect
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


def _fetch_earnings_surprises(exchanges, start_epoch, end_epoch):
    """Fetch deduplicated earnings surprises from fmp.earnings_surprises.

    Joins with fmp.profile for exchange filtering (earnings table has no
    exchange column). Deduplicates with ROW_NUMBER() partitioned by
    (symbol, dateEpoch).

    Returns dict[bare_symbol, list[{epoch, eps_actual, eps_estimated,
    surprise_pct}]] sorted by epoch within each symbol.
    """
    from lib.cr_client import CetaResearch

    cr = CetaResearch()

    exchange_filters = []
    suffixes = []
    for exchange in exchanges:
        if exchange == "NSE":
            exchange_filters.append("p.exchange = 'NSE'")
            suffixes.append(".NS")
        elif exchange in ("US", "NASDAQ", "NYSE"):
            exchange_filters.append("p.exchange IN ('NASDAQ', 'NYSE')")

    if not exchange_filters:
        return {}

    where_clause = " OR ".join(f"({f})" for f in exchange_filters)

    sql = f"""
    WITH ranked AS (
        SELECT e.symbol,
               CAST(e.dateEpoch AS BIGINT) AS dateEpoch,
               e.epsActual,
               e.epsEstimated,
               ROW_NUMBER() OVER (
                   PARTITION BY e.symbol, CAST(e.dateEpoch AS BIGINT)
                   ORDER BY e.epsActual DESC NULLS LAST
               ) as rn
        FROM fmp.earnings_surprises e
        JOIN fmp.profile p ON e.symbol = p.symbol
        WHERE ({where_clause})
          AND CAST(e.dateEpoch AS BIGINT) >= {start_epoch}
          AND CAST(e.dateEpoch AS BIGINT) <= {end_epoch}
          AND e.epsEstimated IS NOT NULL
          AND ABS(e.epsEstimated) > 0.01
    )
    SELECT symbol, dateEpoch, epsActual, epsEstimated
    FROM ranked
    WHERE rn = 1
    ORDER BY symbol, dateEpoch
    """

    print("  Fetching earnings surprises...")
    try:
        results = cr.query(
            sql, timeout=600, limit=10000000, verbose=False,
            memory_mb=16384, threads=6,
        )
    except Exception as e:
        print(f"  WARNING: Could not fetch earnings: {e}")
        return {}

    if not results:
        print("  WARNING: No earnings data fetched")
        return {}

    earnings = {}
    for r in results:
        sym = r["symbol"]
        # Strip exchange suffixes for bare symbol matching
        for suffix in suffixes:
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
                break

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        eps_actual = r.get("epsActual")
        eps_estimated = r.get("epsEstimated")
        if eps_actual is None or eps_estimated is None:
            continue

        eps_actual = float(eps_actual)
        eps_estimated = float(eps_estimated)
        if abs(eps_estimated) < 0.01:
            continue

        surprise_pct = (eps_actual - eps_estimated) / abs(eps_estimated) * 100

        if sym not in earnings:
            earnings[sym] = []
        earnings[sym].append({
            "epoch": epoch,
            "eps_actual": eps_actual,
            "eps_estimated": eps_estimated,
            "surprise_pct": surprise_pct,
        })

    for sym in earnings:
        earnings[sym].sort(key=lambda x: x["epoch"])

    total_events = sum(len(v) for v in earnings.values())
    print(
        f"  Earnings: {len(earnings)} symbols, {total_events} events"
    )
    return earnings


class EarningsDipSignalGenerator:
    """Buy quality stocks after earnings beat + post-earnings dip.

    Post-earnings announcement drift is one of the most robust anomalies
    in finance. This strategy targets the variant where stocks beat
    earnings and then dip on "sell the news" or sector rotation.
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- Earnings Dip Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )
        end_epoch = context.get(
            "end_epoch", context["static_config"]["end_epoch"]
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

        # Fetch earnings surprises
        # Use a wider window for prefetch (earnings before start_epoch
        # can generate dip entries after start_epoch)
        prefetch_days = context["static_config"].get("prefetch_days", 400)
        earnings_start = start_epoch - prefetch_days * 86400
        earnings = _fetch_earnings_surprises(
            list(exchanges), earnings_start, end_epoch
        )

        # Pre-build regime filters
        regime_cache = {}
        for ri, rp in regime_configs:
            regime_cache[(ri, rp)] = build_regime_filter(
                df_tick_data, ri, rp
            )

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
            surprise_threshold = entry_config["surprise_threshold_pct"]
            dip_threshold_pct = entry_config["dip_threshold_pct"]
            dip_threshold = dip_threshold_pct / 100.0
            post_earnings_window = entry_config["post_earnings_window"]
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

            # Build quality universe (re-screen periodically)
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

            pool_sizes = [len(v) for v in quality_universe.values() if v]
            avg_pool = (
                sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
            )

            extras = [
                f"quality={consecutive_years}yr",
                f"surprise>{surprise_threshold}%",
                f"dip>{dip_threshold_pct}%",
                f"window={post_earnings_window}d",
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

            # Build per-instrument price data for post-earnings dip scan
            inst_price_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                inst_price_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                    "opens": g["next_open"].to_list(),
                    "next_epochs": g["next_epoch"].to_list(),
                    "next_volumes": g["next_volume"].to_list(),
                    "scanner_ids": g["scanner_config_ids"].to_list(),
                }

            # Also build exit data from full indicator df (not trimmed)
            exit_data = {}
            for inst_tuple, group in df_ind.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                }

            # ── Post-earnings dip entry detection ──
            entry_candidates = []

            for inst_name, pd_data in inst_price_data.items():
                # Extract bare symbol from instrument (e.g. "NSE:RELIANCE")
                bare_symbol = inst_name.split(":")[1]
                sym_earnings = earnings.get(bare_symbol)
                if not sym_earnings:
                    continue

                pd_epochs = pd_data["epochs"]
                pd_closes = pd_data["closes"]
                pd_opens = pd_data["opens"]
                pd_next_epochs = pd_data["next_epochs"]
                pd_next_volumes = pd_data["next_volumes"]
                pd_scanner_ids = pd_data["scanner_ids"]

                if len(pd_epochs) < 30:
                    continue

                for event in sym_earnings:
                    if event["surprise_pct"] < surprise_threshold:
                        continue

                    earnings_epoch = event["epoch"]
                    if earnings_epoch < start_epoch:
                        continue

                    # Find bar index for earnings date (or closest after)
                    earn_idx = bisect.bisect_left(pd_epochs, earnings_epoch)
                    if earn_idx >= len(pd_epochs):
                        continue

                    # Check quality at earnings date
                    universe = quality_universe.get(pd_epochs[earn_idx])
                    if universe is None or inst_name not in universe:
                        # Try nearby epochs (quality rescreen is periodic)
                        found_quality = False
                        for offset in range(-5, 6):
                            check_idx = earn_idx + offset
                            if 0 <= check_idx < len(pd_epochs):
                                u = quality_universe.get(
                                    pd_epochs[check_idx]
                                )
                                if u and inst_name in u:
                                    found_quality = True
                                    break
                        if not found_quality:
                            continue

                    # Find post-earnings peak (highest close in first 5
                    # trading days after earnings)
                    peak_end = min(earn_idx + 5, len(pd_closes) - 1)
                    if peak_end <= earn_idx:
                        continue
                    post_peak = max(pd_closes[earn_idx:peak_end + 1])
                    if post_peak is None or post_peak <= 0:
                        continue

                    # Look for dip from post-earnings peak within window
                    scan_end = min(
                        earn_idx + post_earnings_window, len(pd_closes) - 1
                    )
                    for i in range(earn_idx + 5, scan_end):
                        if i + 1 >= len(pd_epochs):
                            break

                        close_i = pd_closes[i]
                        if close_i is None or close_i <= 0:
                            continue

                        dip_from_peak = (post_peak - close_i) / post_peak
                        if dip_from_peak < dip_threshold:
                            continue

                        # Must pass scanner on signal day
                        if pd_scanner_ids[i] is None:
                            continue

                        # Entry at next-day open (MOC)
                        entry_price = pd_opens[i]
                        entry_epoch = pd_next_epochs[i]
                        entry_volume = pd_next_volumes[i]

                        if entry_price is None or entry_price <= 0:
                            continue
                        if entry_epoch is None:
                            continue

                        # Regime filter
                        if use_regime and pd_epochs[i] not in bull_epochs:
                            continue

                        entry_candidates.append({
                            "instrument": inst_name,
                            "signal_epoch": pd_epochs[i],
                            "entry_epoch": entry_epoch,
                            "entry_price": entry_price,
                            "entry_volume": entry_volume or 0,
                            "post_peak": post_peak,
                            "dip_pct": dip_from_peak,
                            "scanner_config_ids": pd_scanner_ids[i],
                            "symbol": bare_symbol,
                        })
                        break  # One entry per earnings event

            print(f"  Entry candidates (post-filter): {len(entry_candidates)}")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                orders_this_config = 0

                for entry in entry_candidates:
                    inst = entry["instrument"]
                    symbol = entry["symbol"]
                    entry_epoch = entry["entry_epoch"]
                    entry_price = entry["entry_price"]
                    peak_price = entry["post_peak"]

                    # Fundamental filter
                    if fundamentals and (
                        roe_threshold > 0
                        or pe_threshold > 0
                        or de_threshold > 0
                    ):
                        if not passes_fundamental_filter(
                            fundamentals,
                            symbol,
                            entry["signal_epoch"],
                            roe_threshold,
                            pe_threshold,
                            de_threshold,
                            fundamental_missing,
                        ):
                            continue

                    if peak_price is None or peak_price <= entry_price:
                        continue

                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch, exit_price = walk_forward_exit(
                        ed["epochs"], ed["closes"], start_idx,
                        entry_epoch, entry_price, peak_price,
                        trailing_stop_pct, max_hold_days,
                    )

                    if exit_epoch is None or exit_price is None:
                        continue

                    all_order_rows.append({
                        "instrument": inst,
                        "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "entry_volume": entry["entry_volume"],
                        "exit_volume": 0,
                        "scanner_config_ids": entry["scanner_config_ids"],
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                    })
                    orders_this_config += 1

                print(
                    f"    Exit TSL={trailing_stop_pct * 100:.0f}% "
                    f"hold={max_hold_days}d: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "consecutive_positive_years": entry_cfg.get("consecutive_positive_years", [2]),
            "min_yearly_return_pct": entry_cfg.get("min_yearly_return_pct", [0]),
            "surprise_threshold_pct": entry_cfg.get("surprise_threshold_pct", [5]),
            "dip_threshold_pct": entry_cfg.get("dip_threshold_pct", [5]),
            "post_earnings_window": entry_cfg.get("post_earnings_window", [20]),
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
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [504]),
        }

register_strategy("earnings_dip", EarningsDipSignalGenerator)

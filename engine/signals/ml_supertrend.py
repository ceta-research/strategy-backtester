"""ML SuperTrend + Quality Dip-Buy signal generator.

Combines two alpha sources:
1. Quality universe: stocks with N+/10 positive annual returns (proven compounders)
2. Mild dip detection: stock fell 10%+ from peak but dip is shallow relative to
   its own historical oscillation range (near-peak quartile, not trough)
3. Entry trigger: SuperTrend flip to bullish on the individual stock
   (or simple momentum bounce if supertrend_mode=off)

Key data insight (SQL analysis, 2017-2022 on 144 NSE quality stocks):
- Stocks near their oscillation peak (mild dip) with momentum: 87.8% avg 252d return, 74% win rate
- Stocks near their oscillation trough (deep dip) with momentum: 13.2% avg 252d return, 38% win rate
- Broad market regime filter (NIFTY>SMA200) insufficient -- need stock-level confirmation
- SuperTrend flip on individual stock provides the stock-level trend confirmation

Exit: Trailing stop-loss or SuperTrend flip to bearish, whichever comes first.

Source: TradingView "Machine Learning Supertrend [Aslan]" by Zimord.
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
)

TRADING_DAYS_PER_YEAR = 252


def _compute_supertrend(df: pl.DataFrame, atr_period: int, multiplier: float) -> pl.DataFrame:
    """Compute SuperTrend bands and trend direction per instrument.

    Uses RMA-smoothed ATR (Wilder's smoothing), matching Pine Script exactly.

    Returns df with added columns:
      - st_trend: 1 (bullish) or -1 (bearish)
      - st_upper: support band (valid when bullish)
      - st_lower: resistance band (valid when bearish)
      - st_flip_bull: True on the bar where trend flips from -1 to 1
      - st_flip_bear: True on the bar where trend flips from 1 to -1
    """
    df = df.sort(["instrument", "date_epoch"])

    # True Range
    df = df.with_columns(
        pl.col("close").shift(1).over("instrument").alias("_prev_close")
    )
    df = df.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("_prev_close")).abs(),
            (pl.col("low") - pl.col("_prev_close")).abs(),
        ).alias("_tr")
    )

    # RMA (Wilder's smoothed ATR) = EWM with alpha = 1/period
    # Polars ewm_mean with span=2*period-1 gives alpha=2/(2*period-1+1)=1/period
    df = df.with_columns(
        pl.col("_tr")
        .ewm_mean(span=2 * atr_period - 1, adjust=False, min_samples=atr_period)
        .over("instrument")
        .alias("_atr")
    )

    # hlcc4 source (close-weighted average, Pine Script default)
    df = df.with_columns(
        ((pl.col("high") + pl.col("low") + pl.col("close") + pl.col("close")) / 4.0)
        .alias("_src")
    )

    # Raw bands
    df = df.with_columns([
        (pl.col("_src") - multiplier * pl.col("_atr")).alias("_raw_upper"),
        (pl.col("_src") + multiplier * pl.col("_atr")).alias("_raw_lower"),
    ])

    # SuperTrend requires sequential state (trend direction carries forward).
    # Must process per-instrument in Python for correctness.
    results = []
    for (inst,), group in df.group_by("instrument"):
        g = group.sort("date_epoch")
        closes = g["close"].to_list()
        raw_uppers = g["_raw_upper"].to_list()
        raw_lowers = g["_raw_lower"].to_list()
        n = len(closes)

        st_upper = [0.0] * n
        st_lower = [0.0] * n
        st_trend = [0] * n
        st_flip_bull = [False] * n
        st_flip_bear = [False] * n

        if n == 0:
            continue

        st_upper[0] = raw_uppers[0] if raw_uppers[0] is not None else 0.0
        st_lower[0] = raw_lowers[0] if raw_lowers[0] is not None else 0.0
        st_trend[0] = 1

        for i in range(1, n):
            ru = raw_uppers[i]
            rl = raw_lowers[i]
            c = closes[i]
            pc = closes[i - 1]

            if ru is None or rl is None or c is None or pc is None:
                st_upper[i] = st_upper[i - 1]
                st_lower[i] = st_lower[i - 1]
                st_trend[i] = st_trend[i - 1]
                continue

            # Upper band (support): ratchets up in uptrend
            if pc > st_upper[i - 1]:
                st_upper[i] = max(ru, st_upper[i - 1])
            else:
                st_upper[i] = ru

            # Lower band (resistance): ratchets down in downtrend
            if pc < st_lower[i - 1]:
                st_lower[i] = min(rl, st_lower[i - 1])
            else:
                st_lower[i] = rl

            # Trend direction
            prev_trend = st_trend[i - 1]
            if prev_trend == -1 and c > st_lower[i - 1]:
                st_trend[i] = 1
                st_flip_bull[i] = True
            elif prev_trend == 1 and c < st_upper[i - 1]:
                st_trend[i] = -1
                st_flip_bear[i] = True
            else:
                st_trend[i] = prev_trend

        results.append(
            g.with_columns([
                pl.Series("st_trend", st_trend, dtype=pl.Int8),
                pl.Series("st_upper", st_upper, dtype=pl.Float64),
                pl.Series("st_lower", st_lower, dtype=pl.Float64),
                pl.Series("st_flip_bull", st_flip_bull, dtype=pl.Boolean),
                pl.Series("st_flip_bear", st_flip_bear, dtype=pl.Boolean),
            ])
        )

    df_out = pl.concat(results)
    df_out = df_out.drop(["_prev_close", "_tr", "_atr", "_src", "_raw_upper", "_raw_lower"])
    return df_out


def _compute_quality_years(df: pl.DataFrame, lookback_years: int, min_positive: int) -> pl.DataFrame:
    """Compute quality filter: stock must have min_positive out of lookback_years
    years with positive returns.

    Different from momentum_dip_quality which requires N CONSECUTIVE positive years.
    This uses N out of M years (e.g., 8 out of 10), which is more robust.

    Returns df with added column: is_quality (bool).
    """
    df = df.sort(["instrument", "date_epoch"])

    yearly_return_cols = []
    for yr in range(lookback_years):
        shift_recent = yr * TRADING_DAYS_PER_YEAR
        shift_older = (yr + 1) * TRADING_DAYS_PER_YEAR
        col_name = f"_yr_ret_{yr + 1}"
        df = df.with_columns(
            (
                pl.col("close").shift(shift_recent).over("instrument")
                / pl.col("close").shift(shift_older).over("instrument")
                - 1.0
            ).alias(col_name)
        )
        yearly_return_cols.append(col_name)

    # Count positive years (not requiring consecutive)
    positive_count_expr = pl.lit(0)
    for col_name in yearly_return_cols:
        positive_count_expr = positive_count_expr + pl.when(
            pl.col(col_name) > 0
        ).then(1).otherwise(0)

    df = df.with_columns(positive_count_expr.alias("_positive_years"))
    df = df.with_columns(
        (pl.col("_positive_years") >= min_positive).alias("is_quality")
    )
    df = df.drop(yearly_return_cols + ["_positive_years"])
    return df


def _compute_oscillation_position(df: pl.DataFrame, peak_lookback: int) -> pl.DataFrame:
    """Compute oscillation position: where current drawdown sits relative to
    the stock's own historical maximum drawdown.

    osc_position = current_dd / typical_max_dd (both negative, so result is 0-1+)
    - 0.0 = at peak (no drawdown)
    - 0.25 = mild dip (25% of typical max drawdown)
    - 1.0 = at typical trough
    - >1.0 = worse than typical (unusual)

    Returns df with added columns:
      - rolling_peak: highest close in last peak_lookback days
      - dip_pct: (peak - close) / peak (positive when below peak)
      - typical_max_dd: worst drawdown in trailing 252 days (negative)
      - osc_position: current_dd / typical_max_dd (0 = peak, 1 = trough)
    """
    df = df.sort(["instrument", "date_epoch"])

    # Rolling peak (highest close, excludes current bar)
    df = df.with_columns(
        pl.col("close")
        .shift(1)
        .rolling_max(window_size=peak_lookback, min_samples=min(peak_lookback, 20))
        .over("instrument")
        .alias("rolling_peak")
    )

    # Dip percentage from peak
    df = df.with_columns(
        (
            (pl.col("rolling_peak") - pl.col("close"))
            / pl.col("rolling_peak")
        ).alias("dip_pct")
    )

    # Current drawdown ratio (negative)
    df = df.with_columns(
        (pl.col("close") / pl.col("rolling_peak") - 1.0).alias("_current_dd")
    )

    # Typical max drawdown: worst close/peak ratio in trailing 252 days
    df = df.with_columns(
        pl.col("_current_dd")
        .rolling_min(window_size=TRADING_DAYS_PER_YEAR, min_samples=63)
        .over("instrument")
        .alias("_typical_max_dd")
    )

    # Oscillation position: current_dd / typical_max_dd
    # Both are negative when below peak, so ratio is positive
    df = df.with_columns(
        pl.when(pl.col("_typical_max_dd") < -0.05)
        .then(pl.col("_current_dd") / pl.col("_typical_max_dd"))
        .otherwise(pl.lit(None))
        .alias("osc_position")
    )

    df = df.drop(["_current_dd", "_typical_max_dd"])
    return df


class MLSupertrendSignalGenerator:
    """Quality Mild-Dip + SuperTrend entry signal generator.

    Entry conditions (ALL must pass):
    1. Quality: stock has min_positive_years out of lookback_years positive annual returns
    2. Dip: stock is down dip_threshold_pct% from rolling peak
    3. Oscillation: dip is mild relative to stock's own history (osc_position < max_osc_position)
    4. Entry trigger (one of):
       a. SuperTrend flip to bullish (supertrend_mode != "off")
       b. Simple momentum bounce (mom_5d > bounce_threshold_pct) when supertrend_mode = "off"
    5. Scanner: stock passes liquidity filter on entry day

    Exit: TSL from rolling peak + max hold days via walk_forward_exit().
    """

    def generate_orders(self, context, df_tick_data):
        print("\n--- ML SuperTrend + Quality Dip Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get(
            "start_epoch", context["static_config"]["start_epoch"]
        )

        # ── Phase 1: Scanner (per-day liquidity filter) ──
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # ── Phase 2: Compute indicators on full data ──
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # ── Phase 3: Generate orders per entry/exit config ──
        t1 = time.time()
        all_order_rows = []

        # Pre-compute SuperTrend for all needed (atr_period, multiplier) combos
        st_cache = {}
        for entry_config in get_entry_config_iterator(context):
            if entry_config.get("supertrend_mode", "reversal") != "off":
                key = (entry_config["atr_period"], entry_config["atr_multiplier"])
                if key not in st_cache:
                    st_cache[key] = None  # placeholder

        print(f"  Computing SuperTrend for {len(st_cache)} parameter combos...")
        for (atr_period, atr_mult) in st_cache:
            st_df = _compute_supertrend(df_ind, atr_period, atr_mult)
            st_cache[(atr_period, atr_mult)] = st_df.select([
                "instrument", "date_epoch",
                "st_trend", "st_flip_bull", "st_flip_bear",
            ])
        st_elapsed = round(time.time() - t1, 2)
        print(f"  SuperTrend computed in {st_elapsed}s")

        t2 = time.time()
        for entry_config in get_entry_config_iterator(context):
            lookback_years = entry_config["lookback_years"]
            min_positive_years = entry_config["min_positive_years"]
            dip_threshold = entry_config["dip_threshold_pct"] / 100.0
            peak_lookback = entry_config["peak_lookback_days"]
            max_osc = entry_config["max_osc_position"]
            supertrend_mode = entry_config.get("supertrend_mode", "reversal")
            atr_period = entry_config.get("atr_period", 20)
            atr_mult = entry_config.get("atr_multiplier", 2.0)
            bounce_threshold = entry_config.get("bounce_threshold_pct", 2.0) / 100.0
            rescreen_days = entry_config.get("rescreen_interval_days", 63)

            # Compute quality + oscillation on base df
            df_signals = df_ind.clone()
            df_signals = _compute_quality_years(df_signals, lookback_years, min_positive_years)
            df_signals = _compute_oscillation_position(df_signals, peak_lookback)

            # 5-day momentum (for simple bounce mode or as supplementary)
            df_signals = df_signals.with_columns(
                (
                    pl.col("close")
                    / pl.col("close").shift(5).over("instrument")
                    - 1.0
                ).alias("mom_5d")
            )

            st_flip_lookback = entry_config.get("st_flip_lookback", 1)

            # Join SuperTrend signals if needed
            if supertrend_mode != "off":
                st_df = st_cache[(atr_period, atr_mult)]
                df_signals = df_signals.join(
                    st_df, on=["instrument", "date_epoch"], how="left"
                )

                # Add recent flip detection: did ST flip bullish within last N bars?
                if st_flip_lookback > 1:
                    df_signals = df_signals.sort(["instrument", "date_epoch"])
                    df_signals = df_signals.with_columns(
                        pl.col("st_flip_bull")
                        .cast(pl.Int8)
                        .rolling_max(window_size=st_flip_lookback, min_samples=1)
                        .over("instrument")
                        .cast(pl.Boolean)
                        .alias("st_recent_flip_bull")
                    )
                else:
                    df_signals = df_signals.with_columns(
                        pl.col("st_flip_bull").alias("st_recent_flip_bull")
                    )
            else:
                df_signals = df_signals.with_columns([
                    pl.lit(0).cast(pl.Int8).alias("st_trend"),
                    pl.lit(False).alias("st_flip_bull"),
                    pl.lit(False).alias("st_flip_bear"),
                    pl.lit(False).alias("st_recent_flip_bull"),
                ])

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
                    quality_universe[epoch] = quality_universe[last_screen_epoch]
                    continue
                day_data = df_signals.filter(
                    (pl.col("date_epoch") == epoch)
                    & (pl.col("scanner_config_ids").is_not_null())
                    & (pl.col("is_quality") == True)  # noqa: E712
                )
                quality_universe[epoch] = set(day_data["instrument"].to_list())
                last_screen_epoch = epoch

            pool_sizes = [len(v) for v in quality_universe.values() if v]
            avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0

            mode_str = supertrend_mode if supertrend_mode != "off" else "momentum"
            extras = [
                f"quality={min_positive_years}/{lookback_years}yr",
                f"dip>{dip_threshold * 100:.0f}%",
                f"osc<{max_osc}",
                f"mode={mode_str}",
            ]
            if supertrend_mode != "off":
                extras.append(f"ST({atr_period},{atr_mult})")
            else:
                extras.append(f"bounce>{bounce_threshold * 100:.0f}%")
            print(f"  Universe: avg {avg_pool:.0f} stocks ({', '.join(extras)})")

            # Build per-instrument exit data
            exit_data = {}
            for inst_tuple, group in df_signals.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                    "st_trends": g["st_trend"].to_list() if "st_trend" in g.columns else [],
                }

            # Entry filter
            base_filter = (
                (pl.col("is_quality") == True)  # noqa: E712
                & (pl.col("dip_pct") >= dip_threshold)
                & (pl.col("osc_position").is_not_null())
                & (pl.col("osc_position") <= max_osc)
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
                & (pl.col("rolling_peak").is_not_null())
            )

            if supertrend_mode == "reversal":
                # SuperTrend flipped bullish within last st_flip_lookback bars
                entry_filter = base_filter & (pl.col("st_recent_flip_bull") == True)  # noqa: E712
            elif supertrend_mode == "trend":
                # SuperTrend is currently bullish (trend = 1) + mom bounce
                entry_filter = base_filter & (pl.col("st_trend") == 1) & (pl.col("mom_5d") > bounce_threshold)
            elif supertrend_mode == "breakout":
                # SuperTrend is bullish (trend = 1)
                entry_filter = base_filter & (pl.col("st_trend") == 1)
            else:
                # Simple momentum bounce (no SuperTrend)
                entry_filter = base_filter & (pl.col("mom_5d") > bounce_threshold)

            entry_rows = (
                df_signals.filter(entry_filter)
                .select([
                    "instrument", "date_epoch", "next_epoch", "next_open",
                    "next_volume", "scanner_config_ids", "rolling_peak",
                ])
                .to_dicts()
            )

            print(f"  Entry candidates: {len(entry_rows)}")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                use_st_exit = exit_config.get("supertrend_exit", False)
                orders_this_config = 0

                for entry in entry_rows:
                    inst = entry["instrument"]
                    epoch = entry["date_epoch"]

                    # Must be in quality universe
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
                        # For dip entries, peak > entry price by definition
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    # Exit: walk forward with TSL + optional SuperTrend exit
                    if use_st_exit and ed["st_trends"]:
                        # Custom exit: exit on SuperTrend flip to bearish OR TSL
                        exit_epoch_val = None
                        exit_price_val = None
                        trail_high = entry_price
                        max_bars = max_hold_days if max_hold_days > 0 else 9999
                        entry_day_count = 0

                        for j in range(start_idx, len(ed["epochs"])):
                            c = ed["closes"][j]
                            if c is None:
                                continue
                            entry_day_count += 1

                            if c > trail_high:
                                trail_high = c

                            # TSL check
                            if trailing_stop_pct > 0 and c <= trail_high * (1 - trailing_stop_pct):
                                exit_epoch_val = ed["epochs"][j]
                                exit_price_val = c
                                break

                            # SuperTrend flip to bearish
                            st = ed["st_trends"][j] if j < len(ed["st_trends"]) else 1
                            if j > start_idx and st == -1:
                                exit_epoch_val = ed["epochs"][j]
                                exit_price_val = c
                                break

                            # Max hold
                            if entry_day_count >= max_bars:
                                exit_epoch_val = ed["epochs"][j]
                                exit_price_val = c
                                break

                        if exit_epoch_val is None and len(ed["epochs"]) > start_idx:
                            last = len(ed["epochs"]) - 1
                            exit_epoch_val = ed["epochs"][last]
                            exit_price_val = ed["closes"][last]
                    else:
                        # Standard walk-forward exit (peak recovery + TSL)
                        exit_epoch_val, exit_price_val = walk_forward_exit(
                            ed["epochs"], ed["closes"], start_idx,
                            entry_epoch, entry_price, peak_price,
                            trailing_stop_pct, max_hold_days,
                        )

                    if exit_epoch_val is None or exit_price_val is None:
                        continue

                    all_order_rows.append({
                        "instrument": inst,
                        "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch_val,
                        "entry_price": entry_price,
                        "exit_price": exit_price_val,
                        "entry_volume": entry["next_volume"] or 0,
                        "exit_volume": 0,
                        "scanner_config_ids": entry["scanner_config_ids"],
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                    })
                    orders_this_config += 1

                st_exit_str = "+ST_exit" if use_st_exit else ""
                print(
                    f"    Exit TSL={trailing_stop_pct * 100:.0f}%{st_exit_str} "
                    f"hold={max_hold_days}d: {orders_this_config} orders"
                )

        entry_elapsed = round(time.time() - t2, 2)
        return finalize_orders(all_order_rows, entry_elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "lookback_years": entry_cfg.get("lookback_years", [10]),
            "min_positive_years": entry_cfg.get("min_positive_years", [8]),
            "dip_threshold_pct": entry_cfg.get("dip_threshold_pct", [10]),
            "peak_lookback_days": entry_cfg.get("peak_lookback_days", [252]),
            "max_osc_position": entry_cfg.get("max_osc_position", [0.50]),
            "supertrend_mode": entry_cfg.get("supertrend_mode", ["reversal"]),
            "atr_period": entry_cfg.get("atr_period", [20]),
            "atr_multiplier": entry_cfg.get("atr_multiplier", [2.0]),
            "bounce_threshold_pct": entry_cfg.get("bounce_threshold_pct", [2.0]),
            "st_flip_lookback": entry_cfg.get("st_flip_lookback", [1]),
            "rescreen_interval_days": entry_cfg.get("rescreen_interval_days", [63]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [10]),
            "max_hold_days": exit_cfg.get("max_hold_days", [252]),
            "supertrend_exit": exit_cfg.get("supertrend_exit", [False]),
        }

register_strategy("ml_supertrend", MLSupertrendSignalGenerator)

"""Breakout Pullback: buy the dip after a confirmed breakout.

Entry (ALL conditions on the signal day):
  1. Stock made an N-day high within the last M days (breakout confirmed)
  2. Stock has pulled back X% from that recent high (dip entry)
  3. Still above trend MA (uptrend intact)
  4. close > open (bullish reversal candle)
  5. Scanner pass (liquidity)
  6. Internal regime bullish (optional)

Exit:
  Trailing stop-loss from max price since entry, at next-day open.
  Optional force-exit on regime flip.
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
    finalize_orders,
    build_regime_filter,
)
from engine.exits import anomalous_drop
from engine.internal_regime import compute_internal_regime_epochs

SECONDS_IN_ONE_DAY = 86400
PRICE_DROP_THRESHOLD = 20.0


class BreakoutPullbackSignalGenerator:
    """Buy pullbacks in stocks that recently broke out."""

    def generate_orders(self, context, df_tick_data):
        print("\n--- Breakout Pullback Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Phase 1: Scanner
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # Phase 2: Indicators
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Pre-build regime caches
        regime_cache = {}
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0 and (ri, rp) not in regime_cache:
                regime_cache[(ri, rp)] = build_regime_filter(
                    df_tick_data, ri, rp
                )

        # Phase 3: Generate orders per config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            breakout_window = entry_config["breakout_window"]
            breakout_lookback = entry_config["breakout_lookback"]
            pullback_pct = entry_config["pullback_pct"]
            trend_ma_period = entry_config["trend_ma_period"]
            force_exit_on_regime_flip = entry_config.get(
                "force_exit_on_regime_flip", False
            )

            # Internal regime
            ir_sma = entry_config.get("internal_regime_sma_period", 0)
            ir_thr = entry_config.get("internal_regime_threshold", 0.5)

            # External regime
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_external = bool(bull_epochs)

            # Internal regime from scanner universe
            use_internal = ir_sma > 0
            if use_internal:
                ir_cache_key = ("int", ir_sma, ir_thr)
                if ir_cache_key not in regime_cache:
                    regime_cache[ir_cache_key] = compute_internal_regime_epochs(
                        df_trimmed, sma_period=ir_sma, threshold=ir_thr
                    )
                internal_bull = regime_cache[ir_cache_key]
                if use_external:
                    bull_epochs = bull_epochs & internal_bull
                else:
                    bull_epochs = internal_bull

            use_regime = bool(bull_epochs)

            df_signals = df_ind.clone()

            # Trend MA
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_mean(window_size=trend_ma_period, min_samples=1)
                .over("instrument")
                .alias("trend_ma")
            )

            # N-day rolling high (breakout reference)
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(window_size=breakout_window, min_samples=1)
                .over("instrument")
                .alias("n_day_high")
            )

            # Was there a breakout (close == n_day_high) in the last M days?
            # Mark each day where close == n_day_high as a breakout day.
            df_signals = df_signals.with_columns(
                (pl.col("close") >= pl.col("n_day_high")).cast(pl.Float64)
                .alias("is_breakout_day")
            )

            # Rolling sum of breakout days in last M days
            df_signals = df_signals.with_columns(
                pl.col("is_breakout_day")
                .rolling_sum(window_size=breakout_lookback, min_samples=1)
                .over("instrument")
                .alias("recent_breakout_count")
            )

            # Rolling max close in last M days (the breakout high)
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(window_size=breakout_lookback, min_samples=1)
                .over("instrument")
                .alias("recent_high")
            )

            # Pullback % from the recent high
            df_signals = df_signals.with_columns(
                ((pl.col("recent_high") - pl.col("close"))
                 / pl.col("recent_high") * 100)
                .alias("pullback_from_high")
            )

            # Trim to sim range, merge scanner IDs
            df_signals = df_signals.filter(pl.col("date_epoch") >= start_epoch)
            df_signals = df_signals.with_columns(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":")
                 + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )
            scanner_ids_df = df_trimmed.select(
                ["uid", "scanner_config_ids"]
            ).unique(subset=["uid"])
            df_signals = df_signals.join(scanner_ids_df, on="uid", how="left")

            # Entry filter:
            # 1. Had a breakout in last M days (recent_breakout_count > 0)
            # 2. Pulled back at least X% from that high
            # 3. Still above trend MA
            # 4. Bullish candle (close > open)
            # 5. Scanner pass
            # 6. NOT currently at the high (we want the dip, not the breakout)
            entry_filter = (
                (pl.col("recent_breakout_count") > 0)
                & (pl.col("pullback_from_high") >= pullback_pct)
                & (pl.col("close") > pl.col("trend_ma"))
                & (pl.col("close") > pl.col("open"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
            )
            if use_regime:
                entry_filter = entry_filter & pl.col("date_epoch").is_in(
                    list(bull_epochs)
                )

            entry_rows = (
                df_signals.filter(entry_filter)
                .select([
                    "instrument", "date_epoch", "next_epoch", "next_open",
                    "next_volume", "scanner_config_ids",
                ])
                .to_dicts()
            )

            regime_str = ""
            if use_internal:
                regime_str = f", regime=internal(SMA{ir_sma},thr={ir_thr})"
            elif use_regime:
                regime_str = f", regime={regime_instrument}>SMA{regime_sma_period}"

            print(f"  Entry candidates: {len(entry_rows)} "
                  f"(bo_win={breakout_window}, bo_look={breakout_lookback}, "
                  f"pb={pullback_pct}%, trend_ma={trend_ma_period}"
                  f"{regime_str})")

            # Build exit data
            exit_data = {}
            for (inst_name,), group in df_signals.group_by("instrument", maintain_order=True):
                g = group.sort("date_epoch")
                exit_data[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "closes": g["close"].to_list(),
                    "opens": g["open"].to_list(),
                    "next_opens": g["next_open"].to_list(),
                    "next_epochs": g["next_epoch"].to_list(),
                    "next_volumes": g["next_volume"].to_list(),
                }

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"]
                min_hold_days = exit_config.get("min_hold_time_days", 0)
                orders_this_config = 0

                for entry in entry_rows:
                    inst = entry["instrument"]
                    if inst not in exit_data:
                        continue

                    ed = exit_data[inst]
                    entry_epoch = entry["next_epoch"]
                    entry_price = entry["next_open"]

                    if entry_price is None or entry_price <= 0:
                        continue

                    try:
                        start_idx = ed["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    exit_epoch, exit_price, exit_reason = _walk_forward_tsl(
                        ed["epochs"], ed["closes"], ed["opens"],
                        ed["next_opens"], ed["next_epochs"],
                        start_idx, entry_epoch,
                        trailing_stop_pct, min_hold_days,
                        bull_epochs=bull_epochs if (
                            use_regime and force_exit_on_regime_flip
                        ) else None,
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
                        "exit_reason": exit_reason,
                    })
                    orders_this_config += 1

                print(f"    Exit TSL={trailing_stop_pct}% min_hold={min_hold_days}d: "
                      f"{orders_this_config} orders")

        elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "breakout_window": entry_cfg.get("breakout_window", [20]),
            "breakout_lookback": entry_cfg.get("breakout_lookback", [10]),
            "pullback_pct": entry_cfg.get("pullback_pct", [5]),
            "trend_ma_period": entry_cfg.get("trend_ma_period", [50]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
            "force_exit_on_regime_flip": entry_cfg.get(
                "force_exit_on_regime_flip", [False]
            ),
            "internal_regime_sma_period": entry_cfg.get(
                "internal_regime_sma_period", [0]
            ),
            "internal_regime_threshold": entry_cfg.get(
                "internal_regime_threshold", [0.5]
            ),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [8]),
        }


def _walk_forward_tsl(epochs, closes, opens, next_opens, next_epochs,
                      start_idx, entry_epoch, trailing_stop_pct, min_hold_days,
                      bull_epochs=None):
    """Walk forward from entry to find TSL exit."""
    max_price = closes[start_idx] if closes[start_idx] is not None else 0
    last_close = max_price
    last_epoch = epochs[-1]

    for j in range(start_idx, len(epochs)):
        c = closes[j]
        if c is None:
            continue

        decision = anomalous_drop(c, last_close, PRICE_DROP_THRESHOLD, epochs[j])
        if decision is not None:
            return decision.exit_epoch, decision.exit_price, "anomalous_drop"

        max_price = max(max_price, c)
        hold_days = (epochs[j] - entry_epoch) / SECONDS_IN_ONE_DAY

        if epochs[j] == last_epoch:
            return epochs[j], c, "end_of_data"

        if hold_days < min_hold_days:
            last_close = c
            continue

        # Regime flip
        if bull_epochs is not None and epochs[j] not in bull_epochs:
            if j + 1 < len(epochs):
                next_open = next_opens[j]
                next_ep = next_epochs[j]
                if next_open is not None and next_open > 0 and next_ep is not None:
                    return next_ep, next_open, "regime_flip"
            return epochs[j], c, "regime_flip"

        # TSL
        if max_price > 0:
            drawdown_pct = (max_price - c) / max_price * 100
            if drawdown_pct > trailing_stop_pct:
                if j + 1 < len(epochs):
                    next_open = next_opens[j]
                    next_ep = next_epochs[j]
                    if next_open is not None and next_open > 0 and next_ep is not None:
                        return next_ep, next_open, "trailing_stop"
                return epochs[j], c, "trailing_stop"

        last_close = c

    if len(epochs) > start_idx:
        return epochs[-1], closes[-1], "end_of_data"
    return None, None, None


register_strategy("breakout_pullback", BreakoutPullbackSignalGenerator)

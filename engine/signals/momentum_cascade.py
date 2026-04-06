"""Momentum Cascade signal generator for engine pipeline.

Signal: Buy stocks with ACCELERATING momentum + breakout confirmation.
1. Slow momentum (126d) above min_momentum threshold
2. Fast momentum (fast_lookback_days) positive
3. Acceleration (fast_mom_now - fast_mom_lagged) > accel_threshold
4. New 63d high (breakout confirmation)

Exit: TSL after reaching peak_price (signal-day close), or max_hold_days.

MOC execution: signal at close[i], enter at open[i+1].
"""

import time

import polars as pl

from engine.config_loader import (
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import register_strategy, add_next_day_values


def _build_regime_filter(df_tick_data, regime_instrument, regime_sma_period):
    """Build set of bullish epochs where instrument > SMA."""
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
    print(f"  Regime: {regime_instrument} > SMA({regime_sma_period}), "
          f"{len(bull_epochs)}/{total} bullish ({pct:.0f}%)")
    return bull_epochs


class MomentumCascadeSignalGenerator:
    """Buy stocks with accelerating momentum + breakout confirmation."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Momentum Cascade Signal Generation ---")
        t0 = time.time()

        df = df_tick_data.clone()
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Scanner phase: liquidity filter
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
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") +
                 pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )["uid"]
            shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

        # Build scanner_config_ids mapping
        df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
        df_trimmed = df_trimmed.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") +
             pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )
        signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
        uids = df_trimmed["uid"].to_list()
        uid_to_signals = {}
        for uid in uids:
            signals = [str(k) for k, v in signal_sets.items() if uid in v]
            uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
        df_trimmed = df_trimmed.with_columns(
            pl.Series("scanner_config_ids",
                       [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
        )

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df_trimmed.height} rows")

        # Add next-day values for MOC execution
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Build per-instrument data for signal computation + exit walk
        inst_data = {}
        for inst_tuple, group in df_ind.group_by("instrument"):
            inst_name = inst_tuple[0]
            g = group.sort("date_epoch")
            inst_data[inst_name] = {
                "epochs": g["date_epoch"].to_list(),
                "closes": g["close"].to_list(),
                "next_epochs": g["next_epoch"].to_list(),
                "next_opens": g["next_open"].to_list(),
                "next_volumes": g["next_volume"].to_list(),
            }

        # Pre-build regime filters
        regime_cache = {}
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0 and (ri, rp) not in regime_cache:
                regime_cache[(ri, rp)] = _build_regime_filter(df_tick_data, ri, rp)

        # Build scanner pass set for quick lookup
        scanner_pass = set()
        for uid, sids in uid_to_signals.items():
            if sids:
                scanner_pass.add(uid)

        # Generate orders
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            fast_lb = entry_config["fast_lookback_days"]
            slow_lb = entry_config["slow_lookback_days"]
            accel_thresh = entry_config["accel_threshold_pct"] / 100.0
            min_mom = entry_config["min_momentum_pct"] / 100.0
            breakout_window = entry_config.get("breakout_window", 63)

            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            bull_epochs = regime_cache.get((ri, rp), set())
            use_regime = bool(bull_epochs)

            lookback_needed = max(slow_lb, fast_lb * 2, breakout_window) + 10
            entry_signals = []

            for inst_name, d in inst_data.items():
                closes = d["closes"]
                epochs = d["epochs"]
                next_epochs = d["next_epochs"]
                next_opens = d["next_opens"]
                next_volumes = d["next_volumes"]
                n = len(closes)

                if n < lookback_needed + 2:
                    continue

                for i in range(lookback_needed, n):
                    ep = epochs[i]
                    if ep < start_epoch:
                        continue

                    # Scanner check
                    uid = f"{inst_name}:{ep}"
                    if uid not in scanner_pass:
                        continue

                    # Regime check
                    if use_regime and ep not in bull_epochs:
                        continue

                    c = closes[i]
                    if c is None or c <= 0:
                        continue

                    # Slow momentum
                    past_slow = closes[i - slow_lb]
                    if past_slow is None or past_slow <= 0:
                        continue
                    slow_mom = (c - past_slow) / past_slow
                    if slow_mom < min_mom:
                        continue

                    # Fast momentum now
                    past_fast = closes[i - fast_lb]
                    if past_fast is None or past_fast <= 0:
                        continue
                    fast_mom_now = (c - past_fast) / past_fast

                    # Fast momentum lagged
                    if i - fast_lb < fast_lb:
                        continue
                    past_fast_ago = closes[i - 2 * fast_lb]
                    if past_fast_ago is None or past_fast_ago <= 0:
                        continue
                    fast_mom_ago = (closes[i - fast_lb] - past_fast_ago) / past_fast_ago

                    # Acceleration
                    acceleration = fast_mom_now - fast_mom_ago
                    if acceleration < accel_thresh:
                        continue

                    # Breakout: new N-day high
                    window_start = max(0, i - breakout_window)
                    window_high = max(
                        x for x in closes[window_start:i] if x is not None
                    )
                    if c <= window_high:
                        continue

                    # MOC: enter at next day's open
                    next_ep = next_epochs[i]
                    next_op = next_opens[i]
                    next_vol = next_volumes[i]
                    if next_ep is None or next_op is None or next_op <= 0:
                        continue

                    scanner_ids = uid_to_signals.get(uid, "0")

                    entry_signals.append({
                        "instrument": inst_name,
                        "signal_epoch": ep,
                        "entry_epoch": next_ep,
                        "entry_price": next_op,
                        "entry_volume": next_vol or 0,
                        "peak_price": c,
                        "acceleration": acceleration,
                        "scanner_config_ids": scanner_ids,
                    })

            print(f"  Entry[{entry_config['id']}] f={fast_lb}d a>{accel_thresh*100:.0f}% "
                  f"m>{min_mom*100:.0f}%: {len(entry_signals)} signals")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                tsl_pct = exit_config["tsl_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]

                for entry in entry_signals:
                    inst = entry["instrument"]
                    if inst not in inst_data:
                        continue

                    d = inst_data[inst]
                    entry_epoch = entry["entry_epoch"]
                    entry_price = entry["entry_price"]
                    peak_price = entry["peak_price"]

                    try:
                        start_idx = d["epochs"].index(entry_epoch)
                    except ValueError:
                        continue

                    # Walk forward to find TSL exit
                    exit_epoch = None
                    exit_price = None
                    trail_high = entry_price
                    reached_peak = False

                    for j in range(start_idx, len(d["epochs"])):
                        c = d["closes"][j]
                        if c is None:
                            continue
                        if c > trail_high:
                            trail_high = c
                        hold_days = (d["epochs"][j] - entry_epoch) / 86400
                        if max_hold_days > 0 and hold_days >= max_hold_days:
                            exit_epoch = d["epochs"][j]
                            exit_price = c
                            break
                        if c >= peak_price:
                            reached_peak = True
                        if reached_peak and tsl_pct > 0 and c <= trail_high * (1 - tsl_pct):
                            exit_epoch = d["epochs"][j]
                            exit_price = c
                            break

                    if exit_epoch is None and len(d["epochs"]) > start_idx:
                        last_idx = len(d["epochs"]) - 1
                        exit_epoch = d["epochs"][last_idx]
                        exit_price = d["closes"][last_idx]

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

        entry_elapsed = round(time.time() - t1, 2)

        if not all_order_rows:
            print(f"  Signal gen: {entry_elapsed}s, 0 orders")
            column_order = [
                "instrument", "entry_epoch", "exit_epoch",
                "entry_price", "exit_price", "entry_volume", "exit_volume",
                "scanner_config_ids", "entry_config_ids", "exit_config_ids",
            ]
            return pl.DataFrame(schema={
                c: pl.Utf8 if c in ("instrument", "scanner_config_ids",
                                     "entry_config_ids", "exit_config_ids")
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


register_strategy("momentum_cascade", MomentumCascadeSignalGenerator)

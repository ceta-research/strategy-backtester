"""EOD Breakout: N-day high breakout with direction score filter.

Ported from ATO_Simulator/simulator/steps/order_generation_step/process_step.py.
This is the original ATO_Simulator strategy.

Entry (ALL conditions on the signal day):
  1. close > N-day moving average (trend confirmation)
  2. close >= N-day rolling high (breakout)
  3. close > open (bullish candle)
  4. direction_score > threshold (market breadth: fraction of stocks above their MA)
  5. Scanner pass (liquidity)

Exit:
  Trailing stop-loss from max price since entry, at next-day open (MOC).
  Optional min_hold_days before TSL activates.
  Price gap >20% triggers forced exit at 80% of last close.
"""

import math
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
PRICE_DROP_THRESHOLD = 20.0  # % — forced exit on gap


class EodBreakoutSignalGenerator:
    """N-day high breakout with market breadth filter and TSL exit."""

    def generate_orders(self, context, df_tick_data):
        print("\n--- EOD Breakout Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Audit hooks (Phase 2b inspection drill, 2026-04-28). When
        # audit_mode is False (default), all hook bodies are skipped and the
        # code path is byte-identical to the pre-hook version. The collector
        # is a dict supplied by the audit run wrapper; hooks append polars
        # DataFrames / dicts into well-known keys.
        audit_mode = bool(context.get("audit_mode", False))
        audit_collector = context.get("audit_collector") if audit_mode else None

        # Phase 1: Scanner (shared per-day liquidity filter)
        shortlist_tracker, df_trimmed = run_scanner(context, df_tick_data)

        # HOOK 1: post-scanner candidate snapshot. df_trimmed has
        # `scanner_config_ids` set on rows that passed the scanner; rows with
        # NULL `scanner_config_ids` are scanner-rejected. Captures both for
        # inspection.
        if audit_mode and audit_collector is not None:
            audit_collector.setdefault("scanner_snapshots", []).append(
                df_trimmed
                .select(["instrument", "date_epoch", "scanner_config_ids"])
                .with_columns(
                    pl.col("scanner_config_ids").is_not_null().alias("scanner_pass")
                )
            )

        # Phase 2: Compute indicators on full data range
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])

        # Pre-build regime filters for all (instrument, period) combos
        # used across entry configs. Empty tuple means disabled.
        regime_cache = {}
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0 and (ri, rp) not in regime_cache:
                regime_cache[(ri, rp)] = build_regime_filter(
                    df_tick_data, ri, rp
                )

        # Phase 3: Generate orders per entry/exit config
        t1 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            n_day_ma_window = entry_config["n_day_ma"]
            n_day_high_window = entry_config["n_day_high"]
            ds_config = entry_config["direction_score"]
            ds_n_day_ma = ds_config["n_day_ma"]
            ds_threshold = ds_config["score"]
            regime_instrument = entry_config.get("regime_instrument", "")
            regime_sma_period = entry_config.get("regime_sma_period", 0)
            force_exit_on_regime_flip = entry_config.get(
                "force_exit_on_regime_flip", False
            )
            # Universe-level vol filter (NEW 2026-04-28 pt3). Sentinel >= 500
            # means filter disabled — when disabled, NO vol computation is done
            # and the entry_filter is byte-identical to pre-change behavior.
            max_stock_vol_pct = entry_config.get("max_stock_vol_pct", 999)
            vol_lookback_days = entry_config.get("vol_lookback_days", 60)
            vol_filter_active = max_stock_vol_pct < 500
            if vol_filter_active and vol_lookback_days < 2:
                raise ValueError(
                    f"vol_lookback_days must be >= 2 when filter active; "
                    f"got {vol_lookback_days}"
                )

            # Internal regime params (scanner-universe breadth)
            ir_sma = entry_config.get("internal_regime_sma_period", 0)
            ir_thr = entry_config.get("internal_regime_threshold", 0.5)

            # Separate exit regime params (hybrid mode). When set, force-exit
            # uses exit_regime (e.g. NIFTYBEES) while entry uses internal.
            exit_regime_instrument = entry_config.get(
                "exit_regime_instrument", "")
            exit_regime_sma_period = entry_config.get(
                "exit_regime_sma_period", 0)

            bull_epochs = regime_cache.get(
                (regime_instrument, regime_sma_period), set()
            )
            use_external = bool(bull_epochs)

            # Internal regime: compute from scanner-passed universe
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

            # Hybrid: separate exit regime for force-exit
            exit_bull_epochs = bull_epochs  # default: same as entry
            if exit_regime_instrument and exit_regime_sma_period > 0:
                exit_key = (exit_regime_instrument, exit_regime_sma_period)
                if exit_key not in regime_cache:
                    regime_cache[exit_key] = build_regime_filter(
                        df_tick_data, exit_regime_instrument,
                        exit_regime_sma_period
                    )
                exit_bull_epochs = regime_cache[exit_key]

            df_signals = df_ind.clone()

            # N-day moving average of close
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_mean(window_size=n_day_ma_window, min_samples=1)
                .over("instrument")
                .alias("n_day_ma")
            )

            # N-day rolling high of close
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_max(window_size=n_day_high_window, min_samples=1)
                .over("instrument")
                .alias("n_day_high")
            )

            # Direction score: fraction of instruments above their N-day MA per day
            df_signals = df_signals.with_columns(
                pl.col("close")
                .rolling_mean(window_size=ds_n_day_ma, min_samples=1)
                .over("instrument")
                .alias("ds_ma")
            )
            df_signals = df_signals.with_columns(
                pl.when(pl.col("close") > pl.col("ds_ma"))
                .then(1.0)
                .otherwise(0.0)
                .alias("above_ma")
            )
            # Aggregate per date_epoch: mean of above_ma across all instruments
            direction_scores = (
                df_signals.group_by("date_epoch", maintain_order=True)
                .agg(pl.col("above_ma").mean().alias("direction_score"))
            )
            df_signals = df_signals.join(direction_scores, on="date_epoch", how="left")

            # Optional: trailing-window annualized vol filter (universe-level).
            # Computes per-instrument rolling std of simple daily returns over
            # `vol_lookback_days`, annualized by sqrt(252). Stocks with
            # insufficient history get NULL vol and are excluded by the filter
            # clause below. Skipped entirely when filter is inactive — no
            # change to df_signals shape vs pre-change pipeline.
            if vol_filter_active:
                df_signals = df_signals.with_columns(
                    pl.col("close").pct_change().over("instrument").alias("_return_for_vol")
                ).with_columns(
                    (pl.col("_return_for_vol")
                     .rolling_std(
                         window_size=vol_lookback_days,
                         min_samples=vol_lookback_days,
                     )
                     .over("instrument") * math.sqrt(252)
                    ).alias("trailing_vol_annual")
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

            # Entry filter: breakout + bullish candle + direction score + scanner
            entry_filter = (
                (pl.col("close") > pl.col("n_day_ma"))
                & (pl.col("close") >= pl.col("n_day_high"))
                & (pl.col("close") > pl.col("open"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("direction_score") > ds_threshold)
                & (pl.col("next_epoch").is_not_null())
                & (pl.col("next_open").is_not_null())
            )
            if use_regime:
                entry_filter = entry_filter & pl.col("date_epoch").is_in(
                    list(bull_epochs)
                )
            if vol_filter_active:
                entry_filter = entry_filter & (
                    pl.col("trailing_vol_annual").is_not_null()
                    & (pl.col("trailing_vol_annual") < max_stock_vol_pct / 100)
                )

            # HOOK 2: per-clause flag emission. Mirrors the entry_filter
            # AND above as separate boolean columns. Behavior of the
            # filtered set is unchanged — this only computes auxiliary
            # columns and emits to the collector.
            if audit_mode and audit_collector is not None:
                clause_exprs = [
                    (pl.col("close") > pl.col("n_day_ma")).alias("clause_close_gt_ma"),
                    (pl.col("close") >= pl.col("n_day_high")).alias("clause_close_ge_ndhigh"),
                    (pl.col("close") > pl.col("open")).alias("clause_close_gt_open"),
                    pl.col("scanner_config_ids").is_not_null().alias("clause_scanner_pass"),
                    (pl.col("direction_score") > ds_threshold).alias("clause_ds_gt_thr"),
                    pl.col("next_epoch").is_not_null().alias("clause_next_epoch_present"),
                    pl.col("next_open").is_not_null().alias("clause_next_open_present"),
                ]
                clause_names = [
                    "clause_close_gt_ma", "clause_close_ge_ndhigh",
                    "clause_close_gt_open", "clause_scanner_pass",
                    "clause_ds_gt_thr", "clause_next_epoch_present",
                    "clause_next_open_present",
                ]
                if use_regime:
                    clause_exprs.append(
                        pl.col("date_epoch").is_in(list(bull_epochs))
                        .alias("clause_regime_bullish")
                    )
                    clause_names.append("clause_regime_bullish")
                if vol_filter_active:
                    clause_exprs.append(
                        (pl.col("trailing_vol_annual").is_not_null()
                         & (pl.col("trailing_vol_annual") < max_stock_vol_pct / 100))
                        .alias("clause_vol_below_cap")
                    )
                    clause_names.append("clause_vol_below_cap")
                df_clause = df_signals.with_columns(clause_exprs)
                all_pass_expr = pl.col(clause_names[0])
                for n in clause_names[1:]:
                    all_pass_expr = all_pass_expr & pl.col(n)
                df_clause = df_clause.with_columns(
                    all_pass_expr.alias("all_clauses_pass")
                )
                audit_collector.setdefault("entry_audits", []).append(
                    df_clause.select(
                        ["instrument", "date_epoch"] + clause_names + ["all_clauses_pass"]
                    )
                )

            select_cols = [
                "instrument", "date_epoch", "next_epoch", "next_open",
                "next_volume", "scanner_config_ids",
            ]
            if audit_mode:
                # At-entry context piggy-backed onto entry_rows for HOOK 3.
                select_cols += ["close", "n_day_high", "direction_score"]
            entry_rows = (
                df_signals.filter(entry_filter)
                .select(select_cols)
                .to_dicts()
            )

            if use_internal and use_external:
                regime_str = (f", regime={regime_instrument}>SMA{regime_sma_period}"
                              f"+internal(SMA{ir_sma},thr={ir_thr})")
            elif use_internal:
                regime_str = f", regime=internal(SMA{ir_sma},thr={ir_thr})"
            elif use_regime:
                regime_str = f", regime={regime_instrument}>SMA{regime_sma_period}"
            else:
                regime_str = ""
            vol_str = (
                f", vol<{max_stock_vol_pct}%@{vol_lookback_days}d"
                if vol_filter_active else ""
            )
            print(f"  Entry candidates: {len(entry_rows)} "
                  f"(n_day_high={n_day_high_window}, n_day_ma={n_day_ma_window}, "
                  f"ds_threshold={ds_threshold}{regime_str}{vol_str})")

            # Build per-instrument exit data for walk-forward
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

                    # Walk forward: TSL exit matching ATO_Simulator logic.
                    # When regime is active AND force_exit_on_regime_flip is
                    # set, also exit on the first bar where the regime turns
                    # bearish (epoch not in bull_epochs).
                    exit_epoch, exit_price, exit_reason = _walk_forward_tsl(
                        ed["epochs"], ed["closes"], ed["opens"],
                        ed["next_opens"], ed["next_epochs"],
                        start_idx, entry_epoch,
                        trailing_stop_pct, min_hold_days,
                        bull_epochs=exit_bull_epochs if (
                            use_regime and force_exit_on_regime_flip
                        ) else None,
                        entry_price=entry_price,
                        tsl_tighten_after_pct=exit_config.get(
                            "tsl_tighten_after_pct", 999.0),
                        tsl_tight_pct=exit_config.get("tsl_tight_pct", 0.0),
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
                        # Propagate exit_reason so simulator carries the real
                        # taxonomy (anomalous_drop / regime_flip / trailing_stop)
                        # instead of falling back to "natural". Phase 4
                        # (2026-04-28) — the audit's exit_reason was already
                        # informative; this just plumbs it through.
                        "exit_reason": exit_reason,
                    })
                    orders_this_config += 1

                    # HOOK 3: per-trade audit row with at-entry context
                    # and exit_reason.
                    if audit_mode and audit_collector is not None:
                        audit_collector.setdefault("trade_log_audits", []).append({
                            "instrument": inst,
                            "entry_epoch": entry_epoch,
                            "exit_epoch": exit_epoch,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "exit_reason": exit_reason,
                            "entry_close_signal": entry.get("close"),
                            "entry_n_day_high": entry.get("n_day_high"),
                            "entry_direction_score": entry.get("direction_score"),
                            "entry_regime_bullish": (
                                entry["date_epoch"] in bull_epochs
                                if use_regime else None
                            ),
                            "entry_config_id": str(entry_config["id"]),
                            "exit_config_id": str(exit_config["id"]),
                        })

                print(f"    Exit TSL={trailing_stop_pct}% min_hold={min_hold_days}d: "
                      f"{orders_this_config} orders")

        elapsed = round(time.time() - t1, 2)
        return finalize_orders(all_order_rows, elapsed)

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "n_day_ma": entry_cfg.get("n_day_ma", [3]),
            "n_day_high": entry_cfg.get("n_day_high", [2]),
            "direction_score": entry_cfg.get("direction_score", [
                {"n_day_ma": 3, "score": 0.54}
            ]),
            # Optional regime gate (entries only). Empty string / 0 = disabled.
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
            # When True AND regime is active, force-exit positions on first
            # bar where regime turns bearish (option ii).
            "force_exit_on_regime_flip": entry_cfg.get(
                "force_exit_on_regime_flip", [False]
            ),
            # Universe-level vol filter (NEW 2026-04-28 pt3). Sentinel >= 500
            # disables the filter; in that case no vol computation is done
            # and behavior is byte-identical to pre-change. Default 999 means
            # disabled, matching legacy configs.
            "max_stock_vol_pct": entry_cfg.get("max_stock_vol_pct", [999]),
            "vol_lookback_days": entry_cfg.get("vol_lookback_days", [60]),
            # Internal regime (scanner-universe breadth). 0 = disabled.
            "internal_regime_sma_period": entry_cfg.get(
                "internal_regime_sma_period", [0]
            ),
            "internal_regime_threshold": entry_cfg.get(
                "internal_regime_threshold", [0.5]
            ),
            # Hybrid: separate exit regime for force-exit. When set,
            # entry uses internal regime, force-exit uses this external.
            "exit_regime_instrument": entry_cfg.get(
                "exit_regime_instrument", [""]
            ),
            "exit_regime_sma_period": entry_cfg.get(
                "exit_regime_sma_period", [0]
            ),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [15]),
            # Phase 5 adaptive TSL: tighten once MFE > threshold.
            "tsl_tighten_after_pct": exit_cfg.get("tsl_tighten_after_pct", [999]),
            "tsl_tight_pct": exit_cfg.get("tsl_tight_pct", [0]),
        }


def _walk_forward_tsl(epochs, closes, opens, next_opens, next_epochs,
                      start_idx, entry_epoch, trailing_stop_pct, min_hold_days,
                      bull_epochs=None,
                      entry_price=0.0,
                      tsl_tighten_after_pct=999.0,
                      tsl_tight_pct=0.0):
    """Walk forward from entry to find TSL exit, matching ATO_Simulator logic.

    TSL: exit at next-day open when drawdown from max price > trailing_stop_pct.
    Price gap >20%: forced exit at 80% of last close.
    Last bar: exit at close.

    If `bull_epochs` is provided (a set of epochs where the regime is bullish),
    positions are force-exited at next-day open on the first bar past
    `min_hold_days` where the epoch is NOT in bull_epochs (regime flipped
    bearish). This is option (ii) from the bias audit.

    Adaptive TSL (Phase 5, 2026-04-28): when MFE from `entry_price` exceeds
    `tsl_tighten_after_pct`%, the effective TSL tightens from
    `trailing_stop_pct` to `tsl_tight_pct`. Default 999 = disabled (byte-
    identical to legacy behavior).

    Returns (exit_epoch, exit_price, exit_reason) or (None, None, None).

    `exit_reason` is one of: "anomalous_drop", "end_of_data", "regime_flip",
    "trailing_stop". Always populated when a real exit is returned (Phase 2b
    audit hook; computing it has no behavioral effect on exit_epoch /
    exit_price).
    """
    max_price = closes[start_idx] if closes[start_idx] is not None else 0
    last_close = max_price
    last_epoch = epochs[-1]

    for j in range(start_idx, len(epochs)):
        c = closes[j]
        if c is None:
            continue

        # Signed downward-gap detection (P0 #8 fix: pre-fix, `abs(diff)`
        # triggered on positive gaps, booking losses on days the stock rallied).
        decision = anomalous_drop(c, last_close, PRICE_DROP_THRESHOLD, epochs[j])
        if decision is not None:
            return decision.exit_epoch, decision.exit_price, "anomalous_drop"

        max_price = max(max_price, c)
        hold_days = (epochs[j] - entry_epoch) / SECONDS_IN_ONE_DAY

        # Last bar: exit at close
        if epochs[j] == last_epoch:
            return epochs[j], c, "end_of_data"

        # Min hold time check
        if hold_days < min_hold_days:
            last_close = c
            continue

        # Regime flip check: exit if regime turned bearish past min_hold.
        if bull_epochs is not None and epochs[j] not in bull_epochs:
            if j + 1 < len(epochs):
                next_open = next_opens[j]
                next_ep = next_epochs[j]
                if next_open is not None and next_open > 0 and next_ep is not None:
                    return next_ep, next_open, "regime_flip"
            return epochs[j], c, "regime_flip"

        # TSL check — adaptive: tighten once MFE exceeds threshold.
        if max_price > 0:
            effective_tsl = trailing_stop_pct
            if entry_price > 0 and tsl_tighten_after_pct < 999:
                mfe_from_entry_pct = (max_price - entry_price) / entry_price * 100
                if mfe_from_entry_pct > tsl_tighten_after_pct:
                    effective_tsl = tsl_tight_pct if tsl_tight_pct > 0 else trailing_stop_pct

            drawdown_pct = (max_price - c) / max_price * 100
            if drawdown_pct > effective_tsl:
                # Exit at next-day open
                if j + 1 < len(epochs):
                    next_open = next_opens[j]
                    next_ep = next_epochs[j]
                    if next_open is not None and next_open > 0 and next_ep is not None:
                        return next_ep, next_open, "trailing_stop"
                return epochs[j], c, "trailing_stop"

        last_close = c

    # No exit trigger - exit at last bar close
    if len(epochs) > start_idx:
        return epochs[-1], closes[-1], "end_of_data"
    return None, None, None


register_strategy("eod_breakout", EodBreakoutSignalGenerator)

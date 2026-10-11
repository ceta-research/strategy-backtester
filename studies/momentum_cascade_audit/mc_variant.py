"""Study-local copy of engine/signals/momentum_cascade.py with audit knobs.

Registered as strategy_type "momentum_cascade_audit" so the real generator is never shadowed.
With KNOBS at their defaults it must reproduce momentum_cascade's trade list exactly (gate).

Knobs (set by the runner before run_pipeline; the engine's static config drops unknown keys):
  exit_fill     "close" (engine) | "next_open": fill TSL / max-hold exits at the next bar's open
  shuffle_seed  None | int: shuffle df_orders rows after the final sort (ranking tie-order test)
  order_log     None | path: write every order with whether its TSL ever armed and why it exited
  lag_liquidity False | True: liquidity cap uses the previous bar's 30-bar mean turnover
"""
import json
import random
import sys
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import engine.pipeline as _pipeline  # noqa: E402
import engine.utils as _utils  # noqa: E402
from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator  # noqa: E402
from engine.signals.base import register_strategy, run_scanner, add_next_day_values, build_regime_filter  # noqa: E402
from engine.signals.momentum_cascade import MomentumCascadeSignalGenerator  # noqa: E402

KNOBS = {"exit_fill": "close", "shuffle_seed": None, "order_log": None, "lag_liquidity": False}

_orig_stats = _utils.create_epoch_wise_instrument_stats


def _lagged_stats(df_tick_data: pl.DataFrame) -> dict:
    """Same as engine.utils.create_epoch_wise_instrument_stats, avg_txn shifted one bar back."""
    df = df_tick_data.select(["instrument", "date_epoch", "close", "volume", "average_price"])
    df = df.with_columns((pl.col("volume") * pl.col("average_price")).alias("avg_txn"))
    df = df.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("avg_txn").rolling_mean(window_size=30, min_samples=1).over("instrument")
        .shift(1).over("instrument").alias("avg_txn"))
    out, one_day = {}, 86400
    for inst, g in df.group_by("instrument", maintain_order=True):
        name = inst[0]
        dd = {e: {"close": c, "avg_txn": a}
              for e, c, a in zip(g["date_epoch"].to_list(), g["close"].to_list(), g["avg_txn"].to_list())}
        eps = g["date_epoch"].to_list()
        last = None
        for ep in range(min(eps), max(eps) + one_day, one_day):
            if ep in dd:
                last = dd[ep]
            elif last is not None:
                dd[ep] = last
        for ep, v in dd.items():
            out.setdefault(ep, {})[name] = v
    return out


def stats_for_run(df_tick_data):
    return _lagged_stats(df_tick_data) if KNOBS["lag_liquidity"] else _orig_stats(df_tick_data)


# pipeline imported the name directly; point it at the switch.
_pipeline.create_epoch_wise_instrument_stats = stats_for_run


class MomentumCascadeAudit(MomentumCascadeSignalGenerator):

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
        _, df_trimmed = run_scanner(context, df_tick_data)
        uid_to_signals = dict(zip(df_trimmed["uid"].to_list(), df_trimmed["scanner_config_ids"].to_list()))

        df_ind = add_next_day_values(df_tick_data.clone()).sort(["instrument", "date_epoch"])
        inst_data = {}
        for inst_tuple, group in df_ind.group_by("instrument", maintain_order=True):
            g = group.sort("date_epoch")
            inst_data[inst_tuple[0]] = {
                "epochs": g["date_epoch"].to_list(),
                "closes": g["close"].to_list(),
                "next_epochs": g["next_epoch"].to_list(),
                "next_opens": g["next_open"].to_list(),
                "next_volumes": g["next_volume"].to_list(),
            }

        regime_cache = {}
        for ec in get_entry_config_iterator(context):
            ri, rp = ec.get("regime_instrument", ""), ec.get("regime_sma_period", 0)
            if ri and rp > 0 and (ri, rp) not in regime_cache:
                regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)
        scanner_pass = {uid for uid, sids in uid_to_signals.items() if sids}

        all_order_rows, order_log = [], []
        for entry_config in get_entry_config_iterator(context):
            fast_lb = entry_config["fast_lookback_days"]
            slow_lb = entry_config["slow_lookback_days"]
            accel_thresh = entry_config["accel_threshold_pct"] / 100.0
            min_mom = entry_config["min_momentum_pct"] / 100.0
            breakout_window = entry_config.get("breakout_window", 63)
            bull_epochs = regime_cache.get((entry_config.get("regime_instrument", ""),
                                            entry_config.get("regime_sma_period", 0)), set())
            use_regime = bool(bull_epochs)
            lookback_needed = max(slow_lb, fast_lb * 2, breakout_window) + 10
            entry_signals = []

            for inst_name, d in inst_data.items():
                closes, epochs = d["closes"], d["epochs"]
                n = len(closes)
                if n < lookback_needed + 2:
                    continue
                for i in range(lookback_needed, n):
                    ep = epochs[i]
                    if ep < start_epoch:
                        continue
                    uid = f"{inst_name}:{ep}"
                    if uid not in scanner_pass:
                        continue
                    if use_regime and ep not in bull_epochs:
                        continue
                    c = closes[i]
                    if c is None or c <= 0:
                        continue
                    past_slow = closes[i - slow_lb]
                    if past_slow is None or past_slow <= 0:
                        continue
                    if (c - past_slow) / past_slow < min_mom:
                        continue
                    past_fast = closes[i - fast_lb]
                    if past_fast is None or past_fast <= 0:
                        continue
                    fast_mom_now = (c - past_fast) / past_fast
                    if i - fast_lb < fast_lb:
                        continue
                    past_fast_ago = closes[i - 2 * fast_lb]
                    if past_fast_ago is None or past_fast_ago <= 0:
                        continue
                    fast_mom_ago = (closes[i - fast_lb] - past_fast_ago) / past_fast_ago
                    acceleration = fast_mom_now - fast_mom_ago
                    if acceleration < accel_thresh:
                        continue
                    window_high = max(x for x in closes[max(0, i - breakout_window):i] if x is not None)
                    if c <= window_high:
                        continue
                    next_ep, next_op = d["next_epochs"][i], d["next_opens"][i]
                    if next_ep is None or next_op is None or next_op <= 0:
                        continue
                    entry_signals.append({
                        "instrument": inst_name, "signal_epoch": ep, "entry_epoch": next_ep,
                        "entry_price": next_op, "entry_volume": d["next_volumes"][i] or 0,
                        "peak_price": c, "acceleration": acceleration,
                        "scanner_config_ids": uid_to_signals.get(uid, "0"),
                    })

            for exit_config in get_exit_config_iterator(context):
                tsl = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                for entry in entry_signals:
                    d = inst_data.get(entry["instrument"])
                    if d is None:
                        continue
                    entry_epoch, entry_price, peak_price = entry["entry_epoch"], entry["entry_price"], entry["peak_price"]
                    try:
                        start_idx = d["epochs"].index(entry_epoch)
                    except ValueError:
                        continue
                    exit_epoch = exit_price = None
                    reason, trail_high, reached_peak = "eod", entry_price, False
                    for j in range(start_idx, len(d["epochs"])):
                        c = d["closes"][j]
                        if c is None:
                            continue
                        if c > trail_high:
                            trail_high = c
                        hold_days = (d["epochs"][j] - entry_epoch) / 86400
                        hit = None
                        if max_hold_days > 0 and hold_days >= max_hold_days:
                            hit = "max_hold"
                        else:
                            if c >= peak_price:
                                reached_peak = True
                            if reached_peak and tsl > 0 and c <= trail_high * (1 - tsl):
                                hit = "tsl"
                        if hit:
                            reason = hit
                            nxt_ep, nxt_op = d["next_epochs"][j], d["next_opens"][j]
                            if KNOBS["exit_fill"] == "next_open" and nxt_ep is not None and nxt_op and nxt_op > 0:
                                exit_epoch, exit_price = nxt_ep, nxt_op
                            else:
                                exit_epoch, exit_price = d["epochs"][j], c
                            break
                    if exit_epoch is None and len(d["epochs"]) > start_idx:
                        exit_epoch, exit_price = d["epochs"][-1], d["closes"][-1]
                    if exit_epoch is None or exit_price is None:
                        continue
                    all_order_rows.append({
                        "instrument": entry["instrument"], "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch, "entry_price": entry_price, "exit_price": exit_price,
                        "entry_volume": entry["entry_volume"], "exit_volume": 0,
                        "scanner_config_ids": entry["scanner_config_ids"],
                        "entry_config_ids": str(entry_config["id"]),
                        "exit_config_ids": str(exit_config["id"]),
                    })
                    order_log.append({"instrument": entry["instrument"], "signal_epoch": entry["signal_epoch"],
                                      "entry_epoch": entry_epoch, "exit_epoch": exit_epoch,
                                      "armed": reached_peak, "reason": reason})

        if KNOBS["order_log"]:
            with open(KNOBS["order_log"], "w") as f:
                json.dump(order_log, f)
        if not all_order_rows:
            return pl.DataFrame()
        df_orders = pl.DataFrame(all_order_rows).select([
            "instrument", "entry_epoch", "exit_epoch", "entry_price", "exit_price",
            "entry_volume", "exit_volume", "scanner_config_ids", "entry_config_ids", "exit_config_ids",
        ]).sort(["instrument", "entry_epoch", "exit_epoch"])
        if KNOBS["shuffle_seed"] is not None:
            idx = list(range(df_orders.height))
            random.Random(KNOBS["shuffle_seed"]).shuffle(idx)
            df_orders = df_orders[idx]
        print(f"  [audit] {df_orders.height} orders, knobs={KNOBS}")
        return df_orders


register_strategy("momentum_cascade_audit", MomentumCascadeAudit)

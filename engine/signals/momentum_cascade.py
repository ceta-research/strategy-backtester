"""Momentum Cascade signal generator for engine pipeline.

Signal: Buy stocks with ACCELERATING momentum + breakout confirmation.
1. Slow momentum (126d) above min_momentum threshold
2. Fast momentum (fast_lookback_days) positive
3. Acceleration (fast_mom_now - fast_mom_lagged) > accel_threshold
4. New 63d high (breakout confirmation)

Exit: TSL after reaching peak_price (signal-day close), or max_hold_days.

MOC execution: signal at close[i], enter at open[i+1].

The entry condition (`entry_signal`) and the exit walk (`walk_exit`) are module-level
functions so the live runner (scripts/momentum_cascade_daily.py) evaluates the LATEST bar
with the same code the backtest uses; the generator itself never sees that bar because
add_next_day_values drops it.

Exit knobs (2026-10-11, all default-off, byte-identical at defaults; see
studies/momentum_cascade_audit/PREREG.md):
  exit_fill         close | next_open   fill a close-triggered exit at the next bar's open
  stop_mode         close | gtt         gtt: level = high-water x (1-tsl) ratcheted after each
                                        close, fires if the next session's low <= level, fills
                                        at min(open, level)
  trail_from_entry  bool                the TSL is live from the entry bar (arm S1)
  initial_stop_pct  float               fixed floor below entry until the TSL arms (arm S2)
"""

import time

import polars as pl

from engine.config_loader import (
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import register_strategy, run_scanner, add_next_day_values, build_regime_filter


def build_inst_data(df_ind: pl.DataFrame) -> dict:
    """Per-instrument bar lists. df_ind must carry next_epoch/next_open/next_volume
    (add_next_day_values); a live caller that keeps the last bar passes nulls there."""
    inst_data = {}
    for inst_tuple, group in df_ind.group_by("instrument", maintain_order=True):
        g = group.sort("date_epoch")
        inst_data[inst_tuple[0]] = {
            "epochs": g["date_epoch"].to_list(),
            "closes": g["close"].to_list(),
            "opens": g["open"].to_list() if "open" in g.columns else None,
            "lows": g["low"].to_list() if "low" in g.columns else None,
            "next_epochs": g["next_epoch"].to_list(),
            "next_opens": g["next_open"].to_list(),
            "next_volumes": g["next_volume"].to_list(),
        }
    return inst_data


def entry_signal(closes, i, slow_lb, fast_lb, accel_thresh, min_mom, breakout_window):
    """Acceleration if bar i is an entry signal, else None. Reads closes[:i+1] only."""
    c = closes[i]
    if c is None or c <= 0:
        return None
    past_slow = closes[i - slow_lb]
    if past_slow is None or past_slow <= 0:
        return None
    if (c - past_slow) / past_slow < min_mom:
        return None
    past_fast = closes[i - fast_lb]
    if past_fast is None or past_fast <= 0:
        return None
    fast_mom_now = (c - past_fast) / past_fast
    if i - fast_lb < fast_lb:
        return None
    past_fast_ago = closes[i - 2 * fast_lb]
    if past_fast_ago is None or past_fast_ago <= 0:
        return None
    fast_mom_ago = (closes[i - fast_lb] - past_fast_ago) / past_fast_ago
    acceleration = fast_mom_now - fast_mom_ago
    if acceleration < accel_thresh:
        return None
    window_high = max(x for x in closes[max(0, i - breakout_window):i] if x is not None)
    if c <= window_high:
        return None
    return acceleration


def walk_exit(d, start_idx, entry_price, peak_price, trailing_stop_pct, max_hold_days,
              exit_fill="close", stop_mode="close", trail_from_entry=False, initial_stop_pct=0.0):
    """Walk bars from start_idx (the entry bar) and find the exit.

    Returns {"exit_epoch", "exit_price", "reason", "trail_high", "armed", "level"}.
    reason is "tsl" | "floor" | "max_hold" | None. If a close-triggered exit wants the next
    open and there is no next bar (live caller on the latest close), exit_epoch is None and
    reason is set: the sell is pending for tomorrow's open. `level` is the GTT level that
    would apply to the next session under stop_mode="gtt".
    """
    epochs, closes = d["epochs"], d["closes"]
    opens, lows = d.get("opens"), d.get("lows")
    entry_epoch = epochs[start_idx]
    trail_high = entry_price
    armed = bool(trail_from_entry)
    level = None
    res = {"exit_epoch": None, "exit_price": None, "reason": None}
    for j in range(start_idx, len(epochs)):
        c = closes[j]
        if c is None:
            continue
        if stop_mode == "gtt" and level is not None and j > start_idx and lows is not None:
            lo = lows[j]
            if lo is not None and lo <= level:
                op = opens[j] if opens is not None else None
                px = min(op, level) if op is not None and op > 0 else level
                res.update(exit_epoch=epochs[j], exit_price=px, reason="tsl")
                break
        if c > trail_high:
            trail_high = c
        hold_days = (epochs[j] - entry_epoch) / 86400
        hit = None
        if max_hold_days > 0 and hold_days >= max_hold_days:
            hit = "max_hold"
        else:
            if c >= peak_price:
                armed = True
            if stop_mode == "close":
                if armed and trailing_stop_pct > 0 and c <= trail_high * (1 - trailing_stop_pct):
                    hit = "tsl"
                elif not armed and initial_stop_pct > 0 and c <= entry_price * (1 - initial_stop_pct):
                    hit = "floor"
        if hit:
            if exit_fill == "next_open":
                nep, nop = d["next_epochs"][j], d["next_opens"][j]
                res["reason"] = hit
                if nep is not None and nop is not None and nop > 0:
                    res.update(exit_epoch=nep, exit_price=nop)
            else:
                res.update(exit_epoch=epochs[j], exit_price=c, reason=hit)
            break
        if stop_mode == "gtt":
            if armed and trailing_stop_pct > 0:
                lvl = trail_high * (1 - trailing_stop_pct)
            elif initial_stop_pct > 0:
                lvl = entry_price * (1 - initial_stop_pct)
            else:
                lvl = None
            if lvl is not None:
                level = lvl if level is None else max(level, lvl)   # ratchet: never lowers
    res.update(trail_high=trail_high, armed=armed, level=level)
    return res


class MomentumCascadeSignalGenerator:
    """Buy stocks with accelerating momentum + breakout confirmation."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Momentum Cascade Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

        # Scanner phase: liquidity filter
        _, df_trimmed = run_scanner(context, df_tick_data)
        uid_to_signals = dict(zip(df_trimmed["uid"].to_list(), df_trimmed["scanner_config_ids"].to_list()))

        # Add next-day values for MOC execution
        df_ind = df_tick_data.clone()
        df_ind = add_next_day_values(df_ind)
        df_ind = df_ind.sort(["instrument", "date_epoch"])
        inst_data = build_inst_data(df_ind)

        # Pre-build regime filters
        regime_cache = {}
        for entry_config in get_entry_config_iterator(context):
            ri = entry_config.get("regime_instrument", "")
            rp = entry_config.get("regime_sma_period", 0)
            if ri and rp > 0 and (ri, rp) not in regime_cache:
                regime_cache[(ri, rp)] = build_regime_filter(df_tick_data, ri, rp)

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

                    acceleration = entry_signal(closes, i, slow_lb, fast_lb, accel_thresh, min_mom, breakout_window)
                    if acceleration is None:
                        continue

                    # MOC: enter at next day's open
                    next_ep = d["next_epochs"][i]
                    next_op = d["next_opens"][i]
                    next_vol = d["next_volumes"][i]
                    if next_ep is None or next_op is None or next_op <= 0:
                        continue

                    scanner_ids = uid_to_signals.get(uid, "0")

                    entry_signals.append({
                        "instrument": inst_name,
                        "signal_epoch": ep,
                        "entry_epoch": next_ep,
                        "entry_price": next_op,
                        "entry_volume": next_vol or 0,
                        "peak_price": closes[i],
                        "acceleration": acceleration,
                        "scanner_config_ids": scanner_ids,
                    })

            print(f"  Entry[{entry_config['id']}] f={fast_lb}d a>{accel_thresh*100:.0f}% "
                  f"m>{min_mom*100:.0f}%: {len(entry_signals)} signals")

            # Walk forward for each exit config
            for exit_config in get_exit_config_iterator(context):
                trailing_stop_pct = exit_config["trailing_stop_pct"] / 100.0
                max_hold_days = exit_config["max_hold_days"]
                knobs = dict(
                    exit_fill=exit_config.get("exit_fill", "close"),
                    stop_mode=exit_config.get("stop_mode", "close"),
                    trail_from_entry=bool(exit_config.get("trail_from_entry", False)),
                    initial_stop_pct=float(exit_config.get("initial_stop_pct", 0) or 0) / 100.0,
                )

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

                    w = walk_exit(d, start_idx, entry_price, peak_price, trailing_stop_pct,
                                  max_hold_days, **knobs)
                    exit_epoch, exit_price = w["exit_epoch"], w["exit_price"]

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
                        # Propagated by the simulator into the trade log; "end_of_data" marks
                        # a position still open at the last bar (the live runner's book).
                        "exit_reason": w["reason"] or "end_of_data",
                    })

        entry_elapsed = round(time.time() - t1, 2)

        if not all_order_rows:
            print(f"  Signal gen: {entry_elapsed}s, 0 orders")
            column_order = [
                "instrument", "entry_epoch", "exit_epoch",
                "entry_price", "exit_price", "entry_volume", "exit_volume",
                "scanner_config_ids", "entry_config_ids", "exit_config_ids", "exit_reason",
            ]
            return pl.DataFrame(schema={
                c: pl.Utf8 if c in ("instrument", "scanner_config_ids",
                                     "entry_config_ids", "exit_config_ids", "exit_reason")
                else pl.Float64 for c in column_order
            })

        df_orders = pl.DataFrame(all_order_rows)
        df_orders = df_orders.select([
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids", "exit_reason",
        ]).sort(["instrument", "entry_epoch", "exit_epoch"])

        print(f"  Signal gen: {entry_elapsed}s, {df_orders.height} orders")
        return df_orders

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "fast_lookback_days": entry_cfg.get("fast_lookback_days", [42]),
            "slow_lookback_days": entry_cfg.get("slow_lookback_days", [126]),
            "accel_threshold_pct": entry_cfg.get("accel_threshold_pct", [2]),
            "min_momentum_pct": entry_cfg.get("min_momentum_pct", [20]),
            "breakout_window": entry_cfg.get("breakout_window", [63]),
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [12]),
            "max_hold_days": exit_cfg.get("max_hold_days", [504]),
            "exit_fill": exit_cfg.get("exit_fill", ["close"]),
            "stop_mode": exit_cfg.get("stop_mode", ["close"]),
            "trail_from_entry": exit_cfg.get("trail_from_entry", [False]),
            "initial_stop_pct": exit_cfg.get("initial_stop_pct", [0]),
        }

register_strategy("momentum_cascade", MomentumCascadeSignalGenerator)

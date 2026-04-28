"""EOD Technical signal generator.

Wraps the existing scanner + order_generator logic as a SignalGenerator.
This is the original strategy: MA crossover + n-day high + direction score
entry, trailing stop-loss exit.

Optional regime gate (mirrors eod_breakout, 2026-04-28):
  - regime_instrument + regime_sma_period: when the regime instrument is
    BELOW its N-day SMA, no new entries are allowed. Implemented by
    nullifying scanner_config_ids on off-regime signal days, which makes
    add_entry_signal_inplace's `scanner_config_ids.is_not_null()` check
    fail. Equivalent to eod_breakout.py:149-152.
  - force_exit_on_regime_flip: when True and regime is active, force-exit
    open positions at next-day open on the first bar (past min_hold_days)
    where the regime turned bearish. Mirrors eod_breakout's _walk_forward_tsl
    bull_epochs path (lines 313-321).

Fast path: when no entry config has regime enabled, behavior is byte-identical
to the pre-2026-04-28 wrapper (single scanner+order_generator pass).

Slow path: when any entry config has regime params, we run scanner+order_gen
once per entry config with a per-config gate. Cost scales linearly with the
number of entry configs in the run. Suitable for the 8-config Phase 2 regime
sweep (~2-3 min/run × 8 ≈ 20 min); not for sweeps with hundreds of configs.
"""

import copy
import time

import polars as pl

from engine import scanner, order_generator
from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.constants import SECONDS_IN_ONE_DAY
from engine.signals.base import register_strategy, build_regime_filter


def _entry_has_regime(entry_config: dict) -> bool:
    return bool(entry_config.get("regime_instrument", "")) and \
        entry_config.get("regime_sma_period", 0) > 0


class EodTechnicalSignalGenerator:
    """Original ATO_Simulator-ported EOD technical strategy."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        entry_configs = list(get_entry_config_iterator(context))
        any_regime = any(_entry_has_regime(ec) for ec in entry_configs)

        if not any_regime:
            return self._run_no_regime(context, df_tick_data)

        return self._run_per_config(context, df_tick_data, entry_configs)

    # ---------------------------------------------------------------------
    # Fast path (regime disabled everywhere)
    # ---------------------------------------------------------------------
    def _run_no_regime(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Scanner Step ---")
        t0 = time.time()
        df_scanned = scanner.process(context, df_tick_data)
        print(f"  Scanner: {round(time.time() - t0, 2)}s, {df_scanned.height} rows")

        print("\n--- Order Generation Step ---")
        t0 = time.time()
        df_orders = order_generator.process(context, df_scanned)
        print(f"  Order gen total: {round(time.time() - t0, 2)}s")
        return df_orders

    # ---------------------------------------------------------------------
    # Slow path (per-entry-config regime gating)
    # ---------------------------------------------------------------------
    def _run_per_config(self, context: dict, df_tick_data: pl.DataFrame,
                        entry_configs: list) -> pl.DataFrame:
        print(f"\n--- Regime mode: per-config order generation "
              f"({len(entry_configs)} entry configs) ---")

        bull_cache: dict = {}
        per_config_orders = []
        original_entry_input = context["entry_config_input"]

        try:
            for ec in entry_configs:
                ri = ec.get("regime_instrument", "")
                rp = ec.get("regime_sma_period", 0)
                fe = ec.get("force_exit_on_regime_flip", False)

                use_regime = bool(ri) and rp > 0
                if use_regime:
                    cache_key = (ri, rp)
                    if cache_key not in bull_cache:
                        bull_cache[cache_key] = build_regime_filter(
                            df_tick_data, ri, rp
                        )
                    bull_epochs = bull_cache[cache_key]
                    use_regime = bool(bull_epochs)
                else:
                    bull_epochs = set()

                # Override context with a single-config entry input. We pass
                # only the params that get_entry_config_iterator can re-emit
                # (each as a single-element list / single value).
                context["entry_config_input"] = self._single_entry_input(ec)

                # Scanner — unchanged universe filter.
                t0 = time.time()
                df_scanned = scanner.process(context, df_tick_data)

                # Apply regime entry gate by nullifying scanner_config_ids on
                # off-regime signal days. add_entry_signal_inplace's
                # `scanner_config_ids.is_not_null()` check then suppresses
                # entries on those days, mirroring eod_breakout.py:149-152.
                if use_regime:
                    bull_list = list(bull_epochs)
                    df_scanned = df_scanned.with_columns(
                        pl.when(pl.col("date_epoch").is_in(bull_list))
                        .then(pl.col("scanner_config_ids"))
                        .otherwise(pl.lit(None, dtype=pl.Utf8))
                        .alias("scanner_config_ids")
                    )

                df_orders = order_generator.process(context, df_scanned)
                t_total = round(time.time() - t0, 2)

                regime_str = (f" [regime={ri}>SMA{rp}, force_exit={fe}]"
                              if use_regime else " [regime=off]")
                original_id = ec.get("id", 1)
                print(f"  Config id={original_id}{regime_str}: "
                      f"{df_orders.height} orders ({t_total}s)")

                if use_regime and fe and df_orders.height > 0:
                    df_orders = self._apply_force_exit_on_flip(
                        df_orders, df_tick_data, bull_epochs, context
                    )

                # Relabel entry_config_ids to the ORIGINAL entry config's id.
                # Each per-config pass runs order_generator with a context that
                # contains only this single entry config, so it stamps id=1 on
                # every row. After concat, the simulator would see all rows as
                # belonging to config 1 (configs 2..N would receive 0 orders).
                # Rewriting to the original id restores correct dispatch.
                if df_orders.height > 0:
                    df_orders = df_orders.with_columns(
                        pl.lit(str(original_id)).alias("entry_config_ids")
                    )

                per_config_orders.append(df_orders)
        finally:
            context["entry_config_input"] = original_entry_input

        if not per_config_orders:
            return pl.DataFrame()

        non_empty = [d for d in per_config_orders if d.height > 0]
        if not non_empty:
            return per_config_orders[0]
        return pl.concat(non_empty, how="vertical_relaxed")

    @staticmethod
    def _single_entry_input(entry_config: dict) -> dict:
        """Build an entry_config_input dict that get_entry_config_iterator
        can expand into exactly this single materialized config.

        The iterator does Cartesian product over list-valued params, so we
        wrap each scalar in a single-element list. We also strip the
        synthetic 'id' key that the iterator adds during expansion.
        """
        out: dict = {}
        for k, v in entry_config.items():
            if k == "id":
                continue
            out[k] = [v]
        return out

    # ---------------------------------------------------------------------
    # Force-exit on regime flip (mirrors eod_breakout._walk_forward_tsl
    # bull_epochs branch)
    # ---------------------------------------------------------------------
    @staticmethod
    def _apply_force_exit_on_flip(df_orders: pl.DataFrame,
                                  df_tick_data: pl.DataFrame,
                                  bull_epochs: set,
                                  context: dict) -> pl.DataFrame:
        """For each order, if the regime turns bearish before the existing
        exit (and past min_hold_days), override exit to next-day open at the
        first off-regime bar.

        min_hold_days is read from the matching exit config (looked up by the
        order's exit_config_ids). For multi-config rows, the smallest
        min_hold_days across referenced configs is used (most permissive,
        matches the earliest a regime-flip exit could fire under any config).
        """
        if df_orders.height == 0:
            return df_orders

        # Build exit_config_id -> min_hold_days lookup
        min_hold_by_id: dict = {}
        for ec in get_exit_config_iterator(context):
            min_hold_by_id[str(ec["id"])] = ec.get("min_hold_time_days", 0)

        # Build per-instrument forward-walk arrays
        df_walk = df_tick_data.sort(["instrument", "date_epoch"]).with_columns([
            pl.col("date_epoch").shift(-1).over("instrument").alias("next_epoch_w"),
            pl.col("open").shift(-1).over("instrument").alias("next_open_w"),
            pl.col("volume").shift(-1).over("instrument").alias("next_volume_w"),
        ])

        by_inst: dict = {}
        for inst_tuple, group in df_walk.group_by("instrument", maintain_order=True):
            inst = inst_tuple[0]
            g = group.sort("date_epoch")
            by_inst[inst] = {
                "epochs": g["date_epoch"].to_list(),
                "closes": g["close"].to_list(),
                "next_opens": g["next_open_w"].to_list(),
                "next_epochs": g["next_epoch_w"].to_list(),
                "next_volumes": g["next_volume_w"].to_list(),
            }

        new_rows = []
        flip_count = 0
        for row in df_orders.to_dicts():
            inst = row["instrument"]
            ed = by_inst.get(inst)
            if ed is None:
                new_rows.append(row)
                continue

            try:
                start_idx = ed["epochs"].index(row["entry_epoch"])
            except ValueError:
                new_rows.append(row)
                continue

            # Resolve min_hold_days for this row.
            ids = str(row.get("exit_config_ids", "") or "").split(",")
            mh_candidates = [min_hold_by_id.get(i.strip(), 0) for i in ids if i.strip()]
            min_hold_days = min(mh_candidates) if mh_candidates else 0
            entry_epoch = row["entry_epoch"]
            existing_exit_epoch = row["exit_epoch"]

            flip_exit_epoch = None
            flip_exit_price = None
            flip_exit_volume = 0

            for j in range(start_idx, len(ed["epochs"])):
                ep = ed["epochs"][j]
                if existing_exit_epoch is not None and ep >= existing_exit_epoch:
                    break  # existing exit fires first

                hold_days = (ep - entry_epoch) / SECONDS_IN_ONE_DAY
                if hold_days < min_hold_days:
                    continue

                if ep in bull_epochs:
                    continue

                # Regime is OFF on this bar; exit at next-day open if
                # available, else current close (last bar).
                no = ed["next_opens"][j]
                ne = ed["next_epochs"][j]
                if (j + 1 < len(ed["epochs"]) and no is not None and no > 0
                        and ne is not None):
                    flip_exit_epoch = ne
                    flip_exit_price = no
                    flip_exit_volume = ed["next_volumes"][j] or 0
                else:
                    c = ed["closes"][j]
                    if c is not None:
                        flip_exit_epoch = ep
                        flip_exit_price = c
                        flip_exit_volume = 0
                break

            if (flip_exit_epoch is not None
                    and (existing_exit_epoch is None
                         or flip_exit_epoch < existing_exit_epoch)):
                row = copy.copy(row)
                row["exit_epoch"] = flip_exit_epoch
                row["exit_price"] = flip_exit_price
                row["exit_volume"] = flip_exit_volume
                row["exit_reason"] = "regime_flip"
                flip_count += 1

            new_rows.append(row)

        print(f"    Force-exit on regime flip: {flip_count} orders rerouted")

        # Preserve original column order/schema.
        out = pl.DataFrame(new_rows, schema=df_orders.schema)
        return out

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "n_day_ma": entry_cfg.get("n_day_ma", [3]),
            "n_day_high": entry_cfg.get("n_day_high", [2]),
            "direction_score": entry_cfg.get("direction_score", [
                {"n_day_ma": 3, "score": 0.54}
            ]),
            # Optional regime gate (entries only; exits via force_exit flag).
            # Empty string / 0 = disabled. Mirrors eod_breakout schema.
            "regime_instrument": entry_cfg.get("regime_instrument", [""]),
            "regime_sma_period": entry_cfg.get("regime_sma_period", [0]),
            "force_exit_on_regime_flip": entry_cfg.get(
                "force_exit_on_regime_flip", [False]
            ),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [15]),
            # Phase 5 adaptive TSL
            "tsl_tighten_after_pct": exit_cfg.get("tsl_tighten_after_pct", [999]),
            "tsl_tight_pct": exit_cfg.get("tsl_tight_pct", [0]),
        }


register_strategy("eod_technical", EodTechnicalSignalGenerator)

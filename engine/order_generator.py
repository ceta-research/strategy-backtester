"""Order generation: entry/exit signal computation.

Ported from ATO_Simulator/simulator/steps/order_generation_step/process_step.py.
Pseudo-future-epoch logic removed (not needed for in-memory pipeline).
"""

import copy
import time
from collections import defaultdict
from multiprocessing import Pool, cpu_count

import polars as pl

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.constants import SECONDS_IN_ONE_DAY
from engine.exits import (
    ExitTracker, anomalous_drop, end_of_data, trailing_stop, below_min_hold,
)


class OrderGenerationUtil:

    def __init__(self, df_tick_data: pl.DataFrame):
        self.df_tick_data = df_tick_data
        self.order_config_mapping = {}

    @staticmethod
    def add_direction_score(df_tick_data: pl.DataFrame, direction_score_config: dict) -> pl.DataFrame:
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])
        df_tick_data = df_tick_data.with_columns(
            pl.col("close")
            .rolling_mean(window_size=direction_score_config["n_day_ma"], min_samples=1)
            .over("instrument")
            .alias("direction_score_n_day_ma")
        )

        df_tick_data = df_tick_data.with_columns(
            pl.when(pl.col("close") > pl.col("direction_score_n_day_ma"))
            .then(1.0)
            .otherwise(0.0)
            .alias("direction_score")
        )

        df_direction = df_tick_data.group_by("date_epoch", maintain_order=True).agg(
            pl.col("direction_score").mean()
        )

        df_tick_data = df_tick_data.drop(["direction_score", "direction_score_n_day_ma"])
        df_tick_data = df_tick_data.join(df_direction, on="date_epoch", how="left")
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])
        return df_tick_data

    def add_entry_signal_inplace(self, df_tick_data: pl.DataFrame, entry_config: dict) -> pl.DataFrame:
        df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
        df_tick_data = self.add_direction_score(df_tick_data, entry_config["direction_score"])
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"]).with_columns([
            pl.col("close")
            .rolling_mean(window_size=entry_config["n_day_ma"], min_samples=1)
            .over("instrument")
            .alias("n_day_ma"),
            pl.col("close")
            .rolling_max(window_size=entry_config["n_day_high"], min_samples=1)
            .over("instrument")
            .alias("n_day_high"),
        ])

        df_tick_data = df_tick_data.with_columns(
            (
                (pl.col("close") > pl.col("n_day_ma"))
                & (pl.col("close") >= pl.col("n_day_high"))
                & (pl.col("close") > pl.col("open"))
                & (pl.col("scanner_config_ids").is_not_null())
                & (pl.col("direction_score") > entry_config["direction_score"]["score"])
            ).alias("can_enter")
        )
        return df_tick_data

    def update_config_order_map(self, df_tick_data: pl.DataFrame, entry_config_id: int):
        df_tick_data = df_tick_data.filter(pl.col("can_enter"))

        instruments = df_tick_data["instrument"].to_list()
        scanner_ids = df_tick_data["scanner_config_ids"].to_list()
        next_epochs = df_tick_data["next_epoch"].to_list()
        next_opens = df_tick_data["next_open"].to_list()
        next_volumes = df_tick_data["next_volume"].to_list()

        for instrument, scanner_config_ids, entry_epoch, entry_price, entry_volume in zip(
            instruments, scanner_ids, next_epochs, next_opens, next_volumes,
        ):
            if instrument not in self.order_config_mapping:
                self.order_config_mapping[instrument] = {}
            if entry_epoch not in self.order_config_mapping[instrument]:
                self.order_config_mapping[instrument][entry_epoch] = {
                    "entry_price": entry_price,
                    "entry_volume": entry_volume,
                    "scanner_config_ids": scanner_config_ids,
                    "entry_config_ids": f"{entry_config_id}",
                }
            else:
                prev_entry_config_ids = self.order_config_mapping[instrument][entry_epoch]["entry_config_ids"]
                self.order_config_mapping[instrument][entry_epoch][
                    "entry_config_ids"
                ] = f"{prev_entry_config_ids},{entry_config_id}"

    def generate_order_df(self) -> pl.DataFrame:
        """Convert nested order config mapping to DataFrame.

        After generate_exit_attributes, entries are:
            order_config_mapping[instrument][entry_epoch] = {exit_epoch: {attrs...}, ...}
        Entries that weren't processed by exit generation remain as flat dicts
        (keys are attr names, not exit epochs). Skip those.
        """
        rows = []
        for instrument, entry_data in self.order_config_mapping.items():
            for entry_epoch, exit_data in entry_data.items():
                if not isinstance(exit_data, dict):
                    continue
                for exit_epoch, config in exit_data.items():
                    if not isinstance(config, dict):
                        continue
                    rows.append({
                        "instrument": instrument,
                        "entry_epoch": entry_epoch,
                        "exit_epoch": exit_epoch,
                        **config,
                    })

        # Ship-blocker fix (code review 2026-04-21): `exit_reason` is set by
        # `_record_exit` onto the attrs dict, but was being projected away here
        # before df_orders reached the simulator. Now preserved.
        column_order = [
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids",
            "exit_reason",
        ]
        utf8_cols = {"instrument", "scanner_config_ids", "entry_config_ids",
                     "exit_config_ids", "exit_reason"}
        if not rows:
            return pl.DataFrame(schema={
                c: pl.Utf8 if c in utf8_cols else pl.Float64
                for c in column_order
            })
        df = pl.DataFrame(rows)
        # Some signal generators (walk_forward_exit path) don't emit
        # exit_reason. Fill with "natural" so the column is never null and
        # downstream code can rely on non-null strings.
        if "exit_reason" not in df.columns:
            df = df.with_columns(pl.lit("natural").alias("exit_reason"))
        else:
            df = df.with_columns(pl.col("exit_reason").fill_null("natural"))
        df = df.select(column_order)
        df = df.sort(["instrument", "entry_epoch", "exit_epoch"])
        return df

    def generate_exit_attributes(self, context: dict):
        """Compute exit prices/epochs for all instruments using multiprocessing."""
        instrument_tick_data_map = {}
        self.df_tick_data = self.df_tick_data.filter(
            pl.col("instrument").is_in(list(self.order_config_mapping.keys()))
        )

        for instrument_tuple, group in self.df_tick_data.group_by("instrument", maintain_order=True):
            instrument_name = instrument_tuple[0]
            instrument_tick_data_map[instrument_name] = group.sort("date_epoch")

        drop_threshold = context.get("anomalous_drop_threshold_pct", 20)
        instrument_tasks = []
        for instrument, group_df in instrument_tick_data_map.items():
            instrument_tasks.append(
                (instrument, self.order_config_mapping[instrument], group_df, context, drop_threshold)
            )

        max_workers_cap = context.get("multiprocessing_workers", 4)
        max_workers = min(cpu_count() - 1, max_workers_cap) if cpu_count() > 1 else 1
        with Pool(processes=max_workers) as pool:
            results = pool.starmap(generate_exit_attributes_for_instrument, instrument_tasks)

        for instrument, instrument_order_config in results:
            self.order_config_mapping[instrument] = instrument_order_config


def generate_exit_attributes_for_instrument(instrument, instrument_order_config, df_instrument_tick_data, context,
                                             drop_threshold=20):
    """Compute exit attributes for a single instrument.

    For each entry order, walk forward through price data to find exit point based on:
    - Trailing stop-loss breach (exit at next day's open)
    - Anomalous price drop (exit at 80% of last good price)
    - End of data (exit at last close)
    """
    # Pre-extract columns as lists for fast iteration
    date_epochs = df_instrument_tick_data["date_epoch"].to_list()
    closes = df_instrument_tick_data["close"].to_list()
    opens = df_instrument_tick_data["open"].to_list()
    next_opens = df_instrument_tick_data["next_open"].to_list()
    next_volumes = df_instrument_tick_data["next_volume"].to_list()
    next_epochs = df_instrument_tick_data["next_epoch"].to_list()

    instrument_last_epoch = max(date_epochs)

    for idx in range(len(date_epochs)):
        entry_epoch = date_epochs[idx]
        current_close = closes[idx]

        if entry_epoch not in instrument_order_config:
            continue

        # Guard against missing entry-day close. Signal generators typically
        # filter these out upstream, but if an entry-day lands on a
        # forward-filled weekend bar the close can be None. Downstream
        # comparisons (e.g. trailing_stop's `max_price - close_price`) would
        # TypeError. Skip the entry rather than silently miscomputing.
        if current_close is None:
            continue

        last_close = current_close
        max_price = last_close
        order_attributes = instrument_order_config[entry_epoch]
        instrument_order_config[entry_epoch] = {}
        tracker = ExitTracker()

        for j in range(idx, len(date_epochs)):
            close_price = closes[j]
            # Skip bars with missing close entirely. Forward-filled weekend
            # or holiday bars can land here; propagating None through
            # anomalous_drop / trailing_stop downstream causes TypeError on
            # numeric comparisons. `last_close` carries forward from the
            # most recent valid bar, which is the correct behavior for
            # anomalous_drop's reference-price check.
            if close_price is None:
                continue
            next_open_price = next_opens[j]
            next_volume = next_volumes[j]
            this_epoch = date_epochs[j]
            next_epoch = next_epochs[j]

            if close_price > max_price:
                max_price = close_price

            for exit_config in get_exit_config_iterator(context):
                exit_config_id = exit_config["id"]
                if tracker.has_fired(exit_config_id):
                    continue

                # 1. Anomalous DOWNWARD price gap (signed check — P0 #8 fix).
                decision = anomalous_drop(
                    close_price, last_close, drop_threshold, this_epoch)
                if decision is not None:
                    _record_exit(instrument_order_config, entry_epoch,
                                 order_attributes, decision, exit_config_id,
                                 next_volume=None)
                    # P0 #9 fix: tracker.record() is called for EVERY exit
                    # decision, so the TSL branch can no longer fire again
                    # for the same exit_config the next day.
                    tracker.record(exit_config_id)
                    continue

                # 2. Last bar of data: force-close at close.
                decision = end_of_data(this_epoch, instrument_last_epoch, close_price)
                if decision is not None:
                    _record_exit(instrument_order_config, entry_epoch,
                                 order_attributes, decision, exit_config_id,
                                 next_volume=None)
                    tracker.record(exit_config_id)
                    continue

                # 3. Min-hold gate.
                if below_min_hold(this_epoch, entry_epoch, exit_config["min_hold_time_days"]):
                    continue

                # 4. Trailing stop loss (MOC: exit at next_open if available).
                decision = trailing_stop(
                    close_price, max_price, exit_config["trailing_stop_pct"],
                    next_epoch, next_open_price, this_epoch)
                if decision is not None:
                    _record_exit(instrument_order_config, entry_epoch,
                                 order_attributes, decision, exit_config_id,
                                 next_volume=next_volume)
                    tracker.record(exit_config_id)
                    continue

            last_close = close_price
            if tracker.all_fired(context["total_exit_configs"]):
                break

    return instrument, instrument_order_config


def _record_exit(instrument_order_config, entry_epoch, order_attributes,
                 decision, exit_config_id, next_volume=None):
    """Merge an exit decision into instrument_order_config[entry_epoch].

    If an exit row already exists at this decision's epoch, append the
    exit_config_id to its comma-separated list. Otherwise clone
    order_attributes and populate with the decision's exit price/volume.
    """
    existing = instrument_order_config[entry_epoch].get(decision.exit_epoch)
    if existing is not None:
        existing["exit_config_ids"] = (
            f"{existing['exit_config_ids']},{exit_config_id}"
        )
        # First-decision-wins on exit_price / exit_reason; later-arriving
        # exit_configs at the same epoch only append their id.
        # NOTE (Decision 3 nuance, code review 2026-04-21): within a single
        # exit_config's per-bar iteration, anomalous_drop is checked before
        # end_of_data before trailing_stop, so the more authoritative
        # decision wins. ACROSS exit_configs at the same epoch, however, the
        # winner is whichever config's iteration happened first in
        # `get_exit_config_iterator(context)` order — not a priority order.
        # A future change may want to sort decisions by reason priority.
        return
    attrs = copy.deepcopy(order_attributes)
    attrs["exit_price"] = decision.exit_price
    if next_volume is not None:
        attrs["exit_volume"] = next_volume
    attrs["exit_config_ids"] = f"{exit_config_id}"
    attrs["exit_reason"] = decision.reason
    instrument_order_config[entry_epoch][decision.exit_epoch] = attrs


def process(context: dict, df_tick_data_original: pl.DataFrame) -> pl.DataFrame:
    """Run order generation: compute entry signals and exit attributes.

    Args:
        context: dict with entry_config_input, exit_config_input, total_exit_configs
        df_tick_data_original: pl.DataFrame from scanner step (with scanner_config_ids)

    Returns:
        pl.DataFrame of orders with columns:
            instrument, entry_epoch, exit_epoch, entry_price, exit_price,
            entry_volume, exit_volume, scanner_config_ids, entry_config_ids,
            exit_config_ids, exit_reason

    Non-determinism note (code review 2026-04-21):
        `generate_exit_attributes` uses `multiprocessing.Pool.starmap` to
        parallelize the per-instrument walk-forward exit computation.
        Pool task completion order is not guaranteed across runs, so the
        row order of the returned DataFrame can vary. A `.sort(...)` is
        applied before return, but any per-instrument row-level comparison
        across runs should be metric-level (summary stats), not
        trade-level (exact trade ordering). Summary metrics are stable;
        trade_log indices are not.
    """
    df_tick_data_original = df_tick_data_original.sort(["instrument", "date_epoch"])

    # Compute next-day values (entry happens on the day after signal)
    df_tick_data_original = df_tick_data_original.with_columns([
        pl.col("date_epoch").shift(-1).over("instrument").alias("next_epoch"),
        pl.col("open").shift(-1).over("instrument").alias("next_open"),
        pl.col("volume").shift(-1).over("instrument").alias("next_volume"),
    ])

    # Drop rows without next-day data (last day per instrument)
    df_tick_data_original = df_tick_data_original.filter(pl.col("next_epoch").is_not_null())

    order_generation_util = OrderGenerationUtil(df_tick_data_original)

    print(f"  Order gen: {df_tick_data_original.height} rows, "
          f"{df_tick_data_original['instrument'].n_unique()} instruments")

    _checkpoint = time.time()
    for entry_config in get_entry_config_iterator(context):
        df_tick_data = df_tick_data_original.clone()
        df_tick_data = order_generation_util.add_entry_signal_inplace(df_tick_data, entry_config)
        order_generation_util.update_config_order_map(df_tick_data, entry_config["id"])
    print(f"  Entry signals: {round(time.time() - _checkpoint, 2)}s")

    _checkpoint = time.time()
    order_generation_util.generate_exit_attributes(context)
    print(f"  Exit attributes: {round(time.time() - _checkpoint, 2)}s")

    df_orders = order_generation_util.generate_order_df()
    print(f"  Orders generated: {df_orders.height}")

    return df_orders

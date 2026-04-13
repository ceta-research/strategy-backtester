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

        df_direction = df_tick_data.group_by("date_epoch").agg(
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

        column_order = [
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids",
        ]
        if not rows:
            return pl.DataFrame(schema={c: pl.Utf8 if c in ("instrument", "scanner_config_ids", "entry_config_ids", "exit_config_ids") else pl.Float64 for c in column_order})
        df = pl.DataFrame(rows)
        df = df.select(column_order)
        df = df.sort(["instrument", "entry_epoch", "exit_epoch"])
        return df

    def generate_exit_attributes(self, context: dict):
        """Compute exit prices/epochs for all instruments using multiprocessing."""
        instrument_tick_data_map = {}
        self.df_tick_data = self.df_tick_data.filter(
            pl.col("instrument").is_in(list(self.order_config_mapping.keys()))
        )

        for instrument_tuple, group in self.df_tick_data.group_by("instrument"):
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

        last_close = current_close
        max_price = last_close
        order_attributes = instrument_order_config[entry_epoch]
        instrument_order_config[entry_epoch] = {}
        order_exit_tracker = set()

        for j in range(idx, len(date_epochs)):
            close_price = closes[j]
            open_price = opens[j]
            next_open_price = next_opens[j]
            next_volume = next_volumes[j]
            this_epoch = date_epochs[j]
            next_epoch = next_epochs[j]

            max_price = max(max_price, close_price)
            hold_time_in_days = (this_epoch - entry_epoch) / SECONDS_IN_ONE_DAY

            for exit_config in get_exit_config_iterator(context):
                if exit_config["id"] in order_exit_tracker:
                    continue

                # Handle anomalous price drops (merger/de-merger/etc.)
                diff_since_reference_price = (close_price - last_close) * 100 / last_close
                if abs(diff_since_reference_price) > drop_threshold:
                    if this_epoch in instrument_order_config[entry_epoch]:
                        _order_attributes = instrument_order_config[entry_epoch][this_epoch]
                        _order_attributes["exit_config_ids"] = (
                            f"{_order_attributes['exit_config_ids']},{exit_config['id']}"
                        )
                    else:
                        _order_attributes = copy.deepcopy(order_attributes)
                        _order_attributes["exit_price"] = last_close * 0.8
                        _order_attributes["exit_config_ids"] = f"{exit_config['id']}"
                        instrument_order_config[entry_epoch][this_epoch] = _order_attributes
                    continue

                # End of data: exit at last close
                if this_epoch == instrument_last_epoch:
                    if this_epoch in instrument_order_config[entry_epoch]:
                        _order_attributes = instrument_order_config[entry_epoch][this_epoch]
                        _order_attributes["exit_config_ids"] = (
                            f"{_order_attributes['exit_config_ids']},{exit_config['id']}"
                        )
                    else:
                        _order_attributes = copy.deepcopy(order_attributes)
                        _order_attributes["exit_price"] = close_price
                        _order_attributes["exit_config_ids"] = f"{exit_config['id']}"
                        instrument_order_config[entry_epoch][this_epoch] = _order_attributes
                    continue

                min_hold_time_days = exit_config["min_hold_time_days"]
                if hold_time_in_days < min_hold_time_days:
                    continue

                draw_down_percent = (max_price - close_price) * 100 / max_price
                trailing_stop_pct = exit_config["trailing_stop_pct"]
                if draw_down_percent > trailing_stop_pct:
                    order_exit_tracker.add(exit_config["id"])
                    if next_epoch in instrument_order_config[entry_epoch]:
                        _order_attributes = instrument_order_config[entry_epoch][next_epoch]
                        _order_attributes["exit_config_ids"] = (
                            f"{_order_attributes['exit_config_ids']},{exit_config['id']}"
                        )
                    else:
                        _order_attributes = copy.deepcopy(order_attributes)
                        _order_attributes["exit_volume"] = next_volume
                        _order_attributes["exit_price"] = next_open_price
                        _order_attributes["exit_config_ids"] = f"{exit_config['id']}"
                        instrument_order_config[entry_epoch][next_epoch] = _order_attributes

            last_close = close_price
            if len(order_exit_tracker) == context["total_exit_configs"]:
                break

    return instrument, instrument_order_config


def process(context: dict, df_tick_data_original: pl.DataFrame) -> pl.DataFrame:
    """Run order generation: compute entry signals and exit attributes.

    Args:
        context: dict with entry_config_input, exit_config_input, total_exit_configs
        df_tick_data_original: pl.DataFrame from scanner step (with scanner_config_ids)

    Returns:
        pl.DataFrame of orders with columns:
            instrument, entry_epoch, exit_epoch, entry_price, exit_price,
            entry_volume, exit_volume, scanner_config_ids, entry_config_ids, exit_config_ids
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

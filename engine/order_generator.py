"""Order generation: entry/exit signal computation.

Ported from ATO_Simulator/simulator/steps/order_generation_step/process_step.py.
Pseudo-future-epoch logic removed (not needed for in-memory pipeline).
"""

import copy
import time
from collections import defaultdict
from multiprocessing import Pool, cpu_count

import pandas as pd

from engine.config_loader import get_entry_config_iterator, get_exit_config_iterator
from engine.constants import SECONDS_IN_ONE_DAY


class OrderGenerationUtil:
    PRICE_DROP_THRESHOLD = 20

    def __init__(self, df_tick_data):
        self.df_tick_data = df_tick_data
        self.order_config_mapping = {}

    @staticmethod
    def add_direction_score(df_tick_data, direction_score_config):
        df_tick_data.sort_values(["instrument", "date_epoch"], inplace=True)
        df_tick_data["direction_score_n_day_ma"] = df_tick_data.groupby("instrument")["close"].transform(
            lambda x: x.rolling(window=direction_score_config["n_day_ma"], min_periods=1).mean()
        )

        df_tick_data.loc[~(df_tick_data["close"] > df_tick_data["direction_score_n_day_ma"]), "direction_score"] = 0
        df_tick_data.loc[df_tick_data["close"] > df_tick_data["direction_score_n_day_ma"], "direction_score"] = 1
        df_direction = df_tick_data.groupby("date_epoch")["direction_score"].mean().reset_index()

        df_tick_data = df_tick_data.drop(["direction_score", "direction_score_n_day_ma"], axis=1)
        df_tick_data = pd.merge(df_tick_data, df_direction, on=["date_epoch"])
        df_tick_data.sort_values(["instrument", "date_epoch"], inplace=True)
        return df_tick_data

    def add_entry_signal_inplace(self, df_tick_data, entry_config):
        df_tick_data["instrument"] = df_tick_data["instrument"].astype("str")
        df_tick_data = self.add_direction_score(df_tick_data, entry_config["direction_score"])
        df_tick_data["n_day_ma"] = df_tick_data.groupby("instrument")["close"].transform(
            lambda x: x.rolling(window=entry_config["n_day_ma"], min_periods=1).mean()
        )

        df_tick_data["n_day_high"] = df_tick_data.groupby("instrument")["close"].transform(
            lambda x: x.rolling(window=entry_config["n_day_high"], min_periods=1).max()
        )

        df_tick_data["can_enter"] = (
            (df_tick_data["close"] > df_tick_data["n_day_ma"])
            & (df_tick_data["close"] >= df_tick_data["n_day_high"])
            & (df_tick_data["close"] > df_tick_data["open"])
            & (~df_tick_data["scanner_config_ids"].isna())
            & (df_tick_data["direction_score"] > entry_config["direction_score"]["score"])
        )
        return df_tick_data

    def update_config_order_map(self, df_tick_data, entry_config_id):
        df_tick_data = df_tick_data[df_tick_data["can_enter"]]

        for instrument, scanner_config_ids, entry_epoch, entry_price, entry_volume in zip(
            df_tick_data["instrument"],
            df_tick_data["scanner_config_ids"],
            df_tick_data["next_epoch"],
            df_tick_data["next_open"],
            df_tick_data["next_volume"],
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

    def generate_order_df(self):
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
            return pd.DataFrame(columns=column_order)
        df = pd.DataFrame(rows)
        df = df[column_order]
        df = df.sort_values(["instrument", "entry_epoch", "exit_epoch"])
        return df

    def generate_exit_attributes(self, context):
        """Compute exit prices/epochs for all instruments using multiprocessing."""
        instrument_tick_data_idx_map = defaultdict(list)
        self.df_tick_data = self.df_tick_data[self.df_tick_data["instrument"].isin(self.order_config_mapping.keys())]
        self.df_tick_data.reset_index(drop=True, inplace=True)
        for idx, instrument in zip(self.df_tick_data.index, self.df_tick_data["instrument"]):
            instrument_tick_data_idx_map[instrument].append(idx)

        instrument_tasks = []
        for instrument in instrument_tick_data_idx_map:
            instrument_indices = list(instrument_tick_data_idx_map[instrument])
            df_instrument_tick_data = self.df_tick_data.loc[instrument_indices, :].reset_index(drop=True)
            instrument_tasks.append(
                (instrument, self.order_config_mapping[instrument], df_instrument_tick_data, context)
            )

        max_workers = min(cpu_count() - 1, 4) if cpu_count() > 1 else 1
        with Pool(processes=max_workers) as pool:
            results = pool.starmap(generate_exit_attributes_for_instrument, instrument_tasks)

        for instrument, instrument_order_config in results:
            self.order_config_mapping[instrument] = instrument_order_config


def generate_exit_attributes_for_instrument(instrument, instrument_order_config, df_instrument_tick_data, context):
    """Compute exit attributes for a single instrument.

    For each entry order, walk forward through price data to find exit point based on:
    - Trailing stop-loss breach (exit at next day's open)
    - Anomalous price drop >20% (exit at 80% of last good price)
    - End of data (exit at last close)
    """
    instrument_last_epoch = df_instrument_tick_data["date_epoch"].max()

    for idx, entry_epoch, current_close in zip(
        df_instrument_tick_data.index, df_instrument_tick_data["date_epoch"], df_instrument_tick_data["close"]
    ):
        if entry_epoch not in instrument_order_config:
            continue

        last_close = current_close
        max_price = last_close
        order_attributes = instrument_order_config[entry_epoch]
        instrument_order_config[entry_epoch] = {}
        order_exit_tracker = set()

        for close_price, open_price, next_open_price, next_volume, this_epoch, next_epoch in zip(
            df_instrument_tick_data["close"][idx:],
            df_instrument_tick_data["open"][idx:],
            df_instrument_tick_data["next_open"][idx:],
            df_instrument_tick_data["next_volume"][idx:],
            df_instrument_tick_data["date_epoch"][idx:],
            df_instrument_tick_data["next_epoch"][idx:],
        ):
            max_price = max(max_price, close_price)
            hold_time_in_days = (this_epoch - entry_epoch) / SECONDS_IN_ONE_DAY

            for exit_config in get_exit_config_iterator(context):
                if exit_config["id"] in order_exit_tracker:
                    continue

                # Handle anomalous price drops (merger/de-merger/etc.)
                diff_since_reference_price = (close_price - last_close) * 100 / last_close
                if abs(diff_since_reference_price) > OrderGenerationUtil.PRICE_DROP_THRESHOLD:
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
                trailing_stop_loss = exit_config["trailing_stop_loss"]
                if draw_down_percent > trailing_stop_loss:
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


def process(context, df_tick_data_original: pd.DataFrame):
    """Run order generation: compute entry signals and exit attributes.

    Args:
        context: dict with entry_config_input, exit_config_input, total_exit_configs
        df_tick_data_original: DataFrame from scanner step (with scanner_config_ids)

    Returns:
        DataFrame of orders with columns:
            instrument, entry_epoch, exit_epoch, entry_price, exit_price,
            entry_volume, exit_volume, scanner_config_ids, entry_config_ids, exit_config_ids
    """
    df_tick_data_original.sort_values(["instrument", "date_epoch"], inplace=True)

    # Compute next-day values (entry happens on the day after signal)
    df_tick_data_original["next_epoch"] = df_tick_data_original.groupby(["instrument"])["date_epoch"].shift(-1)
    df_tick_data_original["next_open"] = df_tick_data_original.groupby(["instrument"])["open"].shift(-1)
    df_tick_data_original["next_volume"] = df_tick_data_original.groupby(["instrument"])["volume"].shift(-1)

    # Drop rows without next-day data (last day per instrument)
    df_tick_data_original.dropna(subset=["next_epoch"], inplace=True)

    order_generation_util = OrderGenerationUtil(df_tick_data_original)

    print(f"  Order gen: {len(df_tick_data_original)} rows, "
          f"{len(df_tick_data_original['instrument'].unique())} instruments")

    _checkpoint = time.time()
    for entry_config in get_entry_config_iterator(context):
        df_tick_data = df_tick_data_original.copy()
        df_tick_data = order_generation_util.add_entry_signal_inplace(df_tick_data, entry_config)
        order_generation_util.update_config_order_map(df_tick_data, entry_config["id"])
    print(f"  Entry signals: {round(time.time() - _checkpoint, 2)}s")

    _checkpoint = time.time()
    order_generation_util.generate_exit_attributes(context)
    print(f"  Exit attributes: {round(time.time() - _checkpoint, 2)}s")

    df_orders = order_generation_util.generate_order_df()
    print(f"  Orders generated: {len(df_orders)}")

    return df_orders

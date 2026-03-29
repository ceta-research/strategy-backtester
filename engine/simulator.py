"""Position-level trading simulator (state machine).

Ported from ATO_Simulator/simulator/steps/simulate_step/process_step.py.
Daily loop: entries, exits, MTM, position tracking with real broker charges.
"""

import copy
from collections import defaultdict

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.charges import calculate_charges


def process(context, df_orders, epoch_wise_instrument_stats, current_snapshot, sim_config, config_id):
    """Run the simulation state machine for a single config combination.

    Args:
        context: dict with start_margin, start_epoch, end_epoch
        df_orders: DataFrame of orders (filtered and ranked for this config)
        epoch_wise_instrument_stats: {epoch: {instrument: {close, avg_txn}}}
        current_snapshot: dict (empty on first run, carries state across chunks)
        sim_config: simulation config dict
        config_id: string identifier for this config combination

    Returns:
        tuple of (day_wise_log, config_order_ids, current_snapshot, day_wise_positions, trade_log)
    """
    pay_out_config = sim_config.get("pay_out", {})
    max_positions = sim_config["max_positions"]
    max_positions_per_instrument = sim_config["max_positions_per_instrument"]

    if not current_snapshot:
        current_snapshot = {
            "margin_available": context["start_margin"],
            "current_position_value": 0,
            "simulation_date": context["start_epoch"] - (context["start_epoch"] % SECONDS_IN_ONE_DAY),
            "current_positions_count": 0,
            "max_account_value": context["start_margin"],
            "current_positions": {},
        }
        if pay_out_config:
            current_snapshot["next_payout_epoch"] = (
                current_snapshot["simulation_date"]
                + min(pay_out_config["withdrawal_lockup_days"], pay_out_config["payout_interval_days"])
                * SECONDS_IN_ONE_DAY
            )

    margin_available = current_snapshot["margin_available"]
    current_position_value = current_snapshot["current_position_value"]
    current_positions_count = current_snapshot["current_positions_count"]
    max_account_value = current_snapshot["max_account_value"]
    current_positions = current_snapshot["current_positions"]

    order_epochs = set()
    date_orders = defaultdict(lambda: {"entries": [], "exits": []})

    # Add open positions to exit tracker
    for instrument, positions in current_positions.items():
        for order_id in positions:
            order_epochs.add(positions[order_id]["exit_epoch"])
            date_orders[positions[order_id]["exit_epoch"]]["exits"].append(positions[order_id])

    if len(df_orders) > 0:
        end_epoch = df_orders["entry_epoch"].max()
        order_epochs = set(df_orders["entry_epoch"].unique().to_list()) | set(df_orders["exit_epoch"].unique().to_list())
    else:
        end_epoch = context["end_epoch"]

    mtm_epochs = set(epoch_wise_instrument_stats.keys())
    processing_dates = sorted(mtm_epochs | order_epochs)

    if len(df_orders) > 0:
        for record in df_orders.to_dicts():
            date_orders[record["entry_epoch"]]["entries"].append(record)

    day_wise_log = []
    config_order_ids = []
    day_wise_positions = {}
    trade_log = []

    for simulation_date_epoch in processing_dates:
        if simulation_date_epoch < current_snapshot["simulation_date"]:
            continue
        if simulation_date_epoch >= end_epoch:
            break

        current_account_value = current_position_value + margin_available

        # Handle payouts
        if "next_payout_epoch" in current_snapshot and simulation_date_epoch >= current_snapshot["next_payout_epoch"]:
            pay_out_sum = 0
            if pay_out_config["type"] == "fixed":
                pay_out_sum = pay_out_config["value"]
            elif pay_out_config["type"] == "percentage":
                pay_out_sum = pay_out_config["value"] * current_account_value / 100

            current_snapshot["next_payout_epoch"] = simulation_date_epoch + (
                pay_out_config["payout_interval_days"] * SECONDS_IN_ONE_DAY
            )
            margin_available -= min(margin_available, pay_out_sum)

        # Compute order value
        order_value = current_account_value / max_positions
        if "order_value" in sim_config:
            if sim_config["order_value"]["type"] == "fixed":
                order_value = sim_config["order_value"]["value"]
            elif sim_config["order_value"]["type"] == "percentage_of_account_value":
                order_value = current_account_value * sim_config["order_value"]["value"] / 100
            elif sim_config["order_value"]["type"] == "percentage_of_available_margin":
                order_value = margin_available * sim_config["order_value"]["value"] / 100

        if "order_value_multiplier" in sim_config:
            order_value *= sim_config["order_value_multiplier"]

        if orders := date_orders.get(simulation_date_epoch):
            # Process entries
            for entry_order in orders["entries"]:
                if current_positions_count >= max_positions:
                    break

                instrument_open_position_count = 0
                if entry_order["instrument"] in current_positions:
                    instrument_open_position_count = len(current_positions[entry_order["instrument"]])

                if instrument_open_position_count >= max_positions_per_instrument:
                    continue

                _order_value = order_value
                if "max_order_value" in sim_config:
                    max_order_config = sim_config["max_order_value"]
                    if max_order_config["type"] == "fixed":
                        _order_value = min(_order_value, max_order_config["value"])
                    elif max_order_config["type"] == "percentage_of_account_value":
                        _order_value = min(_order_value, current_account_value * max_order_config["value"] / 100)
                    elif max_order_config["type"] == "percentage_of_available_margin":
                        _order_value = min(_order_value, margin_available * max_order_config["value"] / 100)
                    elif max_order_config["type"] == "percentage_of_instrument_avg_txn":
                        avg_txn = (
                            epoch_wise_instrument_stats
                            .get(simulation_date_epoch, {})
                            .get(entry_order["instrument"], {})
                            .get("avg_txn")
                        )
                        if avg_txn is not None:
                            _order_value = min(_order_value, avg_txn * max_order_config["value"] / 100)

                order_quantity = int(_order_value / entry_order["entry_price"])
                exchange = entry_order["instrument"].split(":")[0]
                charges = calculate_charges(
                    exchange,
                    order_quantity * entry_order["entry_price"],
                    segment="EQUITY",
                    trade_type="DELIVERY",
                    which_side="BUY_SIDE",
                )

                slippage = order_quantity * entry_order["entry_price"] * 0.0005
                required_margin_for_entry = order_quantity * entry_order["entry_price"] + charges + slippage

                if margin_available >= required_margin_for_entry and order_quantity > 0:
                    current_positions_count += 1
                    margin_available -= required_margin_for_entry
                    order_id = f"{entry_order['instrument']}_{entry_order['entry_epoch']}_{entry_order['exit_epoch']}"
                    this_order = {
                        "instrument": entry_order["instrument"],
                        "entry_epoch": entry_order["entry_epoch"],
                        "exit_epoch": entry_order["exit_epoch"],
                        "entry_price": entry_order["entry_price"],
                        "exit_price": entry_order["exit_price"],
                        "quantity": order_quantity,
                        "last_close_price": entry_order["entry_price"],
                        "entry_charges": charges,
                        "entry_slippage": slippage,
                    }

                    current_positions.setdefault(entry_order["instrument"], {})[order_id] = this_order
                    config_order_ids.append(order_id)
                    date_orders[entry_order["exit_epoch"]]["exits"].append(this_order)

            # Process exits
            for exit_order in orders["exits"]:
                order_id = f"{exit_order['instrument']}_{exit_order['entry_epoch']}_{exit_order['exit_epoch']}"
                if (
                    exit_order["instrument"] in current_positions
                    and order_id in current_positions[exit_order["instrument"]]
                ):
                    position = current_positions[exit_order["instrument"]][order_id]
                    exchange = exit_order["instrument"].split(":")[0]
                    current_positions_count -= 1
                    sell_charges = calculate_charges(
                        exchange,
                        position["quantity"] * position["exit_price"],
                        segment="EQUITY",
                        trade_type="DELIVERY",
                        which_side="SELL_SIDE",
                    )
                    sell_slippage = position["quantity"] * position["exit_price"] * 0.0005
                    margin_available += position["quantity"] * position["exit_price"] - sell_charges - sell_slippage

                    trade_log.append({
                        "instrument": position["instrument"],
                        "entry_epoch": position["entry_epoch"],
                        "exit_epoch": position["exit_epoch"],
                        "entry_price": position["entry_price"],
                        "exit_price": position["exit_price"],
                        "quantity": position["quantity"],
                        "entry_charges": position.get("entry_charges", 0),
                        "sell_charges": sell_charges,
                        "slippage": position.get("entry_slippage", 0) + sell_slippage,
                    })

                    del current_positions[exit_order["instrument"]][order_id]
                    if len(current_positions[exit_order["instrument"]]) == 0:
                        del current_positions[exit_order["instrument"]]

        # MTM update
        if simulation_date_epoch in mtm_epochs:
            invested_value = 0
            for instrument, positions in current_positions.items():
                close_price = (
                    epoch_wise_instrument_stats
                    .get(simulation_date_epoch, {})
                    .get(instrument, {})
                    .get("close")
                )
                if close_price:
                    for pos in positions.values():
                        pos["last_close_price"] = close_price

                invested_value += sum(pos["quantity"] * pos["last_close_price"] for pos in positions.values())

            current_position_value = invested_value
            current_account_value = margin_available + current_position_value
            day_summary = {
                "log_date_epoch": simulation_date_epoch,
                "invested_value": invested_value,
                "margin_available": margin_available,
            }
            day_wise_log.append(day_summary)
            day_wise_positions[simulation_date_epoch] = copy.deepcopy(current_positions)

    # Update snapshot
    current_snapshot["margin_available"] = margin_available
    current_snapshot["current_position_value"] = current_position_value
    current_snapshot["simulation_date"] = end_epoch
    current_snapshot["current_positions_count"] = current_positions_count
    current_snapshot["max_account_value"] = max_account_value
    current_snapshot["current_positions"] = current_positions

    return day_wise_log, config_order_ids, current_snapshot, day_wise_positions, trade_log

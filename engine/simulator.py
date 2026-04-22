"""Position-level trading simulator (state machine).

Ported from ATO_Simulator/simulator/steps/simulate_step/process_step.py.
Daily loop: entries, exits, MTM, position tracking with real broker charges.

Limitations:
  - **Long-only**: no short-selling infrastructure. All strategies assume
    long equity positions. Short signals are not supported.
  - **No T+1/T+2 settlement lag**: sale proceeds are available immediately.
    Capital-constrained sweeps may overstate reinvestment speed.
  - **Linear slippage**: ``slippage_rate`` (default 5 bps) scales linearly
    with notional. Real slippage follows a concave (square-root) law;
    large orders or illiquid names will understate actual slippage.
  - **All epochs are UTC**: no timezone or DST conversion. NSE close
    (15:30 IST = 10:00 UTC) and US close (16:00 ET) both map to
    end-of-day UTC epochs. Daily granularity avoids DST edge cases.
"""

import bisect
import copy
from collections import defaultdict

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.charges import calculate_charges
from engine.order_key import OrderKey


def _process_exits(orders, current_positions, current_positions_count, margin_available, trade_log,
                    slippage_rate=0.0005):
    """Process all exit orders for a given epoch. Returns updated state."""
    for exit_order in orders["exits"]:
        order_id = OrderKey.from_order(exit_order)
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
            sell_slippage = position["quantity"] * position["exit_price"] * slippage_rate
            margin_available += position["quantity"] * position["exit_price"] - sell_charges - sell_slippage

            # NOTE: `exit_order` and `position` are the same Python dict
            # object. The `this_order` built in `_process_entries` is
            # appended to both `date_orders[exit_epoch]["exits"]` (yielding
            # `exit_order` here, via orders["exits"]) and
            # `current_positions[instrument][order_id]` (yielding `position`).
            # Python's list.append stores a reference, not a copy. We read
            # `exit_reason` from `exit_order` because the reason semantically
            # belongs to the exit event; reading from `position` would be
            # equivalent. See docs/SESSION_CODE_REVIEW.md for the trace.
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
                "exit_reason": exit_order.get("exit_reason", "natural"),
            })

            del current_positions[exit_order["instrument"]][order_id]
            if len(current_positions[exit_order["instrument"]]) == 0:
                del current_positions[exit_order["instrument"]]

    return current_positions_count, margin_available


def _process_entries(orders, current_positions, current_positions_count, margin_available,
                     order_value, current_account_value, max_positions, max_positions_per_instrument,
                     sim_config, epoch_wise_instrument_stats, simulation_date_epoch,
                     config_order_ids, date_orders, slippage_rate=0.0005,
                     missing_avg_txn_policy="no_cap", missing_avg_txn_log=None):
    """Process all entry orders for a given epoch. Returns updated state.

    Phase 2.2: when `max_order_value.type == "percentage_of_instrument_avg_txn"`
    and avg_txn is missing for this (epoch, instrument), `missing_avg_txn_policy`
    controls the fallback:
      - "no_cap" (default, preserves pre-fix behavior): place the order without
        applying the cap. Historical results remain byte-identical under this
        default.
      - "skip": do not place the order. The user asked for a liquidity cap;
        if liquidity is unknown, skipping honors the intent strictly. Opt-in
        via `context["missing_avg_txn_policy"] = "skip"` after confirming you
        are willing to accept the order-count reduction.
    Either way, the event is recorded in missing_avg_txn_log so callers can
    audit silent fallbacks after the fact (breaks the previous silence).
    """
    for entry_order in orders["entries"]:
        if current_positions_count >= max_positions:
            break

        instrument_open_position_count = 0
        if entry_order["instrument"] in current_positions:
            instrument_open_position_count = len(current_positions[entry_order["instrument"]])

        if instrument_open_position_count >= max_positions_per_instrument:
            continue

        # Order sizing pipeline:
        #   1. Base: order_value = current_account_value / max_positions
        #      (or sim_config["order_value"] if set — fixed, pct-account, or pct-margin)
        #   2. Cap: sim_config["max_order_value"] caps _order_value via min()
        #      (fixed, pct-account, pct-margin, or pct-of-avg-txn)
        #   3. Multiplier: order_value_multiplier (default 1.0) scales AFTER cap
        #      (applied at line ~140). A multiplier >1 means leverage; the cap
        #      may silently truncate the intended leverage for instruments
        #      whose avg_txn-based cap is smaller than base × multiplier.
        _order_value = order_value
        skip_this_order = False
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
                else:
                    if missing_avg_txn_log is not None:
                        missing_avg_txn_log.append({
                            "epoch": simulation_date_epoch,
                            "instrument": entry_order["instrument"],
                            "policy": missing_avg_txn_policy,
                        })
                    if missing_avg_txn_policy == "skip":
                        skip_this_order = True
                    # else "no_cap": leave _order_value at the uncapped base.
        if skip_this_order:
            continue

        # Phase 2.3 decision: integer-share quantity. This matches real
        # equity delivery (CNC) trading — Indian NSE/BSE do not support
        # fractional shares, and most international equity CNC products don't
        # either. Truncation produces ~1% cash-drag for high-priced stocks
        # with small order_value; that drag is a real cost a live trader
        # experiences, so preserving it in the backtest is correct rather
        # than a bug. If a future strategy needs fractional simulation
        # (e.g. US ETF/fractional brokerages), introduce a sim_config flag
        # and branch here.
        order_quantity = int(_order_value / entry_order["entry_price"])
        exchange = entry_order["instrument"].split(":")[0]
        charges = calculate_charges(
            exchange,
            order_quantity * entry_order["entry_price"],
            segment="EQUITY",
            trade_type="DELIVERY",
            which_side="BUY_SIDE",
        )

        slippage = order_quantity * entry_order["entry_price"] * slippage_rate
        required_margin_for_entry = order_quantity * entry_order["entry_price"] + charges + slippage

        if margin_available >= required_margin_for_entry and order_quantity > 0:
            current_positions_count += 1
            margin_available -= required_margin_for_entry
            # Layer 2 (audit P0 #7): structured identity. Tiered strategies
            # produce distinct entry_config_ids for each tier (e.g. "5_t0"
            # vs "5_t1"), so including it in the key prevents collisions.
            order_id = OrderKey.from_order(entry_order)
            if order_id in current_positions.get(entry_order["instrument"], {}):
                # Two orders cannot share the exact same OrderKey. This is a
                # hard invariant: if it trips, the signal generator produced
                # duplicate rows that used to be silently overwritten.
                raise ValueError(
                    f"Duplicate OrderKey {order_id}; signal generator emitted "
                    f"multiple orders with identical (instrument, entry_epoch, "
                    f"exit_epoch, entry_config_ids). This is a bug."
                )
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
                # Preserve entry_config_ids so the exit-side OrderKey matches
                # the entry-side OrderKey (tiered strategies depend on this).
                "entry_config_ids": entry_order.get("entry_config_ids", ""),
                # Propagate exit_reason from order_generator's exit decision
                # so _process_exits can tag the trade_log row.
                "exit_reason": entry_order.get("exit_reason", "natural"),
            }

            current_positions.setdefault(entry_order["instrument"], {})[order_id] = this_order
            config_order_ids.append(order_id)
            date_orders[entry_order["exit_epoch"]]["exits"].append(this_order)

    return current_positions_count, margin_available


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
    exit_before_entry = sim_config.get("exit_before_entry", False)
    # 5bps per side; override via context["slippage_rate"] for
    # small-cap / large-size / non-NSE which see wider effective spreads.
    slippage_rate = context.get("slippage_rate", 0.0005)
    # Phase 2.2: policy for missing avg_txn when max_order_value uses
    # percentage_of_instrument_avg_txn. Default "no_cap" preserves pre-fix
    # behavior exactly (historical results remain byte-identical). Opt into
    # the conservative "skip" behavior with context["missing_avg_txn_policy"]
    # = "skip" — refuses the order when liquidity is unknown, honoring the
    # user's cap intent strictly. Events are logged in either mode.
    missing_avg_txn_policy = context.get("missing_avg_txn_policy", "no_cap")
    missing_avg_txn_log = []

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

    # Layer 3 (audit P0 #6): simulation window is authoritative from context.
    # Pre-fix, `end_epoch = df_orders.entry_epoch.max()` when orders existed,
    # which (a) stopped MTM updates at the last entry day, (b) skipped any
    # exits scheduled after that date, and (c) used a different rule when
    # df_orders was empty. Now: single source of truth.
    end_epoch = context["end_epoch"]

    if len(df_orders) > 0:
        # SIM-1 fix (code review 2026-04-21): union-assign (`|=`) instead of
        # replace (`=`). Pre-fix, open-position exit_epochs added at lines
        # 203-206 were discarded when df_orders was non-empty. In practice
        # the `mtm_epochs` union on line 218 covered those days, but a
        # chunked/resumed sim with an open-position exit_epoch landing on a
        # data-gap day (holiday, delisting) would have silently skipped the
        # exit. Now preserved.
        order_epochs |= set(df_orders["entry_epoch"].unique().to_list()) | set(df_orders["exit_epoch"].unique().to_list())

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
        # Layer 3 fix: `> end_epoch` so end_epoch itself is processed (off-by-one).
        if simulation_date_epoch > end_epoch:
            break

        current_account_value = current_position_value + margin_available

        # Handle payouts.
        # Phase 2.4 fix: when resuming from a snapshot whose next_payout_epoch
        # lies multiple payout intervals before simulation_date_epoch (e.g.
        # long gap between chunks), pre-fix ran exactly one payout and
        # advanced next_payout_epoch by a single interval — silently skipping
        # the other due payouts. Post-fix: loop forward, running each missed
        # payout, so the account reflects the full set of withdrawals.
        # Percentage payouts re-read current_account_value inside the loop
        # so each successive withdrawal is taken from the post-previous-payout
        # balance, matching real-world sequential payout semantics.
        payout_interval = (
            pay_out_config["payout_interval_days"] * SECONDS_IN_ONE_DAY
            if pay_out_config else 0
        )
        while (
            "next_payout_epoch" in current_snapshot
            and simulation_date_epoch >= current_snapshot["next_payout_epoch"]
            and payout_interval > 0
        ):
            current_account_value = current_position_value + margin_available
            pay_out_sum = 0
            if pay_out_config["type"] == "fixed":
                pay_out_sum = pay_out_config["value"]
            elif pay_out_config["type"] == "percentage":
                pay_out_sum = pay_out_config["value"] * current_account_value / 100
            else:
                raise ValueError(
                    f"Unknown payout type: {pay_out_config['type']!r}. "
                    f"Supported: 'fixed', 'percentage'"
                )
            margin_available -= min(margin_available, pay_out_sum)
            current_snapshot["next_payout_epoch"] += payout_interval

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
            # Phase 2.5 — ordering semantics (matches ATO_Simulator):
            # Default (entries-first): new entries on day T see pre-exit state.
            #   The account_value / margin_available used to size today's new
            #   orders does NOT yet include cash that will be freed by today's
            #   natural exits. This is the ATO legacy behavior: it's slightly
            #   conservative in bull markets (you commit less new capital than
            #   you could if exits settled first) and matches the invariant
            #   that an order was sized "as-of" the sizing day's open state.
            #
            # exit_before_entry=True: exits settle first, capital is freed,
            #   then order_value is recomputed on the larger margin pool and
            #   entries are placed. Closer to "real broker at market open"
            #   semantics for delivery products where sale proceeds are
            #   available same-session. Use this for strategies that rely on
            #   recycling capital within the same day.
            #
            # Choose per-strategy via sim_config["exit_before_entry"]; the
            # default of False preserves historical result comparability.
            if exit_before_entry:
                # Exits first: frees capital + position slots before entries
                current_positions_count, margin_available = _process_exits(
                    orders, current_positions, current_positions_count, margin_available, trade_log,
                    slippage_rate=slippage_rate)

                # Recompute order value with freed capital
                current_position_value = sum(
                    pos["quantity"] * pos["last_close_price"]
                    for positions_dict in current_positions.values()
                    for pos in positions_dict.values()
                )
                current_account_value = margin_available + current_position_value
                order_value = current_account_value / max_positions

                current_positions_count, margin_available = _process_entries(
                    orders, current_positions, current_positions_count, margin_available,
                    order_value, current_account_value, max_positions, max_positions_per_instrument,
                    sim_config, epoch_wise_instrument_stats, simulation_date_epoch,
                    config_order_ids, date_orders, slippage_rate=slippage_rate,
                    missing_avg_txn_policy=missing_avg_txn_policy,
                    missing_avg_txn_log=missing_avg_txn_log)
            else:
                # Default: entries first, then exits (matches ATO_Simulator).
                # See the module-level comment at the top of _process_entries
                # for why this matches ATO; callers who want exit_before_entry
                # semantics pass sim_config["exit_before_entry"] = True.
                current_positions_count, margin_available = _process_entries(
                    orders, current_positions, current_positions_count, margin_available,
                    order_value, current_account_value, max_positions, max_positions_per_instrument,
                    sim_config, epoch_wise_instrument_stats, simulation_date_epoch,
                    config_order_ids, date_orders, slippage_rate=slippage_rate,
                    missing_avg_txn_policy=missing_avg_txn_policy,
                    missing_avg_txn_log=missing_avg_txn_log)

                current_positions_count, margin_available = _process_exits(
                    orders, current_positions, current_positions_count, margin_available, trade_log,
                    slippage_rate=slippage_rate)

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
                # Phase 2.1 fix: explicit None check. Pre-fix used
                # `if close_price:` which also treated 0.0 as "missing" and
                # silently retained the previous MTM price. A genuine zero
                # close (dividend adjustment, delisting stub, corporate action
                # artifact) is a valid MTM input — the position is worth zero.
                # Treating it as "skip update" hides the wipeout.
                if close_price is not None:
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
            # NOTE: day_wise_positions captures the open-positions snapshot taken
            # DURING the main MTM loop (before the end-of-sim close block below).
            # As a result, day_wise_positions[end_epoch] may show positions that
            # snapshot["current_positions"] reports as empty after the function
            # returns (the force-close ran between the two). Consumers that
            # want the final state should read snapshot["current_positions"],
            # not day_wise_positions[end_epoch].
            day_wise_positions[simulation_date_epoch] = copy.deepcopy(current_positions)

    # ── End-of-simulation policy: close any remaining open positions ─────
    #
    # Layer 3 (audit P0 #6): pre-fix, any position with exit_epoch beyond the
    # last entry date was silently abandoned — its trade row never emitted,
    # its final P&L never realized in metrics. Now: at the simulation boundary
    # we force-close every open position at its last known MTM price, record
    # the exit with exit_reason="end_of_sim", and settle cash.
    #
    # Policy is `close_at_mtm` (default). Callers that want to report unrealized
    # open positions separately can read `current_snapshot.current_positions`
    # before this function returns; we do not support that mode yet.
    end_of_sim_policy = context.get("end_of_sim_policy", "close_at_mtm")
    if end_of_sim_policy != "close_at_mtm":
        raise ValueError(
            f"Unknown end_of_sim_policy: {end_of_sim_policy!r}. "
            f"Only 'close_at_mtm' is implemented (see Decision 6 in "
            f"docs/AUDIT_FINDINGS.md for planned alternatives)."
        )
    if current_positions:
        # Decision 7 (code review 2026-04-21): `end_epoch` comes from config
        # and often falls on a non-trading day (year-end holidays, weekends).
        # Using it verbatim would record an exit_epoch for which no price
        # actually traded, while `last_close_price` is a stale value from
        # whatever the most recent MTM day was. Walk back to the nearest
        # prior MTM day so the trade_log's exit_epoch and exit_price
        # correspond to the same bar. Emit a warning so the caller can see
        # the alignment happened.
        effective_end_epoch = end_epoch
        if end_epoch not in mtm_epochs:
            # mtm_epochs is a set; we need a sorted view to bisect.
            sorted_mtm = sorted(mtm_epochs)
            idx = bisect.bisect_right(sorted_mtm, end_epoch) - 1
            if idx >= 0:
                effective_end_epoch = sorted_mtm[idx]
                current_snapshot.setdefault("warnings", []).append(
                    f"end_epoch {end_epoch} is not a trading day; "
                    f"force-close recorded at {effective_end_epoch} "
                    f"(last prior MTM day)."
                )
            # If there's no prior MTM day at all (end_epoch before all
            # data), fall back to end_epoch literally — last_close_price
            # is whatever the snapshot brought in. This is a degenerate
            # case that shouldn't occur in practice; log it too.
            else:
                current_snapshot.setdefault("warnings", []).append(
                    f"end_epoch {end_epoch} precedes all MTM data; "
                    f"force-close uses end_epoch verbatim with stale "
                    f"last_close_price from snapshot."
                )

        for instrument in list(current_positions.keys()):
            positions = current_positions[instrument]
            exchange = instrument.split(":")[0]
            for order_id in list(positions.keys()):
                pos = positions[order_id]
                exit_price = pos["last_close_price"]
                sell_charges = calculate_charges(
                    exchange,
                    pos["quantity"] * exit_price,
                    segment="EQUITY",
                    trade_type="DELIVERY",
                    which_side="SELL_SIDE",
                )
                sell_slippage = pos["quantity"] * exit_price * slippage_rate
                margin_available += pos["quantity"] * exit_price - sell_charges - sell_slippage
                current_positions_count -= 1
                trade_log.append({
                    "instrument": pos["instrument"],
                    "entry_epoch": pos["entry_epoch"],
                    "exit_epoch": effective_end_epoch,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "quantity": pos["quantity"],
                    "entry_charges": pos.get("entry_charges", 0),
                    "sell_charges": sell_charges,
                    "slippage": pos.get("entry_slippage", 0) + sell_slippage,
                    "exit_reason": "end_of_sim",
                })
                del positions[order_id]
            if not positions:
                del current_positions[instrument]
        current_position_value = 0

    # Update snapshot
    current_snapshot["margin_available"] = margin_available
    current_snapshot["current_position_value"] = current_position_value
    current_snapshot["simulation_date"] = end_epoch
    current_snapshot["current_positions_count"] = current_positions_count
    current_snapshot["max_account_value"] = max_account_value
    current_snapshot["current_positions"] = current_positions
    # Phase 2.2: surface every time avg_txn was missing under a
    # percentage_of_instrument_avg_txn cap, so callers can detect silent
    # skips (or silent no-cap fallbacks, depending on policy) after the fact.
    if missing_avg_txn_log:
        current_snapshot.setdefault("missing_avg_txn_events", []).extend(
            missing_avg_txn_log
        )

    return day_wise_log, config_order_ids, current_snapshot, day_wise_positions, trade_log

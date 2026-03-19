"""Pipeline orchestrator: config -> data -> signals -> rank -> simulate -> metrics.

Replaces ATO_Simulator's driver.py + simulator.py + simulate_step_loader.py.
Dispatches to pluggable signal generators based on strategy_type in config.
"""

import sys
import os
import time

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config_loader import (
    load_config,
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
    get_simulation_config_iterator,
)
from engine.config_sweep import create_config_iterator
from engine.data_provider import CRDataProvider
from engine import simulator
from engine.ranking import sort_orders
from engine.utils import create_epoch_wise_instrument_stats, create_config_df_loc_lookup
from lib.metrics import compute_metrics

# Import signal generators to trigger registration
import engine.signals.eod_technical  # noqa: F401
import engine.signals.connors_rsi  # noqa: F401
import engine.signals.ibs_reversion  # noqa: F401
import engine.signals.gap_fill  # noqa: F401
import engine.signals.overnight_hold  # noqa: F401
import engine.signals.darvas_box  # noqa: F401
import engine.signals.swing_master  # noqa: F401
import engine.signals.squeeze  # noqa: F401
import engine.signals.holp_lohp  # noqa: F401
import engine.signals.factor_composite  # noqa: F401
import engine.signals.trending_value  # noqa: F401
from engine.signals.base import get_signal_generator, sanitize_orders


def run_pipeline(config_path, data_provider=None):
    """Run the full backtesting pipeline.

    Args:
        config_path: path to YAML config file
        data_provider: optional data provider instance (defaults to CRDataProvider)

    Returns:
        list of result dicts sorted by Calmar ratio (descending)
    """
    pipeline_start = time.time()
    print(f"Loading config: {config_path}")
    config = load_config(config_path)
    static = config["static_config"]

    strategy_type = static.get("strategy_type", "eod_technical")
    signal_gen = get_signal_generator(strategy_type)
    print(f"Strategy: {strategy_type}")

    # Count total config combinations
    scanner_total, _ = create_config_iterator(**config["scanner_config_input"])
    entry_total, _ = create_config_iterator(**config["entry_config_input"])
    exit_total, _ = create_config_iterator(**config["exit_config_input"])
    sim_total, _ = create_config_iterator(**config["simulation_config_input"])
    total_configs = scanner_total * entry_total * exit_total * sim_total
    print(f"Config combinations: {scanner_total} scanner x {entry_total} entry x "
          f"{exit_total} exit x {sim_total} sim = {total_configs} total")

    # Build context (matches ATO_Simulator's context dict structure)
    context = {
        **config,
        "start_margin": static["start_margin"],
        "start_epoch": static["start_epoch"],
        "end_epoch": static["end_epoch"],
        "prefetch_days": static["prefetch_days"],
        "total_exit_configs": exit_total,
    }

    # Fetch data
    print("\n--- Fetching Data ---")
    if data_provider is None:
        data_provider = CRDataProvider(format="parquet")

    # Extract exchanges from scanner config
    exchanges = set()
    for scanner_cfg in get_scanner_config_iterator(config):
        for inst in scanner_cfg["instruments"]:
            exchanges.add(inst["exchange"])

    # Collect symbols if specified
    all_symbols = set()
    has_symbol_filter = False
    for scanner_cfg in get_scanner_config_iterator(config):
        for inst in scanner_cfg["instruments"]:
            if inst["symbols"]:
                has_symbol_filter = True
                all_symbols.update(inst["symbols"])

    df_tick_data = data_provider.fetch_ohlcv(
        exchanges=list(exchanges),
        symbols=list(all_symbols) if has_symbol_filter else None,
        start_epoch=static["start_epoch"],
        end_epoch=static["end_epoch"],
        prefetch_days=static["prefetch_days"],
    )

    if df_tick_data.is_empty():
        print("No data fetched. Aborting.")
        return []

    # Signal generation (strategy-specific scanner + order gen)
    df_orders = signal_gen.generate_orders(context, df_tick_data)

    if df_orders.is_empty():
        print("No orders generated. Aborting.")
        return []

    # Sanitize orders: remove zero-price entries only (no return cap - matches ATO_Simulator)
    df_orders = sanitize_orders(df_orders, max_return_mult=999.0)

    if df_orders.is_empty():
        print("All orders removed by sanitization. Aborting.")
        return []

    # Build epoch-wise instrument stats (used by simulator + ranking)
    print("\n--- Building Instrument Stats ---")
    stats_start = time.time()
    epoch_wise_instrument_stats = create_epoch_wise_instrument_stats(df_tick_data)
    print(f"  Stats built: {round(time.time() - stats_start, 2)}s")

    # Build config ID lookup for 3-way intersection
    scanner_idx_map, entry_idx_map, exit_idx_map = create_config_df_loc_lookup(df_orders)

    # Simulate each config combination sequentially
    print(f"\n--- Simulation ({total_configs} configs) ---")
    all_results = []
    config_num = 0

    for scanner_cfg in get_scanner_config_iterator(config):
        for entry_cfg in get_entry_config_iterator(config):
            for exit_cfg in get_exit_config_iterator(config):
                for sim_cfg in get_simulation_config_iterator(config):
                    config_num += 1
                    config_id = (
                        f"{scanner_cfg['id']}_{entry_cfg['id']}_{exit_cfg['id']}_{sim_cfg['id']}"
                    )

                    # 3-way config ID intersection
                    scanner_set = scanner_idx_map.get(scanner_cfg["id"], set())
                    entry_set = entry_idx_map.get(entry_cfg["id"], set())
                    exit_set = exit_idx_map.get(exit_cfg["id"], set())
                    order_indices = scanner_set & entry_set & exit_set

                    df_config_orders = df_orders[sorted(order_indices)]

                    # Rank/sort orders
                    if len(df_config_orders) > 0:
                        df_config_orders = sort_orders(
                            df_config_orders, sim_cfg, df_tick_data, epoch_wise_instrument_stats
                        )

                    # Run simulator
                    sim_start = time.time()
                    day_wise_log, config_order_ids, snapshot, day_wise_positions = simulator.process(
                        context, df_config_orders, epoch_wise_instrument_stats,
                        {}, sim_cfg, config_id
                    )
                    sim_elapsed = round(time.time() - sim_start, 2)

                    # Compute metrics from day_wise_log
                    result = _compute_result_metrics(
                        day_wise_log, config_id, scanner_cfg, entry_cfg, exit_cfg, sim_cfg, context
                    )

                    cagr_pct = result.get("cagr", 0) * 100 if result.get("cagr") else 0
                    max_dd_pct = result.get("max_drawdown", 0) * 100 if result.get("max_drawdown") else 0
                    print(
                        f"  config {config_num}/{total_configs}: {config_id} | "
                        f"{len(df_config_orders)} orders, {len(day_wise_log)} days | "
                        f"CAGR={cagr_pct:.1f}% MaxDD={max_dd_pct:.1f}% | {sim_elapsed}s"
                    )

                    all_results.append(result)

    # Sort by Calmar ratio (descending)
    all_results.sort(key=lambda r: r.get("calmar_ratio") or 0, reverse=True)

    elapsed = round(time.time() - pipeline_start, 2)
    print(f"\n--- Pipeline Complete: {elapsed}s ---")
    if all_results:
        best = all_results[0]
        print(f"  Best config: {best['config_id']}")
        print(f"  CAGR: {best.get('cagr', 0) * 100:.2f}%")
        print(f"  Max Drawdown: {best.get('max_drawdown', 0) * 100:.2f}%")
        print(f"  Calmar: {best.get('calmar_ratio', 'N/A')}")

    return all_results


def _compute_result_metrics(day_wise_log, config_id, scanner_cfg, entry_cfg, exit_cfg, sim_cfg, context):
    """Convert day_wise_log into metrics dict."""
    result = {
        "config_id": config_id,
        "scanner_config": scanner_cfg,
        "entry_config": entry_cfg,
        "exit_config": exit_cfg,
        "simulation_config": sim_cfg,
        "num_trading_days": len(day_wise_log),
    }

    if len(day_wise_log) < 2:
        result.update({"cagr": None, "max_drawdown": None, "calmar_ratio": None, "total_return": None})
        return result

    # Compute daily returns from account values
    account_values = [d["invested_value"] + d["margin_available"] for d in day_wise_log]
    daily_returns = []
    for i in range(1, len(account_values)):
        if account_values[i - 1] > 0:
            daily_returns.append((account_values[i] - account_values[i - 1]) / account_values[i - 1])
        else:
            daily_returns.append(0.0)

    if not daily_returns:
        result.update({"cagr": None, "max_drawdown": None, "calmar_ratio": None, "total_return": None})
        return result

    benchmark_returns = [0.0] * len(daily_returns)
    metrics = compute_metrics(daily_returns, benchmark_returns, periods_per_year=252)

    port = metrics["portfolio"]
    result.update({
        "cagr": port.get("cagr"),
        "total_return": port.get("total_return"),
        "max_drawdown": port.get("max_drawdown"),
        "calmar_ratio": port.get("calmar_ratio"),
        "sharpe_ratio": port.get("sharpe_ratio"),
        "sortino_ratio": port.get("sortino_ratio"),
        "annualized_volatility": port.get("annualized_volatility"),
        "var_95": port.get("var_95"),
        "start_value": account_values[0],
        "end_value": account_values[-1],
        "day_wise_log": day_wise_log,
    })

    return result

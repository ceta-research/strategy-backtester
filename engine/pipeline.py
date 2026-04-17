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
from engine.data_provider import CRDataProvider, BhavcopyDataProvider, NseChartingDataProvider
from engine import simulator
from engine.ranking import sort_orders
from engine.utils import create_epoch_wise_instrument_stats, create_config_df_loc_lookup
from lib.backtest_result import BacktestResult, SweepResult

import engine.signals  # noqa: F401 — registers all signal generators
from engine.signals.base import get_signal_generator, sanitize_orders, validate_orders


def run_pipeline(config_path, data_provider=None):
    """Run the full backtesting pipeline.

    Args:
        config_path: path to YAML config file
        data_provider: optional data provider instance (defaults to CRDataProvider)

    Returns:
        SweepResult with BacktestResult per config
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

    start_margin = static["start_margin"]

    # Extract exchange from scanner config for SweepResult
    exchange = "UNKNOWN"
    for scanner_cfg in get_scanner_config_iterator(config):
        for inst in scanner_cfg["instruments"]:
            exchange = inst["exchange"]
            break
        break

    sweep = SweepResult(strategy_type, "PORTFOLIO", exchange, start_margin,
                        description=f"EOD {strategy_type} sweep")

    # Build context (matches ATO_Simulator's context dict structure)
    context = {
        **config,
        "start_margin": start_margin,
        "start_epoch": static["start_epoch"],
        "end_epoch": static["end_epoch"],
        "prefetch_days": static["prefetch_days"],
        "total_exit_configs": exit_total,
        "slippage_rate": static.get("slippage_rate", 0.0005),
        "multiprocessing_workers": static.get("multiprocessing_workers", 4),
        "anomalous_drop_threshold_pct": static.get("anomalous_drop_threshold_pct", 20),
    }

    # Fetch data
    print("\n--- Fetching Data ---")
    if data_provider is None:
        provider_type = static.get("data_provider", "cr")
        if provider_type == "bhavcopy":
            data_provider = BhavcopyDataProvider()
        elif provider_type == "nse_charting":
            data_provider = NseChartingDataProvider()
        else:
            data_provider = CRDataProvider(
                format="parquet",
                timeout=static.get("cr_api_timeout", 600),
                memory_mb=static.get("cr_api_memory_mb", 16384),
                threads=static.get("cr_api_threads", 6),
                disk_mb=static.get("cr_api_disk_mb", 40960),
                spike_threshold=static.get("price_oscillation_spike", 2.0),
                mild_threshold=static.get("price_oscillation_mild", 1.3),
                min_mild_count=static.get("price_oscillation_min_count", 5),
            )

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
        return sweep

    # Signal generation (strategy-specific scanner + order gen)
    df_orders = signal_gen.generate_orders(context, df_tick_data)

    if not df_orders.is_empty():
        validate_orders(df_orders, strategy_type)

    if df_orders.is_empty():
        print("No orders generated. Aborting.")
        return sweep

    # Sanitize orders: remove zero-price entries only (no return cap - matches ATO_Simulator)
    df_orders = sanitize_orders(df_orders, max_return_mult=999.0)

    if df_orders.is_empty():
        print("All orders removed by sanitization. Aborting.")
        return sweep

    # Build epoch-wise instrument stats (used by simulator + ranking)
    # Filter to only instruments that appear in orders to avoid OOM on large datasets
    print("\n--- Building Instrument Stats ---")
    stats_start = time.time()
    order_instruments = set(df_orders["instrument"].unique().to_list())
    # Limit to top 200 instruments by order count to stay within memory
    # (covers 99%+ of portfolio value for concentrated strategies)
    if len(order_instruments) > 200:
        inst_counts = df_orders.group_by("instrument").len().sort("len", descending=True)
        top_instruments = set(inst_counts.head(200)["instrument"].to_list())
        print(f"  WARNING: {len(order_instruments)} order instruments, limiting stats to top 200")
        order_instruments = top_instruments
    # Also filter to epochs near order entry/exit dates
    order_epochs = set(df_orders["entry_epoch"].unique().to_list()) | set(df_orders["exit_epoch"].unique().to_list())
    min_epoch = min(order_epochs) - 86400 * 30  # 30-day buffer for rolling avg_txn
    df_tick_stats = df_tick_data.filter(
        (pl.col("instrument").is_in(list(order_instruments)))
        & (pl.col("date_epoch") >= min_epoch)
    )
    print(f"  Filtering: {df_tick_data['instrument'].n_unique()} -> {len(order_instruments)} instruments for stats")

    # Pre-compute ranking data ONCE for order instruments only.
    # Keep df_tick_ranked BEFORE building stats dict (which consumes lots of memory).
    df_tick_ranked = df_tick_stats  # Already filtered to order instruments

    epoch_wise_instrument_stats = create_epoch_wise_instrument_stats(df_tick_stats)
    del df_tick_stats  # Free filtered copy
    print(f"  Stats built: {round(time.time() - stats_start, 2)}s")

    # Free the large original tick data - no longer needed
    del df_tick_data
    import gc; gc.collect()
    print("  Freed df_tick_data")

    # Build config ID lookup for 3-way intersection
    scanner_idx_map, entry_idx_map, exit_idx_map = create_config_df_loc_lookup(df_orders)

    print("\n--- Pre-computing Ranking ---")
    rank_start = time.time()
    print(f"  Ranking data: {df_tick_ranked.height} rows ({len(order_instruments)} instruments), {round(time.time() - rank_start, 2)}s")

    # Simulate each config combination sequentially
    print(f"\n--- Simulation ({total_configs} configs) ---")
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

                    # Rank/sort orders (using pre-filtered tick data)
                    if len(df_config_orders) > 0:
                        df_config_orders = sort_orders(
                            df_config_orders, sim_cfg, df_tick_ranked, epoch_wise_instrument_stats
                        )

                    # Run simulator
                    sim_start = time.time()
                    day_wise_log, config_order_ids, snapshot, day_wise_positions, trade_log = simulator.process(
                        context, df_config_orders, epoch_wise_instrument_stats,
                        {}, sim_cfg, config_id
                    )
                    sim_elapsed = round(time.time() - sim_start, 2)

                    # Build BacktestResult
                    params = {"config_id": config_id}
                    br = BacktestResult(strategy_type, params, "PORTFOLIO", exchange, start_margin)

                    for day in day_wise_log:
                        br.add_equity_point(day["log_date_epoch"],
                                            day["invested_value"] + day["margin_available"])

                    for t in trade_log:
                        total_charges = t.get("entry_charges", 0) + t.get("sell_charges", 0)
                        br.add_trade(t["entry_epoch"], t["exit_epoch"],
                                     t["entry_price"], t["exit_price"],
                                     t["quantity"], charges=total_charges,
                                     symbol=t.get("instrument", ""))

                    sweep.add_config(params, br)

                    s = br.to_dict().get("summary", {})
                    cagr_pct = (s.get("cagr") or 0) * 100
                    max_dd_pct = (s.get("max_drawdown") or 0) * 100
                    print(
                        f"  config {config_num}/{total_configs}: {config_id} | "
                        f"{len(df_config_orders)} orders, {len(day_wise_log)} days | "
                        f"CAGR={cagr_pct:.1f}% MaxDD={max_dd_pct:.1f}% | {sim_elapsed}s"
                    )

    elapsed = round(time.time() - pipeline_start, 2)
    print(f"\n--- Pipeline Complete: {elapsed}s ---")
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        bs = best_result.to_dict()["summary"]
        print(f"  Best config: {best_params.get('config_id', best_params)}")
        print(f"  CAGR: {(bs.get('cagr') or 0) * 100:.2f}%")
        print(f"  Max Drawdown: {(bs.get('max_drawdown') or 0) * 100:.2f}%")
        print(f"  Calmar: {bs.get('calmar_ratio', 'N/A')}")

    return sweep

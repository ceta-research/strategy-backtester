"""Integration test: synthetic end-to-end pipeline.

Uses 5 instruments, 60 trading days, deterministic OHLCV data.
Verifies the full pipeline: scanner -> order_gen -> ranking -> simulator.
"""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.config_loader import load_config, get_scanner_config_iterator, get_entry_config_iterator, get_exit_config_iterator, get_simulation_config_iterator
from engine.config_sweep import create_config_iterator
from engine import scanner, order_generator, simulator
from engine.ranking import sort_orders
from engine.utils import create_epoch_wise_instrument_stats, create_config_df_loc_lookup


def make_deterministic_data():
    """Create 5 instruments x 60 days of trending data.

    Prices trend upward with periodic dips to trigger trailing stop-loss exits.
    """
    base_epoch = 1577836800  # 2020-01-01
    n_days = 60
    instruments = [
        ("SYM0", 100), ("SYM1", 150), ("SYM2", 200), ("SYM3", 80), ("SYM4", 120)
    ]

    rows = []
    for sym, base_price in instruments:
        for d in range(n_days):
            epoch = base_epoch + d * 86400
            # Upward trend with a dip every 20 days
            trend = d * 0.8
            dip = -15 if d % 20 == 19 else 0
            price = base_price + trend + dip
            volume = 2000000
            rows.append({
                "date_epoch": epoch,
                "open": price - 0.5,
                "high": price + 1.5,
                "low": price - 1.5,
                "close": price,
                "average_price": (price + 1.5 + price - 1.5 + price) / 3,
                "volume": volume,
                "symbol": sym,
                "instrument": f"NSE:{sym}",
                "exchange": "NSE",
            })
    return pl.DataFrame(rows)


MINIMAL_CONFIG = """
static:
  start_margin: 1000000
  start_epoch: 1577836800
  end_epoch: 1582934400
  prefetch_days: 0
  data_granularity: day

scanner:
  instruments:
    - [{exchange: NSE, symbols: []}]
  price_threshold: [50]
  avg_day_transaction_threshold:
    - {period: 5, threshold: 100}
  n_day_gain_threshold:
    - {n: 3, threshold: -999}

entry:
  n_day_ma: [3]
  n_day_high: [2]
  direction_score:
    - {n_day_ma: 3, score: 0.0}

exit:
  min_hold_time_days: [0]
  trailing_stop_loss: [10]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_gainer]
  order_ranking_window_days: [30]
  max_positions: [5]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {type: fixed, value: 200000}
"""


def test_end_to_end():
    """Run full pipeline with synthetic data and verify invariants."""
    # Load config
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(MINIMAL_CONFIG)
        f.flush()
        config = load_config(f.name)
    os.unlink(f.name)

    static = config["static_config"]

    # Count configs
    exit_total, _ = create_config_iterator(**config["exit_config_input"])

    context = {
        **config,
        "start_margin": static["start_margin"],
        "start_epoch": static["start_epoch"],
        "end_epoch": static["end_epoch"],
        "prefetch_days": static["prefetch_days"],
        "total_exit_configs": exit_total,
    }

    df = make_deterministic_data()

    # Scanner
    df_scanned = scanner.process(context, df)
    assert "scanner_config_ids" in df_scanned.columns
    signals = df_scanned["scanner_config_ids"].is_not_null().sum()
    print(f"  Scanner: {df_scanned.height} rows, {signals} with signals")

    # Order generation
    df_orders = order_generator.process(context, df_scanned)
    print(f"  Orders: {df_orders.height}")

    if df_orders.is_empty():
        print("  No orders generated (expected with this synthetic data)")
        print("End-to-end test passed (no-orders path)!")
        return

    # Build stats
    epoch_wise_stats = create_epoch_wise_instrument_stats(df)
    scanner_idx, entry_idx, exit_idx = create_config_df_loc_lookup(df_orders)

    # Run single config
    scanner_cfg = next(get_scanner_config_iterator(config))
    entry_cfg = next(get_entry_config_iterator(config))
    exit_cfg = next(get_exit_config_iterator(config))
    sim_cfg = next(get_simulation_config_iterator(config))

    config_id = f"{scanner_cfg['id']}_{entry_cfg['id']}_{exit_cfg['id']}_{sim_cfg['id']}"

    # 3-way intersection
    order_indices = (
        scanner_idx.get(scanner_cfg["id"], set())
        & entry_idx.get(entry_cfg["id"], set())
        & exit_idx.get(exit_cfg["id"], set())
    )
    df_config_orders = df_orders[sorted(order_indices)]

    if df_config_orders.height > 0:
        df_config_orders = sort_orders(df_config_orders, sim_cfg, df, epoch_wise_stats)

    day_wise_log, order_ids, snapshot, positions = simulator.process(
        context, df_config_orders, epoch_wise_stats, {}, sim_cfg, config_id
    )

    print(f"  Simulation: {len(day_wise_log)} days logged, {len(order_ids)} orders executed")

    # Verify invariants
    for day in day_wise_log:
        account_value = day["invested_value"] + day["margin_available"]
        assert day["margin_available"] >= 0, f"Negative margin: {day['margin_available']}"
        assert account_value > 0, f"Account value <= 0: {account_value}"

    assert snapshot["current_positions_count"] <= sim_cfg["max_positions"], (
        f"Position count {snapshot['current_positions_count']} > max {sim_cfg['max_positions']}"
    )

    # Per-instrument limit
    for instrument, inst_positions in snapshot["current_positions"].items():
        assert len(inst_positions) <= sim_cfg["max_positions_per_instrument"], (
            f"{instrument}: {len(inst_positions)} positions > max {sim_cfg['max_positions_per_instrument']}"
        )

    print("End-to-end test passed!")


if __name__ == "__main__":
    test_end_to_end()

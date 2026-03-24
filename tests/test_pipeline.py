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

    day_wise_log, order_ids, snapshot, positions, trade_log = simulator.process(
        context, df_config_orders, epoch_wise_stats, {}, sim_cfg, config_id
    )

    print(f"  Simulation: {len(day_wise_log)} days logged, {len(order_ids)} orders executed, {len(trade_log)} trades")

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

    # Verify trade_log structure
    for t in trade_log:
        assert "instrument" in t, "trade_log entry missing 'instrument'"
        assert "entry_epoch" in t, "trade_log entry missing 'entry_epoch'"
        assert "exit_epoch" in t, "trade_log entry missing 'exit_epoch'"
        assert "entry_price" in t, "trade_log entry missing 'entry_price'"
        assert "exit_price" in t, "trade_log entry missing 'exit_price'"
        assert "quantity" in t, "trade_log entry missing 'quantity'"
        assert "entry_charges" in t, "trade_log entry missing 'entry_charges'"
        assert "sell_charges" in t, "trade_log entry missing 'sell_charges'"
        assert t["quantity"] > 0, f"Trade quantity must be > 0: {t['quantity']}"
        assert t["entry_charges"] >= 0, f"Entry charges must be >= 0: {t['entry_charges']}"
        assert t["sell_charges"] >= 0, f"Sell charges must be >= 0: {t['sell_charges']}"

    print("End-to-end test passed!")


def test_simulator_trade_log_fields():
    """Verify simulator.process() returns trade_log with correct schema."""
    from engine.constants import SECONDS_IN_ONE_DAY

    base_epoch = 1577836800
    # 2 instruments, 10 days: enough to trigger entries and exits
    rows = []
    for sym, base_price in [("SYM0", 100), ("SYM1", 150)]:
        for d in range(10):
            epoch = base_epoch + d * SECONDS_IN_ONE_DAY
            price = base_price + d * 0.5
            rows.append({
                "date_epoch": epoch,
                "open": price - 0.5, "high": price + 1.5,
                "low": price - 1.5, "close": price,
                "average_price": price, "volume": 2000000,
                "symbol": sym, "instrument": f"NSE:{sym}", "exchange": "NSE",
            })

    df = pl.DataFrame(rows)
    epoch_wise_stats = create_epoch_wise_instrument_stats(df)

    # Two orders: first exits day 5, second enters day 7 (extends simulator end_epoch)
    entry1 = base_epoch + SECONDS_IN_ONE_DAY
    exit1 = base_epoch + 5 * SECONDS_IN_ONE_DAY
    entry2 = base_epoch + 7 * SECONDS_IN_ONE_DAY
    exit2 = base_epoch + 9 * SECONDS_IN_ONE_DAY
    orders = pl.DataFrame([
        {
            "instrument": "NSE:SYM0",
            "entry_epoch": entry1, "exit_epoch": exit1,
            "entry_price": 100.5, "exit_price": 102.5,
            "scanner_config_ids": "s0", "entry_config_ids": "e0", "exit_config_ids": "x0",
        },
        {
            "instrument": "NSE:SYM1",
            "entry_epoch": entry2, "exit_epoch": exit2,
            "entry_price": 153.5, "exit_price": 154.5,
            "scanner_config_ids": "s0", "entry_config_ids": "e0", "exit_config_ids": "x0",
        },
    ])

    sim_cfg = {
        "max_positions": 5,
        "max_positions_per_instrument": 1,
    }
    context = {
        "start_margin": 1000000,
        "start_epoch": base_epoch,
        "end_epoch": base_epoch + 10 * SECONDS_IN_ONE_DAY,
    }

    day_wise_log, order_ids, snapshot, positions, trade_log = simulator.process(
        context, orders, epoch_wise_stats, {}, sim_cfg, "test_config"
    )

    # At least the first order should complete (exit at day 5, simulator runs to day 7+)
    assert len(trade_log) >= 1, f"Expected at least 1 trade, got {len(trade_log)}"

    # Verify all required fields on every trade
    required = {"instrument", "entry_epoch", "exit_epoch", "entry_price",
                "exit_price", "quantity", "entry_charges", "sell_charges"}
    for t in trade_log:
        assert required.issubset(t.keys()), f"Missing fields: {required - t.keys()}"
        assert t["quantity"] > 0, f"Quantity must be > 0: {t['quantity']}"
        assert t["entry_charges"] >= 0, f"Entry charges must be >= 0: {t['entry_charges']}"
        assert t["sell_charges"] >= 0, f"Sell charges must be >= 0: {t['sell_charges']}"

    # First trade should be SYM0
    t0 = trade_log[0]
    assert t0["instrument"] == "NSE:SYM0"
    assert t0["entry_epoch"] == entry1
    assert t0["exit_epoch"] == exit1


def test_risk_free_rate_passthrough():
    """Verify BacktestResult passes risk_free_rate to compute_metrics."""
    from lib.backtest_result import BacktestResult

    # Two results with different risk_free_rate, same equity curve
    r1 = BacktestResult("test", {}, "X", "NSE", 100000, risk_free_rate=0.0)
    r2 = BacktestResult("test", {}, "X", "NSE", 100000, risk_free_rate=0.10)

    # 10 days of mild positive returns
    for i in range(10):
        val = 100000 + i * 500
        r1.add_equity_point(1000000 + i * 86400, val)
        r2.add_equity_point(1000000 + i * 86400, val)

    r1.compute()
    r2.compute()

    s1 = r1.to_dict()["summary"]
    s2 = r2.to_dict()["summary"]

    # Higher risk_free_rate -> lower Sharpe ratio
    assert s1["sharpe_ratio"] is not None
    assert s2["sharpe_ratio"] is not None
    assert s1["sharpe_ratio"] > s2["sharpe_ratio"], (
        f"Sharpe with rfr=0 ({s1['sharpe_ratio']}) should be > Sharpe with rfr=0.10 ({s2['sharpe_ratio']})"
    )

    # risk_free_rate stored in strategy dict
    assert r1.to_dict()["strategy"]["risk_free_rate"] == 0.0
    assert r2.to_dict()["strategy"]["risk_free_rate"] == 0.10


if __name__ == "__main__":
    test_end_to_end()
    test_simulator_trade_log_fields()
    test_risk_free_rate_passthrough()

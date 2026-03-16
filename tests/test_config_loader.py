"""Tests for engine/config_loader.py."""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config_loader import load_config, validate_config


SAMPLE_YAML = """
static:
  start_margin: 500000
  start_epoch: 1577836800
  end_epoch: 1735689600
  prefetch_days: 200
  data_granularity: day

scanner:
  instruments:
    - [{exchange: NSE, symbols: []}]
  price_threshold: [50, 100]
  avg_day_transaction_threshold:
    - {period: 125, threshold: 70000000}
  n_day_gain_threshold:
    - {n: 360, threshold: 0}

entry:
  n_day_ma: [3, 5]
  n_day_high: [2]
  direction_score:
    - {n_day_ma: 3, score: 0.54}

exit:
  min_hold_time_days: [0, 4]
  trailing_stop_loss: [15]

simulation:
  default_sorting_type: [top_gainer]
  order_sorting_type: [top_performer]
  order_ranking_window_days: [180]
  max_positions: [20]
  max_positions_per_instrument: [1]
  order_value_multiplier: [1]
  max_order_value:
    - {type: percentage_of_instrument_avg_txn, value: 4.5}
"""


def test_load_config():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        f.flush()
        config = load_config(f.name)
    os.unlink(f.name)

    assert "scanner_config_input" in config
    assert "entry_config_input" in config
    assert "exit_config_input" in config
    assert "simulation_config_input" in config
    assert "static_config" in config

    assert config["static_config"]["start_margin"] == 500000
    assert config["static_config"]["prefetch_days"] == 200
    assert len(config["scanner_config_input"]["price_threshold"]) == 2
    assert len(config["entry_config_input"]["n_day_ma"]) == 2
    assert len(config["exit_config_input"]["min_hold_time_days"]) == 2


def test_validate_config_bad_epochs():
    config = {
        "scanner_config_input": {"price_threshold": [50]},
        "entry_config_input": {"n_day_ma": [3]},
        "exit_config_input": {"trailing_stop_loss": [15], "min_hold_time_days": [0]},
        "simulation_config_input": {"max_positions": [20]},
        "static_config": {"start_epoch": 200, "end_epoch": 100, "prefetch_days": 400},
    }
    try:
        validate_config(config)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "start_epoch" in str(e)


def test_validate_config_bad_max_positions():
    config = {
        "scanner_config_input": {"price_threshold": [50]},
        "entry_config_input": {"n_day_ma": [3]},
        "exit_config_input": {"trailing_stop_loss": [15], "min_hold_time_days": [0]},
        "simulation_config_input": {"max_positions": [0]},
        "static_config": {"start_epoch": 100, "end_epoch": 200, "prefetch_days": 400},
    }
    try:
        validate_config(config)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "max_positions" in str(e)


def test_defaults():
    """Minimal YAML should get defaults for everything."""
    minimal_yaml = "static:\n  start_margin: 100000\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(minimal_yaml)
        f.flush()
        config = load_config(f.name)
    os.unlink(f.name)

    assert config["static_config"]["start_margin"] == 100000
    assert config["static_config"]["prefetch_days"] == 400
    assert len(config["scanner_config_input"]["price_threshold"]) == 1


if __name__ == "__main__":
    test_load_config()
    test_validate_config_bad_epochs()
    test_validate_config_bad_max_positions()
    test_defaults()
    print("All config_loader tests passed!")

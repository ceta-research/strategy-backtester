"""YAML config loader and config iterator helpers.

Replaces the hardcoded scanner_config.py, entry_config.py, exit_config.py,
simulation_config.py from ATO_Simulator with YAML-driven configuration.
Supports per-strategy config schemas via strategy_type in static section.
"""

import yaml

from engine.config_sweep import create_config_iterator
from engine.constants import SECONDS_IN_ONE_DAY


def load_config(yaml_path: str) -> dict:
    """Load YAML config and return structured dict.

    Dispatches to strategy-specific config builders based on static.strategy_type.
    Default: eod_technical (backward compatible).

    Returns dict with keys:
        scanner_config_input, entry_config_input, exit_config_input,
        simulation_config_input, static_config
    """
    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    static = _build_static_config(raw.get("static", {}))
    strategy_type = static.get("strategy_type", "eod_technical")

    # Strategy-specific entry/exit config builders
    entry_builder = _ENTRY_BUILDERS.get(strategy_type, _build_entry_config_eod_technical)
    exit_builder = _EXIT_BUILDERS.get(strategy_type, _build_exit_config_eod_technical)

    config = {
        "scanner_config_input": _build_scanner_config(raw.get("scanner", {})),
        "entry_config_input": entry_builder(raw.get("entry", {})),
        "exit_config_input": exit_builder(raw.get("exit", {})),
        "simulation_config_input": _build_simulation_config(raw.get("simulation", {})),
        "static_config": static,
    }

    validate_config(config)
    return config


# ---------------------------------------------------------------------------
# Scanner config (shared across strategies)
# ---------------------------------------------------------------------------

def _build_scanner_config(scanner: dict) -> dict:
    return {
        "instruments": scanner.get("instruments", [[{"exchange": "NSE", "symbols": []}]]),
        "price_threshold": scanner.get("price_threshold", [50]),
        "avg_day_transaction_threshold": scanner.get("avg_day_transaction_threshold", [
            {"period": 125, "threshold": 70000000}
        ]),
        "n_day_gain_threshold": scanner.get("n_day_gain_threshold", [
            {"n": 360, "threshold": 0}
        ]),
    }


# ---------------------------------------------------------------------------
# EOD Technical entry/exit (original strategy)
# ---------------------------------------------------------------------------

def _build_entry_config_eod_technical(entry: dict) -> dict:
    return {
        "n_day_ma": entry.get("n_day_ma", [3]),
        "n_day_high": entry.get("n_day_high", [2]),
        "direction_score": entry.get("direction_score", [
            {"n_day_ma": 3, "score": 0.54}
        ]),
    }


def _build_exit_config_eod_technical(exit_cfg: dict) -> dict:
    return {
        "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
        "trailing_stop_loss": exit_cfg.get("trailing_stop_loss", [15]),
    }


# ---------------------------------------------------------------------------
# Connors RSI entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_connors_rsi(entry: dict) -> dict:
    return {
        "rsi_period": entry.get("rsi_period", [2]),
        "rsi_entry_threshold": entry.get("rsi_entry_threshold", [5]),
        "sma_trend_period": entry.get("sma_trend_period", [200]),
    }


def _build_exit_config_connors_rsi(exit_cfg: dict) -> dict:
    return {
        "exit_sma_period": exit_cfg.get("exit_sma_period", [5]),
        "max_hold_days": exit_cfg.get("max_hold_days", [20]),
    }


# ---------------------------------------------------------------------------
# IBS Mean Reversion entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_ibs(entry: dict) -> dict:
    return {
        "ibs_entry_threshold": entry.get("ibs_entry_threshold", [0.2]),
        "sma_trend_period": entry.get("sma_trend_period", [200]),
    }


def _build_exit_config_ibs(exit_cfg: dict) -> dict:
    return {
        "ibs_exit_threshold": exit_cfg.get("ibs_exit_threshold", [0.8]),
        "max_hold_days": exit_cfg.get("max_hold_days", [10]),
    }


# ---------------------------------------------------------------------------
# Strategy dispatch tables
# ---------------------------------------------------------------------------

_ENTRY_BUILDERS = {
    "eod_technical": _build_entry_config_eod_technical,
    "connors_rsi": _build_entry_config_connors_rsi,
    "ibs_mean_reversion": _build_entry_config_ibs,
}

_EXIT_BUILDERS = {
    "eod_technical": _build_exit_config_eod_technical,
    "connors_rsi": _build_exit_config_connors_rsi,
    "ibs_mean_reversion": _build_exit_config_ibs,
}


# ---------------------------------------------------------------------------
# Simulation config (shared across strategies)
# ---------------------------------------------------------------------------

def _build_simulation_config(sim: dict) -> dict:
    return {
        "default_sorting_type": sim.get("default_sorting_type", ["top_gainer"]),
        "order_sorting_type": sim.get("order_sorting_type", ["top_gainer"]),
        "order_ranking_window_days": sim.get("order_ranking_window_days", [180]),
        "max_positions": sim.get("max_positions", [20]),
        "max_positions_per_instrument": sim.get("max_positions_per_instrument", [1]),
        "order_value_multiplier": sim.get("order_value_multiplier", [1]),
        "max_order_value": sim.get("max_order_value", [
            {"type": "percentage_of_instrument_avg_txn", "value": 4.5}
        ]),
    }


def _build_static_config(static: dict) -> dict:
    return {
        "start_margin": static.get("start_margin", 1000000),
        "start_epoch": static.get("start_epoch", 1577836800),
        "end_epoch": static.get("end_epoch", 1735689600),
        "prefetch_days": static.get("prefetch_days", 400),
        "data_granularity": static.get("data_granularity", "day"),
        "strategy_type": static.get("strategy_type", "eod_technical"),
    }


def validate_config(config: dict) -> None:
    """Validate config structure. Raises ValueError on issues."""
    required_sections = [
        "scanner_config_input", "entry_config_input", "exit_config_input",
        "simulation_config_input", "static_config",
    ]
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing config section: {section}")

    static = config["static_config"]
    if static["start_epoch"] >= static["end_epoch"]:
        raise ValueError("start_epoch must be less than end_epoch")

    sim = config["simulation_config_input"]
    for mp in sim["max_positions"]:
        if mp <= 0:
            raise ValueError("max_positions must be > 0")

    # Strategy-specific validation
    strategy_type = static.get("strategy_type", "eod_technical")
    if strategy_type == "eod_technical":
        for tsl in config["exit_config_input"]["trailing_stop_loss"]:
            if tsl <= 0:
                raise ValueError("trailing_stop_loss must be > 0")


def get_scanner_config_iterator(context):
    _, config_iterator = create_config_iterator(**context["scanner_config_input"])
    return config_iterator


def get_entry_config_iterator(context):
    _, config_iterator = create_config_iterator(**context["entry_config_input"])
    return config_iterator


def get_exit_config_iterator(context):
    _, config_iterator = create_config_iterator(**context["exit_config_input"])
    return config_iterator


def get_simulation_config_iterator(context):
    _, config_iterator = create_config_iterator(**context["simulation_config_input"])
    return config_iterator

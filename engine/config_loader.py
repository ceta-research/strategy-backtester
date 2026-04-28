"""YAML config loader and config iterator helpers.

Replaces the hardcoded scanner_config.py, entry_config.py, exit_config.py,
simulation_config.py from ATO_Simulator with YAML-driven configuration.
Supports per-strategy config schemas via strategy_type in static section.
"""

import yaml

from engine.config_sweep import create_config_iterator


def load_config(yaml_path: str) -> dict:
    """Load YAML config and return structured dict.

    Dispatches to strategy-specific config builders based on static.strategy_type.
    Each signal generator class defines its own build_entry_config() and
    build_exit_config() static methods. Falls back to default builders for
    strategies that don't define their own.

    Returns dict with keys:
        scanner_config_input, entry_config_input, exit_config_input,
        simulation_config_input, static_config
    """
    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    static = _build_static_config(raw.get("static", {}))
    strategy_type = static.get("strategy_type", "eod_technical")

    # Lazy import to avoid circular dependency (base.py imports from config_loader).
    # Importing engine.signals triggers registration of all signal generators.
    import engine.signals  # noqa: F401
    from engine.signals.base import _STRATEGY_REGISTRY

    # Strategy-specific entry/exit config builders (defined on signal generator classes)
    cls = _STRATEGY_REGISTRY.get(strategy_type)
    entry_cfg = raw.get("entry", {})
    exit_cfg = raw.get("exit", {})

    if cls and hasattr(cls, "build_entry_config"):
        entry_config = cls.build_entry_config(entry_cfg)
    else:
        entry_config = _build_entry_config_default(entry_cfg)

    if cls and hasattr(cls, "build_exit_config"):
        exit_config = cls.build_exit_config(exit_cfg)
    else:
        exit_config = _build_exit_config_default(exit_cfg)

    config = {
        "scanner_config_input": _build_scanner_config(raw.get("scanner", {})),
        "entry_config_input": entry_config,
        "exit_config_input": exit_config,
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
# Default entry/exit config builders (fallback for strategies without custom builders)
# ---------------------------------------------------------------------------

def _build_entry_config_default(entry: dict) -> dict:
    return {
        "n_day_ma": entry.get("n_day_ma", [3]),
        "n_day_high": entry.get("n_day_high", [2]),
        "direction_score": entry.get("direction_score", [
            {"n_day_ma": 3, "score": 0.54}
        ]),
        # Phase 4 (2026-04-28): optional flag to disable the
        # `close > n_day_ma` clause from add_entry_signal_inplace.
        # Default False preserves byte-identical behavior on existing
        # configs. Used to test the audit finding that the MA clause
        # is essentially redundant on eod_technical (conditional_fail_rate
        # 0.13%).
        "disable_close_gt_ma": entry.get("disable_close_gt_ma", [False]),
    }


def _build_exit_config_default(exit_cfg: dict) -> dict:
    return {
        "min_hold_time_days": exit_cfg.get("min_hold_time_days", [0]),
        "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [15]),
        # Phase 5 adaptive TSL (2026-04-28): once MFE from entry exceeds
        # tsl_tighten_after_pct, the effective TSL tightens to tsl_tight_pct.
        # Default 999 = disabled (byte-identical to legacy behavior).
        "tsl_tighten_after_pct": exit_cfg.get("tsl_tighten_after_pct", [999]),
        "tsl_tight_pct": exit_cfg.get("tsl_tight_pct", [0]),
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
        "exit_before_entry": sim.get("exit_before_entry", [False]),
    }


def _build_static_config(static: dict) -> dict:
    return {
        "start_margin": static.get("start_margin", 1000000),
        "start_epoch": static.get("start_epoch", 1577836800),
        "end_epoch": static.get("end_epoch", 1735689600),
        "prefetch_days": static.get("prefetch_days", 400),
        "data_granularity": static.get("data_granularity", "day"),
        "strategy_type": static.get("strategy_type", "eod_technical"),
        "data_provider": static.get("data_provider", "cr"),
        # Simulator
        "slippage_rate": static.get("slippage_rate", 0.0005),
        # Order generator
        "multiprocessing_workers": static.get("multiprocessing_workers", 4),
        "anomalous_drop_threshold_pct": static.get("anomalous_drop_threshold_pct", 20),
        # CR API resources (for CRDataProvider)
        "cr_api_timeout": static.get("cr_api_timeout", 600),
        "cr_api_memory_mb": static.get("cr_api_memory_mb", 16384),
        "cr_api_threads": static.get("cr_api_threads", 6),
        "cr_api_disk_mb": static.get("cr_api_disk_mb", 40960),
        # Price oscillation filter
        "price_oscillation_spike": static.get("price_oscillation_spike", 2.0),
        "price_oscillation_mild": static.get("price_oscillation_mild", 1.3),
        "price_oscillation_min_count": static.get("price_oscillation_min_count", 5),
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
        for tsl in config["exit_config_input"]["trailing_stop_pct"]:
            if tsl <= 0:
                raise ValueError("trailing_stop_pct must be > 0")


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

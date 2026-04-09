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
# Gap Fill entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_gap_fill(entry: dict) -> dict:
    return {
        "min_gap_down_pct": entry.get("min_gap_down_pct", [0.01]),
        "max_gap_down_pct": entry.get("max_gap_down_pct", [0.04]),
    }


def _build_exit_config_gap_fill(exit_cfg: dict) -> dict:
    return {
        "exit_at": exit_cfg.get("exit_at", ["close"]),
        "max_hold_days": exit_cfg.get("max_hold_days", [1]),
    }


# ---------------------------------------------------------------------------
# Overnight Hold entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_overnight_hold(entry: dict) -> dict:
    return {
        "buy_on_down_day": entry.get("buy_on_down_day", [False]),
        "min_rsi_14": entry.get("min_rsi_14", [0]),
    }


def _build_exit_config_overnight_hold(exit_cfg: dict) -> dict:
    return {
        "exit_at": exit_cfg.get("exit_at", ["next_open"]),
        "max_hold_days": exit_cfg.get("max_hold_days", [1]),
    }


# ---------------------------------------------------------------------------
# Darvas Box entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_darvas_box(entry: dict) -> dict:
    return {
        "box_min_days": entry.get("box_min_days", [10]),
        "volume_breakout_mult": entry.get("volume_breakout_mult", [1.5]),
    }


def _build_exit_config_darvas_box(exit_cfg: dict) -> dict:
    return {
        "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [0.08]),
        "max_hold_days": exit_cfg.get("max_hold_days", [30]),
    }


# ---------------------------------------------------------------------------
# Swing Master entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_swing_master(entry: dict) -> dict:
    return {
        "sma_short": entry.get("sma_short", [10]),
        "sma_long": entry.get("sma_long", [20]),
        "pullback_days": entry.get("pullback_days", [3]),
    }


def _build_exit_config_swing_master(exit_cfg: dict) -> dict:
    return {
        "target_pct": exit_cfg.get("target_pct", [0.07]),
        "stop_pct": exit_cfg.get("stop_pct", [0.04]),
        "max_hold_days": exit_cfg.get("max_hold_days", [20]),
        "trailing_buffer_pct": exit_cfg.get("trailing_buffer_pct", [0.002]),
    }


# ---------------------------------------------------------------------------
# Squeeze entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_squeeze(entry: dict) -> dict:
    return {
        "bb_period": entry.get("bb_period", [20]),
        "bb_std": entry.get("bb_std", [2.0]),
        "kc_period": entry.get("kc_period", [20]),
        "kc_mult": entry.get("kc_mult", [1.5]),
        "mom_period": entry.get("mom_period", [12]),
    }


def _build_exit_config_squeeze(exit_cfg: dict) -> dict:
    return {
        "stop_pct": exit_cfg.get("stop_pct", [0.05]),
        "max_hold_days": exit_cfg.get("max_hold_days", [20]),
    }


# ---------------------------------------------------------------------------
# HOLP/LOHP entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_holp_lohp(entry: dict) -> dict:
    return {
        "lookback_period": entry.get("lookback_period", [20]),
    }


def _build_exit_config_holp_lohp(exit_cfg: dict) -> dict:
    return {
        "trailing_start_day": exit_cfg.get("trailing_start_day", [3]),
        "max_hold_days": exit_cfg.get("max_hold_days", [20]),
    }


# ---------------------------------------------------------------------------
# Trending Value entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_trending_value(entry: dict) -> dict:
    return {
        "max_debt_to_assets": entry.get("max_debt_to_assets", [0.6]),
        "min_roe": entry.get("min_roe", [0.0]),
        "growth_lookback_years": entry.get("growth_lookback_years", [3]),
        "growth_weights": entry.get("growth_weights", [
            {"revenue": 0.5, "earnings": 0.5}
        ]),
        "top_n_stocks": entry.get("top_n_stocks", [20]),
        "rebalance_frequency": entry.get("rebalance_frequency", ["quarterly"]),
    }


def _build_exit_config_trending_value(exit_cfg: dict) -> dict:
    return {
        "min_hold_days": exit_cfg.get("min_hold_days", [365]),
        "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [0.20]),
    }


# ---------------------------------------------------------------------------
# Momentum Dip-Buy entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_momentum_dip(entry: dict) -> dict:
    return {
        "rsi_threshold": entry.get("rsi_threshold", [30]),
        "momentum_lookback_days": entry.get("momentum_lookback_days", [126]),
        "top_n": entry.get("top_n", [50]),
        "rerank_interval_days": entry.get("rerank_interval_days", [21]),
    }


def _build_exit_config_momentum_dip(exit_cfg: dict) -> dict:
    return {
        "profit_target_pct": exit_cfg.get("profit_target_pct", [0.03]),
        "max_hold_days": exit_cfg.get("max_hold_days", [10]),
    }


# ---------------------------------------------------------------------------
# Extended IBS Mean Reversion entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_extended_ibs(entry: dict) -> dict:
    return {
        "ibs_threshold": entry.get("ibs_threshold", [0.3]),
        "sma_trend_period": entry.get("sma_trend_period", [0]),
        "vei_max": entry.get("vei_max", [0]),
    }


def _build_exit_config_extended_ibs(exit_cfg: dict) -> dict:
    return {
        "max_hold_days": exit_cfg.get("max_hold_days", [30]),
        "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
        "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [0]),
    }


# ---------------------------------------------------------------------------
# BB Mean Reversion entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_bb_mean_reversion(entry: dict) -> dict:
    return {
        "bb_period": entry.get("bb_period", [20]),
        "bb_std": entry.get("bb_std", [2.0]),
        "sma_trend_period": entry.get("sma_trend_period", [200]),
    }


def _build_exit_config_bb_mean_reversion(exit_cfg: dict) -> dict:
    return {
        "max_hold_days": exit_cfg.get("max_hold_days", [400]),
    }


# ---------------------------------------------------------------------------
# Factor Composite entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_factor_composite(entry: dict) -> dict:
    return {
        "momentum_lookback_days": entry.get("momentum_lookback_days", [252]),
        "momentum_skip_days": entry.get("momentum_skip_days", [21]),
        "factor_weights": entry.get("factor_weights", [
            {"momentum": 0.4, "gross_profitability": 0.3, "value": 0.3}
        ]),
        "regime_filter_sma": entry.get("regime_filter_sma", [200]),
        "top_n_stocks": entry.get("top_n_stocks", [30]),
    }


def _build_exit_config_factor_composite(exit_cfg: dict) -> dict:
    return {
        "vol_target_annual": exit_cfg.get("vol_target_annual", [0.15]),
        "vol_lookback_days": exit_cfg.get("vol_lookback_days", [126]),
        "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0.15]),
    }


# ---------------------------------------------------------------------------
# Index Green Candle Momentum entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_index_green_candle(entry: dict) -> dict:
    return {
        "green_candles": entry.get("green_candles", [2]),
    }


def _build_exit_config_index_green_candle(exit_cfg: dict) -> dict:
    return {
        "red_candles_exit": exit_cfg.get("red_candles_exit", [1]),
        "take_profit_pct": exit_cfg.get("take_profit_pct", [0]),
        "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
    }


# ---------------------------------------------------------------------------
# Index SMA Crossover entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_index_sma_crossover(entry: dict) -> dict:
    return {
        "sma_short": entry.get("sma_short", [10]),
        "sma_long": entry.get("sma_long", [50]),
    }


def _build_exit_config_index_sma_crossover(exit_cfg: dict) -> dict:
    return {
        "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
        "max_hold_days": exit_cfg.get("max_hold_days", [0]),
    }


# ---------------------------------------------------------------------------
# Index Dip-Buy entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_index_dip_buy(entry: dict) -> dict:
    return {
        "sma_short": entry.get("sma_short", [20]),
        "sma_long": entry.get("sma_long", [200]),
        "rsi_threshold": entry.get("rsi_threshold", [0]),
    }


def _build_exit_config_index_dip_buy(exit_cfg: dict) -> dict:
    return {
        "max_hold_days": exit_cfg.get("max_hold_days", [20]),
        "stop_loss_pct": exit_cfg.get("stop_loss_pct", [0]),
    }


# ---------------------------------------------------------------------------
# Quality Dip-Buy entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_quality_dip_buy(entry: dict) -> dict:
    return {
        "consecutive_positive_years": entry.get("consecutive_positive_years", [3]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "dip_threshold_pct": entry.get("dip_threshold_pct", [10]),
        "peak_lookback_days": entry.get("peak_lookback_days", [252]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "max_per_sector": entry.get("max_per_sector", [0]),
        "rsi_threshold": entry.get("rsi_threshold", [0]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_quality_dip_buy(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [0]),
        "max_hold_days": exit_cfg.get("max_hold_days", [0]),
    }


def _build_entry_config_momentum_cascade(entry: dict) -> dict:
    return {
        "fast_lookback_days": entry.get("fast_lookback_days", [42]),
        "slow_lookback_days": entry.get("slow_lookback_days", [126]),
        "accel_threshold_pct": entry.get("accel_threshold_pct", [2]),
        "min_momentum_pct": entry.get("min_momentum_pct", [20]),
        "breakout_window": entry.get("breakout_window", [63]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_momentum_cascade(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [12]),
        "max_hold_days": exit_cfg.get("max_hold_days", [504]),
    }


# ---------------------------------------------------------------------------
# Momentum Dip Quality entry/exit (champion strategy port)
# ---------------------------------------------------------------------------

def _build_entry_config_momentum_dip_quality(entry: dict) -> dict:
    return {
        "consecutive_positive_years": entry.get("consecutive_positive_years", [2]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "momentum_lookback_days": entry.get("momentum_lookback_days", [63]),
        "momentum_percentile": entry.get("momentum_percentile", [0.30]),
        "rerank_interval_days": entry.get("rerank_interval_days", [63]),
        "dip_threshold_pct": entry.get("dip_threshold_pct", [5]),
        "peak_lookback_days": entry.get("peak_lookback_days", [63]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "roe_threshold": entry.get("roe_threshold", [15]),
        "pe_threshold": entry.get("pe_threshold", [25]),
        "de_threshold": entry.get("de_threshold", [0]),
        "fundamental_missing_mode": entry.get("fundamental_missing_mode", ["skip"]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_momentum_dip_quality(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [10]),
        "max_hold_days": exit_cfg.get("max_hold_days", [504]),
    }


# ---------------------------------------------------------------------------
# Index Breakout entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_index_breakout(entry: dict) -> dict:
    return {
        "lookback_days": entry.get("lookback_days", [3]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_index_breakout(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [5]),
        "max_hold_days": exit_cfg.get("max_hold_days", [0]),
    }


# ---------------------------------------------------------------------------
# Earnings Dip entry/exit (earnings surprise + post-earnings dip)
# ---------------------------------------------------------------------------

def _build_entry_config_earnings_dip(entry: dict) -> dict:
    return {
        "consecutive_positive_years": entry.get("consecutive_positive_years", [2]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "surprise_threshold_pct": entry.get("surprise_threshold_pct", [5]),
        "dip_threshold_pct": entry.get("dip_threshold_pct", [5]),
        "post_earnings_window": entry.get("post_earnings_window", [20]),
        "peak_lookback_days": entry.get("peak_lookback_days", [63]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "roe_threshold": entry.get("roe_threshold", [15]),
        "pe_threshold": entry.get("pe_threshold", [25]),
        "de_threshold": entry.get("de_threshold", [0]),
        "fundamental_missing_mode": entry.get("fundamental_missing_mode", ["skip"]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_earnings_dip(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [10]),
        "max_hold_days": exit_cfg.get("max_hold_days", [504]),
    }


# ---------------------------------------------------------------------------
# Forced Selling Dip entry/exit
# ---------------------------------------------------------------------------

def _build_entry_config_forced_selling_dip(entry: dict) -> dict:
    return {
        "consecutive_positive_years": entry.get("consecutive_positive_years", [2]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "sector_lookback_days": entry.get("sector_lookback_days", [20]),
        "dip_threshold_pct": entry.get("dip_threshold_pct", [5]),
        "volume_multiplier": entry.get("volume_multiplier", [2.0]),
        "peak_lookback_days": entry.get("peak_lookback_days", [63]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "roe_threshold": entry.get("roe_threshold", [15]),
        "pe_threshold": entry.get("pe_threshold", [25]),
        "de_threshold": entry.get("de_threshold", [0]),
        "fundamental_missing_mode": entry.get("fundamental_missing_mode", ["skip"]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_forced_selling_dip(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [10]),
        "max_hold_days": exit_cfg.get("max_hold_days", [504]),
    }


# ---------------------------------------------------------------------------
# Momentum Rebalance entry/exit (Jegadeesh-Titman cross-sectional momentum)
# ---------------------------------------------------------------------------

def _build_entry_config_momentum_rebalance(entry: dict) -> dict:
    return {
        "momentum_lookback_days": entry.get("momentum_lookback_days", [126]),
        "rebalance_interval_days": entry.get("rebalance_interval_days", [21]),
        "num_positions": entry.get("num_positions", [10]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_momentum_rebalance(exit_cfg: dict) -> dict:
    return {
        "max_hold_days": exit_cfg.get("max_hold_days", [0]),
    }


# ---------------------------------------------------------------------------
# Quality Dip-Buy Tiered entry/exit (multi-tier averaging)
# ---------------------------------------------------------------------------

def _build_entry_config_quality_dip_tiered(entry: dict) -> dict:
    return {
        "consecutive_positive_years": entry.get("consecutive_positive_years", [2]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "n_tiers": entry.get("n_tiers", [1]),
        "tier_multiplier": entry.get("tier_multiplier", [1.5]),
        "base_dip_threshold_pct": entry.get("base_dip_threshold_pct", [5]),
        "peak_lookback_days": entry.get("peak_lookback_days", [63]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_quality_dip_tiered(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [10]),
        "max_hold_days": exit_cfg.get("max_hold_days", [504]),
    }


# ---------------------------------------------------------------------------
# Enhanced Breakout entry/exit (multi-layer confirmed breakout)
# ---------------------------------------------------------------------------

def _build_entry_config_enhanced_breakout(entry: dict) -> dict:
    return {
        "breakout_window": entry.get("breakout_window", [2]),
        "consecutive_positive_years": entry.get("consecutive_positive_years", [2]),
        "min_yearly_return_pct": entry.get("min_yearly_return_pct", [0]),
        "momentum_lookback_days": entry.get("momentum_lookback_days", [63]),
        "momentum_percentile": entry.get("momentum_percentile", [0.30]),
        "rerank_interval_days": entry.get("rerank_interval_days", [63]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
        "volume_multiplier": entry.get("volume_multiplier", [0]),
        "volume_avg_period": entry.get("volume_avg_period", [20]),
        "roe_threshold": entry.get("roe_threshold", [0]),
        "pe_threshold": entry.get("pe_threshold", [0]),
        "de_threshold": entry.get("de_threshold", [0]),
        "fundamental_missing_mode": entry.get("fundamental_missing_mode", ["skip"]),
        "regime_instrument": entry.get("regime_instrument", [""]),
        "regime_sma_period": entry.get("regime_sma_period", [0]),
    }


def _build_exit_config_enhanced_breakout(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [12]),
        "max_hold_days": exit_cfg.get("max_hold_days", [252]),
    }


def _build_entry_config_ml_supertrend(entry: dict) -> dict:
    return {
        "lookback_years": entry.get("lookback_years", [10]),
        "min_positive_years": entry.get("min_positive_years", [8]),
        "dip_threshold_pct": entry.get("dip_threshold_pct", [10]),
        "peak_lookback_days": entry.get("peak_lookback_days", [252]),
        "max_osc_position": entry.get("max_osc_position", [0.50]),
        "supertrend_mode": entry.get("supertrend_mode", ["reversal"]),
        "atr_period": entry.get("atr_period", [20]),
        "atr_multiplier": entry.get("atr_multiplier", [2.0]),
        "bounce_threshold_pct": entry.get("bounce_threshold_pct", [2.0]),
        "st_flip_lookback": entry.get("st_flip_lookback", [1]),
        "rescreen_interval_days": entry.get("rescreen_interval_days", [63]),
    }


def _build_exit_config_ml_supertrend(exit_cfg: dict) -> dict:
    return {
        "tsl_pct": exit_cfg.get("tsl_pct", [10]),
        "max_hold_days": exit_cfg.get("max_hold_days", [252]),
        "supertrend_exit": exit_cfg.get("supertrend_exit", [False]),
    }


# ---------------------------------------------------------------------------
# Strategy dispatch tables
# ---------------------------------------------------------------------------

_ENTRY_BUILDERS = {
    "eod_technical": _build_entry_config_eod_technical,
    "connors_rsi": _build_entry_config_connors_rsi,
    "ibs_mean_reversion": _build_entry_config_ibs,
    "gap_fill": _build_entry_config_gap_fill,
    "overnight_hold": _build_entry_config_overnight_hold,
    "darvas_box": _build_entry_config_darvas_box,
    "swing_master": _build_entry_config_swing_master,
    "squeeze": _build_entry_config_squeeze,
    "holp_lohp": _build_entry_config_holp_lohp,
    "factor_composite": _build_entry_config_factor_composite,
    "trending_value": _build_entry_config_trending_value,
    "bb_mean_reversion": _build_entry_config_bb_mean_reversion,
    "extended_ibs": _build_entry_config_extended_ibs,
    "momentum_dip": _build_entry_config_momentum_dip,
    "index_green_candle": _build_entry_config_index_green_candle,
    "index_sma_crossover": _build_entry_config_index_sma_crossover,
    "index_dip_buy": _build_entry_config_index_dip_buy,
    "quality_dip_buy": _build_entry_config_quality_dip_buy,
    "momentum_cascade": _build_entry_config_momentum_cascade,
    "momentum_dip_quality": _build_entry_config_momentum_dip_quality,
    "forced_selling_dip": _build_entry_config_forced_selling_dip,
    "index_breakout": _build_entry_config_index_breakout,
    "momentum_rebalance": _build_entry_config_momentum_rebalance,
    "earnings_dip": _build_entry_config_earnings_dip,
    "quality_dip_tiered": _build_entry_config_quality_dip_tiered,
    "enhanced_breakout": _build_entry_config_enhanced_breakout,
    "ml_supertrend": _build_entry_config_ml_supertrend,
}

_EXIT_BUILDERS = {
    "eod_technical": _build_exit_config_eod_technical,
    "connors_rsi": _build_exit_config_connors_rsi,
    "ibs_mean_reversion": _build_exit_config_ibs,
    "gap_fill": _build_exit_config_gap_fill,
    "overnight_hold": _build_exit_config_overnight_hold,
    "darvas_box": _build_exit_config_darvas_box,
    "swing_master": _build_exit_config_swing_master,
    "squeeze": _build_exit_config_squeeze,
    "holp_lohp": _build_exit_config_holp_lohp,
    "factor_composite": _build_exit_config_factor_composite,
    "trending_value": _build_exit_config_trending_value,
    "bb_mean_reversion": _build_exit_config_bb_mean_reversion,
    "extended_ibs": _build_exit_config_extended_ibs,
    "momentum_dip": _build_exit_config_momentum_dip,
    "index_green_candle": _build_exit_config_index_green_candle,
    "index_sma_crossover": _build_exit_config_index_sma_crossover,
    "index_dip_buy": _build_exit_config_index_dip_buy,
    "quality_dip_buy": _build_exit_config_quality_dip_buy,
    "momentum_cascade": _build_exit_config_momentum_cascade,
    "momentum_dip_quality": _build_exit_config_momentum_dip_quality,
    "forced_selling_dip": _build_exit_config_forced_selling_dip,
    "index_breakout": _build_exit_config_index_breakout,
    "momentum_rebalance": _build_exit_config_momentum_rebalance,
    "earnings_dip": _build_exit_config_earnings_dip,
    "quality_dip_tiered": _build_exit_config_quality_dip_tiered,
    "enhanced_breakout": _build_exit_config_enhanced_breakout,
    "ml_supertrend": _build_exit_config_ml_supertrend,
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

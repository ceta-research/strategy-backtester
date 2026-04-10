"""Pluggable signal generators for EOD strategies.

Importing this package registers all signal generators with the strategy registry.
"""

from engine.signals import eod_technical  # noqa: F401
from engine.signals import connors_rsi  # noqa: F401
from engine.signals import ibs_reversion  # noqa: F401
from engine.signals import gap_fill  # noqa: F401
from engine.signals import overnight_hold  # noqa: F401
from engine.signals import darvas_box  # noqa: F401
from engine.signals import swing_master  # noqa: F401
from engine.signals import squeeze  # noqa: F401
from engine.signals import holp_lohp  # noqa: F401
from engine.signals import factor_composite  # noqa: F401
from engine.signals import trending_value  # noqa: F401
from engine.signals import bb_mean_reversion  # noqa: F401
from engine.signals import extended_ibs  # noqa: F401
from engine.signals import momentum_dip  # noqa: F401
from engine.signals import index_green_candle  # noqa: F401
from engine.signals import index_sma_crossover  # noqa: F401
from engine.signals import index_dip_buy  # noqa: F401
from engine.signals import quality_dip_buy  # noqa: F401
from engine.signals import low_pe  # noqa: F401
from engine.signals import momentum_cascade  # noqa: F401
from engine.signals import momentum_dip_quality  # noqa: F401
from engine.signals import forced_selling_dip  # noqa: F401
from engine.signals import index_breakout  # noqa: F401
from engine.signals import momentum_rebalance  # noqa: F401
from engine.signals import earnings_dip  # noqa: F401
from engine.signals import quality_dip_tiered  # noqa: F401
from engine.signals import enhanced_breakout  # noqa: F401
from engine.signals import ml_supertrend  # noqa: F401
from engine.signals import momentum_top_gainers  # noqa: F401
from engine.signals import eod_breakout  # noqa: F401

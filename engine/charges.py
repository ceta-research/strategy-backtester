"""Broker charge calculations.

All functions named `calculate_*` return PER-SIDE charges. Buy and sell legs
are computed separately; round-trip cost is the sum of the two. Functions
named `*_round_trip_*` return the two-leg total.

Historical bug (fixed 2026-04-21): the non-IN/US fallback returned
`order_value * 0.001` with a comment claiming "0.1% round-trip estimate",
but the caller (simulator.py) invokes it per-side, producing 0.2% round-trip
on every non-IN/US exchange. See docs/AUDIT_FINDINGS.md.

Fee data is organized as module-level constants so adding a new exchange is
editing data, not control flow.

Rate vintage (audit P3.4/P3.5 revisit, 2026-04-21):
  Indian regulatory rates use Zerodha's brokerage calculator as the
  reference (current 2026 rates). Per-exchange non-IN/US schedules use
  max(current, historical) rates — so a single constant gives a
  conservative (upper-bound) cost estimate for backtests spanning multiple
  rate regimes. Each rate cites its source in a trailing comment.

  The golden-value test suite in tests/test_charges.py pins every constant
  so any future rate update is a deliberate, reviewed change.

  Rounding: all charge components are rounded to 2 decimal places (paise
  precision for INR, cents for USD). This matches broker contract-note
  practice. Sub-paise rounding error is negligible at backtest scale.
"""

import logging
from engine.constants import BROKER_KITE, EXCHANGE_BROKER_MAP

_logger = logging.getLogger(__name__)

# Exchanges that fall through to `OTHER_EXCHANGE_PER_SIDE_RATE` — we log a
# one-time warning the first time each is used so silent approximation
# does not go unnoticed in cross-exchange backtests.
_WARNED_FALLBACK_EXCHANGES = set()

# ── India (NSE/BSE) ──────────────────────────────────────────────────────

NSE_BROKERAGE_RATE = 0.0003        # 0.03%, capped per-leg (Zerodha equity intraday)
NSE_BROKERAGE_CAP = 20.0           # Rs 20 per leg (Zerodha max brokerage)
NSE_STT_DELIVERY = 0.001           # 0.1% on BOTH buy and sell (stable since 2013)
NSE_STT_INTRADAY_SELL = 0.00025    # 0.025% on sell side only (stable)
# NSE equity transaction charge: current rate as of 2024 per Zerodha's
# brokerage calculator. Revised downward from the pre-2023 rate of
# 0.00345%. Per-leg fee on notional.
NSE_EXCHANGE_RATE = 0.0000297      # 0.00297% per leg (NSE current 2024+)
NSE_SEBI_RATE = 0.000001           # Rs 10 per crore (stable)
NSE_GST_RATE = 0.18                # on (brokerage + exchange) (stable since 2017)
NSE_STAMP_DUTY_DELIVERY_BUY = 0.00015  # 0.015% buy-side only (uniform from 2020-07)
NSE_STAMP_DUTY_INTRADAY = 0.00003      # 0.003% buy-side only (uniform from 2020-07)

# ── US equities ──────────────────────────────────────────────────────────

# Zero-commission, regulatory fees on sell side only.
US_SEC_FEE_RATE = 0.0000278          # ~$27.80/million, sell (SEC Section 31, 2024)
US_TAF_PER_SHARE = 0.000166          # FINRA TAF per share, sell (2024)
US_TAF_CAP = 8.30                    # FINRA TAF per-transaction cap
US_TAF_ESTIMATED_SHARE_PRICE = 50.0  # used when share count unknown

# ── Non-IN/US detailed schedules ─────────────────────────────────────────
#
# Each exchange below uses `max(current, historical)` rates where the
# historical rate was higher — producing a conservative (upper-bound) cost
# that matches older regimes while not under-pricing newer ones. Stamp
# duties and transaction taxes are the dominant components everywhere
# except XETRA, where there is none.

# LSE (UK). UK Stamp Duty Reserve Tax (SDRT) is 0.5% on BUY only; stable
# since 1986. Sell side is effectively broker-only.
LSE_STAMP_DUTY_BUY = 0.005
LSE_BROKER_RATE = 0.0005             # 0.05% per leg (retail broker approx)

# HKSE (Hong Kong). Stamp duty history:
#   pre-2021-08: 0.10% both sides
#   2021-08..2023-11: 0.13% both sides (raised during budget)
#   2023-11+: 0.10% both sides (reduced back)
# Use the historical max (0.13%) per conservative policy.
HKSE_STAMP_DUTY = 0.0013             # 0.13% both sides (historical max)
HKSE_SFC_LEVY = 0.000027             # 0.0027% both sides (SFC transaction levy)
HKSE_AFRC_LEVY = 0.0000015           # 0.00015% both sides (Accounting and Financial Reporting Council)
HKSE_TRADING_FEE = 0.0000565         # 0.00565% both sides (exchange trading fee)
HKSE_CCASS_RATE = 0.00002            # 0.002% both sides (HKSCC clearing, min HK$2)
HKSE_BROKER_RATE = 0.0015            # 0.15% per leg (retail broker approx)

# XETRA (Germany/Deutsche Börse). No stamp or transaction tax in Germany.
# Xetra exchange trading + clearing fees are ~0.01% combined at retail size.
XETRA_EXCHANGE_FEE = 0.0001          # 0.01% per leg (Xetra + Eurex Clearing)
XETRA_BROKER_RATE = 0.0005           # 0.05% per leg (retail broker approx)

# JPX (Japan, Tokyo Stock Exchange). Securities transaction tax abolished
# 1999; no stamp duty. Broker commission is the dominant cost.
JPX_EXCHANGE_FEE = 0.00005           # 0.005% per leg (JPX trading/clearing)
JPX_BROKER_RATE = 0.001              # 0.1% per leg (retail broker approx)

# KSC (Korea, KRX/KOSPI). Securities transaction tax history:
#   pre-2019: 0.25% sell
#   2019-2022: 0.25% sell
#   2023: 0.23% sell
#   2024+: 0.18% sell (KOSPI includes an additional 0.15% agricultural
#     tax making effective sell-side 0.33%)
# Use historical max for the sec tax (0.25%) + the current agricultural
# tax (0.15%) = 0.40% effective sell-side regulatory. Plus broker.
KSC_SEC_TAX_SELL = 0.0025            # 0.25% sell (historical max)
KSC_AGRICULTURAL_TAX_SELL = 0.0015   # 0.15% sell (KOSPI)
KSC_BROKER_RATE = 0.0005             # 0.05% per leg (retail broker approx)

# TSX (Toronto). Canada abolished its transaction tax; minor OSC/IIROC
# fees. Broker commission dominates.
TSX_EXCHANGE_FEE = 0.00005           # 0.005% per leg (OSC + IIROC combined)
TSX_BROKER_RATE = 0.001              # 0.1% per leg (retail broker approx)

# ASX (Australia). No stamp duty; ASIC trading-based fees.
ASX_EXCHANGE_FEE = 0.000028          # 0.0028% per leg (ASIC listed equities)
ASX_BROKER_RATE = 0.001              # 0.1% per leg (retail broker approx)

# Fallback for exchanges without explicit schedules (SAO, SES, SHH, SHZ,
# TAI, JNB, TWO, SET, JKT, PAR). Coarse 0.05%/side = 0.1% round-trip.
# A one-time warning fires in calculate_charges() so backtest users see
# the approximation.
OTHER_EXCHANGE_PER_SIDE_RATE = 0.0005

US_EXCHANGES = frozenset({"US", "NASDAQ", "NYSE", "AMEX"})
INDIA_EXCHANGES = frozenset({"NSE", "BSE"})


# ── Public API ───────────────────────────────────────────────────────────

def get_primary_broker(exchange: str) -> str:
    return EXCHANGE_BROKER_MAP.get(exchange.upper(), BROKER_KITE)


def calculate_charges(exchange, order_value, segment="EQUITY",
                      trade_type="DELIVERY", which_side="BUY_SIDE"):
    """PER-SIDE charges for a single-leg trade.

    Args:
        exchange: Exchange code. Recognised explicitly: NSE, BSE, US,
            NASDAQ, NYSE, AMEX, LSE, HKSE, XETRA, JPX, KSC, TSX, ASX.
            Anything else falls through to a coarse 0.05%/side estimate
            with a one-time warning.
        order_value: Notional for this leg (local currency).
        segment: "EQUITY" (other segments not yet modeled).
        trade_type: "DELIVERY" or "INTRADAY".
        which_side: "BUY_SIDE" or "SELL_SIDE".

    Returns:
        float: Total charges for THIS LEG ONLY. Round-trip cost is
        calculate_charges(buy) + calculate_charges(sell).
    """
    if exchange in US_EXCHANGES:
        return _us_per_side(order_value, which_side)
    if exchange in INDIA_EXCHANGES:
        return _india_per_side(order_value, segment, trade_type, which_side)
    if exchange == "LSE":
        return _lse_per_side(order_value, which_side)
    if exchange == "HKSE":
        return _hkse_per_side(order_value, which_side)
    if exchange == "XETRA":
        return _xetra_per_side(order_value, which_side)
    if exchange == "JPX":
        return _jpx_per_side(order_value, which_side)
    if exchange == "KSC":
        return _ksc_per_side(order_value, which_side)
    if exchange == "TSX":
        return _tsx_per_side(order_value, which_side)
    if exchange == "ASX":
        return _asx_per_side(order_value, which_side)
    # Fallback: per-side flat rate. Warn once per unknown exchange so
    # cross-exchange backtests don't silently rely on the coarse
    # approximation (audit P3.5).
    if exchange not in _WARNED_FALLBACK_EXCHANGES:
        _WARNED_FALLBACK_EXCHANGES.add(exchange)
        _logger.warning(
            "charges.calculate_charges: exchange=%r has no detailed fee "
            "schedule; using OTHER_EXCHANGE_PER_SIDE_RATE=%.4f (%.3f%% per "
            "side). Add a detailed helper in engine/charges.py if cost "
            "sensitivity matters for this backtest.",
            exchange,
            OTHER_EXCHANGE_PER_SIDE_RATE,
            OTHER_EXCHANGE_PER_SIDE_RATE * 100,
        )
    return round(order_value * OTHER_EXCHANGE_PER_SIDE_RATE, 2)


def calculate_round_trip(exchange, order_value, segment="EQUITY",
                         trade_type="DELIVERY"):
    """Round-trip (buy + sell) charges. Use when a single call is more
    readable than explicitly summing two legs."""
    buy = calculate_charges(exchange, order_value, segment, trade_type, "BUY_SIDE")
    sell = calculate_charges(exchange, order_value, segment, trade_type, "SELL_SIDE")
    return round(buy + sell, 2)


# ── Per-exchange internals ───────────────────────────────────────────────

def _us_per_side(order_value, which_side):
    """US equities: zero-commission, regulatory fees sell-side only."""
    if which_side != "SELL_SIDE":
        return 0.0
    sec_fee = order_value * US_SEC_FEE_RATE
    shares = order_value / US_TAF_ESTIMATED_SHARE_PRICE
    taf = min(shares * US_TAF_PER_SHARE, US_TAF_CAP)
    return round(sec_fee + taf, 2)


def _india_per_side(order_value, segment, trade_type, which_side):
    """India (NSE/BSE): full regulatory + broker breakdown, per leg."""
    brokerage = min(order_value * NSE_BROKERAGE_RATE, NSE_BROKERAGE_CAP)

    stt = 0.0
    if segment == "EQUITY":
        if trade_type == "DELIVERY":
            stt = order_value * NSE_STT_DELIVERY  # both sides
        elif which_side == "SELL_SIDE":
            stt = order_value * NSE_STT_INTRADAY_SELL

    exchange_charges = order_value * NSE_EXCHANGE_RATE
    sebi_charges = order_value * NSE_SEBI_RATE
    gst = (brokerage + exchange_charges) * NSE_GST_RATE

    # Stamp duty: buy-side only, rate depends on trade_type.
    stamp_duty = 0.0
    if which_side == "BUY_SIDE":
        if trade_type == "INTRADAY":
            stamp_duty = order_value * NSE_STAMP_DUTY_INTRADAY
        else:
            stamp_duty = order_value * NSE_STAMP_DUTY_DELIVERY_BUY

    total = brokerage + stt + exchange_charges + sebi_charges + gst + stamp_duty
    return round(total, 2)


def _lse_per_side(order_value, which_side):
    """LSE (UK): 0.5% SDRT stamp on BUY only + flat broker rate."""
    broker = order_value * LSE_BROKER_RATE
    stamp = order_value * LSE_STAMP_DUTY_BUY if which_side == "BUY_SIDE" else 0.0
    return round(broker + stamp, 2)


def _hkse_per_side(order_value, which_side):
    """HKSE: stamp duty + SFC + AFRC + trading fee + CCASS + broker, both sides."""
    stamp = order_value * HKSE_STAMP_DUTY
    sfc = order_value * HKSE_SFC_LEVY
    afrc = order_value * HKSE_AFRC_LEVY
    trading = order_value * HKSE_TRADING_FEE
    ccass = order_value * HKSE_CCASS_RATE
    broker = order_value * HKSE_BROKER_RATE
    # `which_side` unused: all HKSE regulatory fees are symmetric.
    _ = which_side
    return round(stamp + sfc + afrc + trading + ccass + broker, 2)


def _xetra_per_side(order_value, which_side):
    """XETRA: exchange/clearing + broker. No stamp or transaction tax."""
    _ = which_side  # symmetric
    return round(order_value * (XETRA_EXCHANGE_FEE + XETRA_BROKER_RATE), 2)


def _jpx_per_side(order_value, which_side):
    """JPX: exchange fee + broker. No stamp or transaction tax since 1999."""
    _ = which_side  # symmetric
    return round(order_value * (JPX_EXCHANGE_FEE + JPX_BROKER_RATE), 2)


def _ksc_per_side(order_value, which_side):
    """KSC (Korea): sec tax (historical max 0.25%) + agricultural tax (0.15%,
    KOSPI) applied on SELL only. Broker both sides."""
    broker = order_value * KSC_BROKER_RATE
    if which_side == "SELL_SIDE":
        sec_tax = order_value * KSC_SEC_TAX_SELL
        agri = order_value * KSC_AGRICULTURAL_TAX_SELL
        return round(broker + sec_tax + agri, 2)
    return round(broker, 2)


def _tsx_per_side(order_value, which_side):
    """TSX (Canada): minor regulatory + broker, symmetric."""
    _ = which_side  # symmetric
    return round(order_value * (TSX_EXCHANGE_FEE + TSX_BROKER_RATE), 2)


def _asx_per_side(order_value, which_side):
    """ASX (Australia): ASIC listed-equity fee + broker, symmetric."""
    _ = which_side  # symmetric
    return round(order_value * (ASX_EXCHANGE_FEE + ASX_BROKER_RATE), 2)


# ── Round-trip helpers for standalone strategies ─────────────────────────

def nse_intraday_charges(order_value: float) -> float:
    """Round-trip intraday brokerage for NSE equity (Zerodha MIS).

    Note: This helper models Zerodha's intraday structure directly (stamp duty
    on buy side only, STT sell side only). calculate_round_trip(..., INTRADAY)
    produces a slightly different number because per_side helpers apply stamp
    duty proportionally and do not model the broker's specific conventions.
    """
    brokerage_per_leg = min(order_value * NSE_BROKERAGE_RATE, NSE_BROKERAGE_CAP)
    brokerage = brokerage_per_leg * 2
    stt = order_value * NSE_STT_INTRADAY_SELL
    exchange = order_value * NSE_EXCHANGE_RATE * 2
    sebi = order_value * NSE_SEBI_RATE * 2
    stamp = order_value * NSE_STAMP_DUTY_INTRADAY
    gst = (brokerage + exchange) * NSE_GST_RATE
    return round(brokerage + stt + exchange + sebi + stamp + gst, 2)


def us_intraday_charges(order_value: float,
                        avg_share_price: float = US_TAF_ESTIMATED_SHARE_PRICE) -> float:
    """Round-trip intraday charges for US equities (zero-commission).

    Rounded to 2dp to match the rest of the charges module. Pre-fix this
    rounded to 4dp, which was inconsistent with `_us_per_side`.
    """
    if order_value <= 0:
        return 0.0
    shares = order_value / avg_share_price if avg_share_price > 0 else 0
    sec_fee = order_value * US_SEC_FEE_RATE
    taf = min(shares * US_TAF_PER_SHARE, US_TAF_CAP)
    return round(sec_fee + taf, 2)


def nse_delivery_charges(order_value: float) -> float:
    """Round-trip delivery brokerage for NSE equity (Zerodha CNC). Zero brokerage."""
    stt = order_value * NSE_STT_DELIVERY * 2
    exchange = order_value * NSE_EXCHANGE_RATE * 2
    sebi = order_value * NSE_SEBI_RATE * 2
    stamp = order_value * NSE_STAMP_DUTY_DELIVERY_BUY  # buy side only
    gst = exchange * NSE_GST_RATE
    return round(stt + exchange + sebi + stamp + gst, 2)

"""Broker charge calculations.

All functions named `calculate_*` return PER-SIDE charges. Buy and sell legs
are computed separately; round-trip cost is the sum of the two. Functions
named `*_round_trip_*` return the two-leg total.

Historical bug (fixed 2026-04-21): the non-IN/US fallback returned
`order_value * 0.001` with a comment claiming "0.1% round-trip estimate",
but the caller (simulator.py) invokes it per-side, producing 0.2% round-trip
on LSE, JPX, HKSE, XETRA, KSC, ASX, TSX, SAO, SES, SHH, SHZ, TAI, JNB.
See docs/AUDIT_FINDINGS.md.

Fee data is organized as module-level constants so adding a new exchange is
editing data, not control flow.
"""

from engine.constants import BROKER_KITE, EXCHANGE_BROKER_MAP

# ── Named constants (fee schedules as data) ──────────────────────────────

# India (NSE/BSE, delivery and intraday)
NSE_BROKERAGE_RATE = 0.0003        # 0.03%, capped per-leg
NSE_BROKERAGE_CAP = 20.0           # Rs 20 per leg
NSE_STT_DELIVERY = 0.001           # 0.1% on BOTH buy and sell
NSE_STT_INTRADAY_SELL = 0.00025    # 0.025% on sell side only
NSE_EXCHANGE_RATE = 0.0000345      # 0.00345% per leg
NSE_SEBI_RATE = 0.000001           # Rs 10 per crore
NSE_GST_RATE = 0.18                # on (brokerage + exchange)
NSE_STAMP_DUTY_DELIVERY_BUY = 0.00015  # 0.015% buy-side only
NSE_STAMP_DUTY_INTRADAY = 0.00003      # 0.003% buy-side only (round-trip basis in helper)

# US equities (zero-commission, regulatory fees sell-side only)
US_SEC_FEE_RATE = 0.0000278          # ~$27.80 per million, sell side
US_TAF_PER_SHARE = 0.000166          # FINRA TAF, sell side
US_TAF_CAP = 8.30                    # per-transaction cap
US_TAF_ESTIMATED_SHARE_PRICE = 50.0  # used when share count unknown

# Other exchanges (LSE, JPX, HKSE, XETRA, KSC, ASX, TSX, SAO, SES, SHH, SHZ, TAI, JNB)
# Flat per-side percentage as a coarse estimate until exchange-specific
# schedules are added. 0.05% per leg = 0.10% round-trip.
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
        exchange: Exchange code (NSE, BSE, US/NASDAQ/NYSE/AMEX, or other).
        order_value: Notional for this leg.
        segment: "EQUITY" (other segments not yet modeled).
        trade_type: "DELIVERY" or "INTRADAY".
        which_side: "BUY_SIDE" or "SELL_SIDE".

    Returns:
        float: Total charges for THIS LEG ONLY. Round-trip cost is
        calculate_charges(buy) + calculate_charges(sell).

    For exchanges without a detailed fee model, returns
    `order_value * OTHER_EXCHANGE_PER_SIDE_RATE` (0.05% per leg).
    """
    if exchange in US_EXCHANGES:
        return _us_per_side(order_value, which_side)
    if exchange in INDIA_EXCHANGES:
        return _india_per_side(order_value, segment, trade_type, which_side)
    # Other exchanges: per-side flat rate
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
    # Pre-fix, intraday used the delivery rate (5x too high). nse_intraday_charges
    # already used the correct rate in its round-trip form; this path now agrees.
    stamp_duty = 0.0
    if which_side == "BUY_SIDE":
        if trade_type == "INTRADAY":
            stamp_duty = order_value * NSE_STAMP_DUTY_INTRADAY
        else:
            stamp_duty = order_value * NSE_STAMP_DUTY_DELIVERY_BUY

    total = brokerage + stt + exchange_charges + sebi_charges + gst + stamp_duty
    return round(total, 2)


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

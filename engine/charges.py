"""Broker charge calculations for NSE/BSE equity trades.

Combines ATO_Simulator's calculate_charges() with nse_arena's intraday/delivery helpers.
"""

from engine.constants import BROKER_KITE, EXCHANGE_BROKER_MAP


def get_primary_broker(exchange: str) -> str:
    return EXCHANGE_BROKER_MAP.get(exchange.upper(), BROKER_KITE)


def calculate_charges(exchange, order_value, segment="EQUITY", trade_type="DELIVERY", which_side="BUY_SIDE"):
    """Calculate broker charges for a single-leg trade.

    Args:
        exchange: Exchange name (NSE, BSE, US, etc.)
        order_value: Total value of the order
        segment: Trading segment (EQUITY, FO, etc.)
        trade_type: Type of trade (DELIVERY, INTRADAY)
        which_side: BUY_SIDE or SELL_SIDE

    Returns:
        float: Total charges for the trade
    """
    # US equities: zero-commission model (SEC fee + FINRA TAF on sell side only)
    if exchange in ("US", "NASDAQ", "NYSE", "AMEX"):
        if which_side == "SELL_SIDE":
            sec_fee = order_value * 0.0000278   # ~$27.80 per million
            shares = order_value / 50.0         # estimate
            taf = min(shares * 0.000166, 8.30)  # FINRA TAF, capped
            return round(sec_fee + taf, 2)
        return 0.0

    # Non-Indian, non-US exchanges: flat percentage estimate (no country-specific taxes)
    if exchange not in ("NSE", "BSE"):
        return round(order_value * 0.001, 2)  # 0.1% round-trip estimate

    # Indian exchanges: full charge structure
    brokerage_rate = 0.0003  # 0.03% or Rs 20, whichever is lower
    brokerage = min(order_value * brokerage_rate, 20)

    stt = 0
    if segment == "EQUITY":
        if trade_type == "DELIVERY":
            stt = order_value * 0.001  # 0.1% on both buy and sell
        else:
            if which_side == "SELL_SIDE":
                stt = order_value * 0.00025  # 0.025% sell side only

    exchange_charges = order_value * 0.0000345  # 0.00345%

    sebi_charges = order_value * 0.000001  # Rs 10 per crore

    gst = (brokerage + exchange_charges) * 0.18

    stamp_duty = 0
    if which_side == "BUY_SIDE":
        stamp_duty = order_value * 0.00015  # 0.015% on buy side

    total_charges = brokerage + stt + exchange_charges + sebi_charges + gst + stamp_duty
    return round(total_charges, 2)


def nse_intraday_charges(order_value: float) -> float:
    """Round-trip intraday brokerage for NSE equity (Zerodha MIS)."""
    brokerage_per_leg = min(order_value * 0.0003, 20.0)
    brokerage = brokerage_per_leg * 2
    stt = order_value * 0.00025
    exchange = order_value * 0.0000345 * 2
    sebi = order_value * 0.000001 * 2
    stamp = order_value * 0.00003
    gst = (brokerage + exchange) * 0.18
    return round(brokerage + stt + exchange + sebi + stamp + gst, 2)


def us_intraday_charges(order_value: float, avg_share_price: float = 50.0) -> float:
    """Round-trip intraday charges for US equities (zero-commission model).

    Covers SEC fee (sell side) and FINRA TAF (sell side). No brokerage.
    """
    if order_value <= 0:
        return 0.0
    shares = order_value / avg_share_price if avg_share_price > 0 else 0
    sec_fee = order_value * 0.0000278        # ~$27.80 per million, sell side
    taf = min(shares * 0.000166, 8.30)       # FINRA TAF, sell side, capped at $8.30
    return round(sec_fee + taf, 4)


def nse_delivery_charges(order_value: float) -> float:
    """Round-trip delivery brokerage for NSE equity (Zerodha CNC). Zero brokerage."""
    stt = order_value * 0.001 * 2
    exchange = order_value * 0.0000345 * 2
    sebi = order_value * 0.000001 * 2
    stamp = order_value * 0.00015
    gst = exchange * 0.18
    return round(stt + exchange + sebi + stamp + gst, 2)

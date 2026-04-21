"""Tests for engine/charges.py."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.charges import (
    calculate_charges, nse_intraday_charges, nse_delivery_charges,
    get_primary_broker, us_intraday_charges,
)


# --- Existing tests ---

def test_calculate_charges_nse_delivery_buy():
    charges = calculate_charges("NSE", 50000, "EQUITY", "DELIVERY", "BUY_SIDE")
    assert charges > 0
    assert 60 < charges < 100, f"Expected ~77, got {charges}"


def test_calculate_charges_nse_delivery_sell():
    charges = calculate_charges("NSE", 50000, "EQUITY", "DELIVERY", "SELL_SIDE")
    assert charges > 0
    assert charges < calculate_charges("NSE", 50000, "EQUITY", "DELIVERY", "BUY_SIDE")


def test_calculate_charges_intraday_sell():
    charges = calculate_charges("NSE", 100000, "EQUITY", "INTRADAY", "SELL_SIDE")
    assert charges > 0
    assert charges > calculate_charges("NSE", 100000, "EQUITY", "INTRADAY", "BUY_SIDE")


def test_nse_intraday_charges():
    charges = nse_intraday_charges(100000)
    assert charges > 0
    assert isinstance(charges, float)


def test_nse_delivery_charges():
    charges = nse_delivery_charges(100000)
    assert charges > 0
    assert isinstance(charges, float)
    assert nse_delivery_charges(100000) > nse_intraday_charges(100000)


def test_get_primary_broker():
    assert get_primary_broker("NSE") == "kite"
    assert get_primary_broker("BSE") == "kite"
    assert get_primary_broker("UNKNOWN") == "kite"


# --- New NSE tests ---

def test_nse_intraday_exact_breakdown():
    """Verify each component for order_value=100K."""
    ov = 100_000
    brokerage_per_leg = min(ov * 0.0003, 20.0)  # min(30, 20) = 20
    brokerage = brokerage_per_leg * 2            # 40
    stt = ov * 0.00025                           # 25
    exchange = ov * 0.0000345 * 2                # 6.9
    sebi = ov * 0.000001 * 2                     # 0.2
    stamp = ov * 0.00003                         # 3.0
    gst = (brokerage + exchange) * 0.18          # (40 + 6.9) * 0.18 = 8.442

    expected = round(brokerage + stt + exchange + sebi + stamp + gst, 2)
    actual = nse_intraday_charges(ov)

    assert actual == expected, f"Expected {expected}, got {actual}"
    # Verify individual components are reasonable
    assert brokerage == 40.0
    assert stt == 25.0
    assert abs(exchange - 6.9) < 0.01
    assert abs(gst - 8.442) < 0.01


def test_brokerage_cap_at_20_per_leg():
    """Cap triggers at order_value where 0.03% > Rs 20 (i.e. ov > 66667)."""
    # At 100K: 0.03% = 30, capped at 20
    ov_above = 100_000
    brokerage_per_leg = min(ov_above * 0.0003, 20.0)
    assert brokerage_per_leg == 20.0

    # At 50K: 0.03% = 15, no cap
    ov_below = 50_000
    brokerage_per_leg = min(ov_below * 0.0003, 20.0)
    assert abs(brokerage_per_leg - 15.0) < 1e-10

    # Charges at 100K should be less than naive linear extrapolation from 50K
    # because brokerage is capped
    c50 = nse_intraday_charges(50_000)
    c100 = nse_intraday_charges(100_000)
    assert c100 < c50 * 2  # cap reduces the scaling


def test_brokerage_below_cap():
    """No cap for small orders (0.03% < Rs 20)."""
    ov = 10_000  # 0.03% = 3, well below cap
    brokerage_per_leg = min(ov * 0.0003, 20.0)
    assert brokerage_per_leg == ov * 0.0003  # 3.0, no cap


def test_zero_order_value():
    """Zero order value returns 0."""
    assert nse_intraday_charges(0) == 0.0


def test_round_trip_consistency():
    """buy_charges + sell_charges ≈ nse_intraday_charges() for same OV."""
    ov = 100_000
    buy = calculate_charges("NSE", ov, "EQUITY", "INTRADAY", "BUY_SIDE")
    sell = calculate_charges("NSE", ov, "EQUITY", "INTRADAY", "SELL_SIDE")
    round_trip_via_single_leg = buy + sell
    round_trip = nse_intraday_charges(ov)

    # They won't be exactly equal (different GST base, stamp logic) but within 5%
    ratio = round_trip / round_trip_via_single_leg if round_trip_via_single_leg > 0 else 0
    assert 0.85 < ratio < 1.15, (
        f"round_trip={round_trip}, single_leg_sum={round_trip_via_single_leg}, ratio={ratio}"
    )


# --- US charges tests ---

def test_us_intraday_charges_basic():
    """Returns small positive value for $100K order."""
    charges = us_intraday_charges(100_000)
    assert charges > 0
    assert isinstance(charges, float)
    # ~$2.78 SEC + ~$0.332 TAF = ~$3.11
    assert charges < 10, f"Expected ~$3, got {charges}"


def test_us_much_cheaper_than_nse():
    """US charges < 5% of NSE charges for same order value."""
    ov = 100_000
    us = us_intraday_charges(ov)
    nse = nse_intraday_charges(ov)
    assert us < nse * 0.05, f"US={us}, NSE={nse}, ratio={us/nse:.4f}"


def test_us_zero_order_value():
    """Returns 0.0 for zero order."""
    assert us_intraday_charges(0) == 0.0


def test_us_taf_cap():
    """FINRA TAF is capped at $8.30."""
    # Very large order: 10M at $50/share = 200K shares, TAF = 200K * 0.000166 = $33.2 -> capped at $8.30
    charges = us_intraday_charges(10_000_000, avg_share_price=50.0)
    shares = 10_000_000 / 50.0
    uncapped_taf = shares * 0.000166
    assert uncapped_taf > 8.30  # confirm cap would apply
    # Total should include SEC + capped TAF; 2dp precision matches module convention
    expected_sec = 10_000_000 * 0.0000278
    expected = round(expected_sec + 8.30, 2)
    assert charges == expected, f"Expected {expected}, got {charges}"


# --- P0 regression: non-IN/US exchanges must be per-side, not round-trip ---

def test_non_in_us_exchange_is_per_side():
    """Audit P0 #11: Pre-fix, non-IN/US exchanges returned order_value * 0.001
    with a comment claiming '0.1% round-trip estimate', but simulator.py calls
    calculate_charges() once per leg, so actual round-trip was 0.2%. Fix:
    per-leg rate is OTHER_EXCHANGE_PER_SIDE_RATE = 0.0005 (0.05% per leg =
    0.1% round-trip, matching the original stated intent).
    """
    from engine.charges import OTHER_EXCHANGE_PER_SIDE_RATE, calculate_round_trip
    # 0.05% per leg, not 0.1%
    assert OTHER_EXCHANGE_PER_SIDE_RATE == 0.0005
    # LSE buy leg only
    buy = calculate_charges("LSE", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("LSE", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    # Each leg: 0.05% of 100k = 50
    assert buy == 50.0, f"LSE buy leg should be 50, got {buy}"
    assert sell == 50.0, f"LSE sell leg should be 50, got {sell}"
    # Round-trip: 100 total (0.1% of notional)
    rt = calculate_round_trip("LSE", 100_000, "EQUITY", "DELIVERY")
    assert rt == 100.0, f"LSE round-trip should be 100 (0.1%), got {rt}"


def test_every_exchange_has_per_side_semantics():
    """For any exchange, round-trip must equal buy + sell. This is the
    invariant that went wrong before the fix."""
    from engine.charges import calculate_round_trip
    for exchange in ("NSE", "LSE", "HKSE", "JPX", "NASDAQ"):
        for trade_type in ("DELIVERY", "INTRADAY"):
            buy = calculate_charges(exchange, 100_000, "EQUITY", trade_type, "BUY_SIDE")
            sell = calculate_charges(exchange, 100_000, "EQUITY", trade_type, "SELL_SIDE")
            rt = calculate_round_trip(exchange, 100_000, "EQUITY", trade_type)
            assert abs(rt - (buy + sell)) < 0.01, (
                f"{exchange}/{trade_type}: round_trip={rt}, buy+sell={buy+sell}"
            )


def test_nse_intraday_per_side_matches_helper_round_trip():
    """P1 regression: pre-fix, `_india_per_side` used the delivery stamp
    duty rate (0.015%) for intraday too. Post-fix, intraday uses 0.003%,
    matching the existing `nse_intraday_charges` helper that models Zerodha
    MIS. This test locks in agreement: calculate_round_trip(..., INTRADAY)
    must equal nse_intraday_charges up to the rounding difference per leg.
    """
    from engine.charges import calculate_round_trip
    for ov in (10_000, 50_000, 100_000, 500_000):
        per_side_rt = calculate_round_trip("NSE", ov, "EQUITY", "INTRADAY")
        helper_rt = nse_intraday_charges(ov)
        # Within 2 rupees to absorb per-leg rounding; was >>50 rupees pre-fix
        assert abs(per_side_rt - helper_rt) < 2.0, (
            f"ov={ov}: per_side_rt={per_side_rt}, helper_rt={helper_rt}, "
            f"diff={per_side_rt - helper_rt} (pre-fix this was ~0.012% of ov)"
        )


def test_nse_sell_side_stt_present():
    """NSE delivery: STT is on BOTH sides. Make the math explicit."""
    sell = calculate_charges("NSE", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    # STT 0.1% = 100, plus brokerage 20, exchange 3.45, sebi 0.1, gst ~4.22
    # No stamp duty sell side.
    assert 120 < sell < 135, f"Expected NSE delivery sell ~127, got {sell}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

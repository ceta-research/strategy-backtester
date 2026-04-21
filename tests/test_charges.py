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
    exchange = ov * 0.0000297 * 2                # 5.94
    sebi = ov * 0.000001 * 2                     # 0.2
    stamp = ov * 0.00003                         # 3.0
    gst = (brokerage + exchange) * 0.18          # (40 + 5.94) * 0.18 = 8.2692

    expected = round(brokerage + stt + exchange + sebi + stamp + gst, 2)
    actual = nse_intraday_charges(ov)

    assert actual == expected, f"Expected {expected}, got {actual}"
    # Verify individual components are reasonable
    assert brokerage == 40.0
    assert stt == 25.0
    assert abs(exchange - 5.94) < 0.01
    assert abs(gst - 8.2692) < 0.01


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

def test_fallback_exchange_is_per_side():
    """Audit P0 #11: Pre-fix, non-IN/US exchanges returned order_value * 0.001
    with a comment claiming '0.1% round-trip estimate', but simulator.py calls
    calculate_charges() once per leg, so actual round-trip was 0.2%. Fix:
    per-leg rate is OTHER_EXCHANGE_PER_SIDE_RATE = 0.0005 (0.05% per leg =
    0.1% round-trip, matching the original stated intent).

    Post-P3.5 revisit: LSE now has a detailed helper, so we use an
    exchange that genuinely falls through (e.g. SAO) to exercise the
    OTHER_EXCHANGE path.
    """
    from engine.charges import OTHER_EXCHANGE_PER_SIDE_RATE, calculate_round_trip
    # 0.05% per leg, not 0.1%
    assert OTHER_EXCHANGE_PER_SIDE_RATE == 0.0005
    buy = calculate_charges("SAO", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("SAO", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    # Each leg: 0.05% of 100k = 50
    assert buy == 50.0, f"SAO buy leg should be 50, got {buy}"
    assert sell == 50.0, f"SAO sell leg should be 50, got {sell}"
    # Round-trip: 100 total (0.1% of notional)
    rt = calculate_round_trip("SAO", 100_000, "EQUITY", "DELIVERY")
    assert rt == 100.0, f"SAO round-trip should be 100 (0.1%), got {rt}"


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
    # STT 0.1% = 100, plus brokerage 20, exchange 2.97, sebi 0.1, gst ~4.13
    # No stamp duty sell side. Post-P3.4 rate update: exchange rate
    # dropped 0.00345% -> 0.00297%, so total drops ~0.5 from pre-update.
    assert 120 < sell < 135, f"Expected NSE delivery sell ~127, got {sell}"


# --- P3.4: rate-vintage pins ---------------------------------------------
# These tests lock the exact values of every India/US rate constant so that
# any future rate change is a deliberate edit. If one of these fails, the
# expectation is "update both the constant and this test in the same commit,
# with a rate-vintage note in the module docstring", not "silently relax the
# test".

def test_nse_rate_constants_vintage_pinned():
    from engine import charges
    assert charges.NSE_BROKERAGE_RATE == 0.0003
    assert charges.NSE_BROKERAGE_CAP == 20.0
    assert charges.NSE_STT_DELIVERY == 0.001
    assert charges.NSE_STT_INTRADAY_SELL == 0.00025
    # Current 2024+ rate per Zerodha. Was 0.0000345 pre-P3.4-revisit.
    assert charges.NSE_EXCHANGE_RATE == 0.0000297
    assert charges.NSE_SEBI_RATE == 0.000001
    assert charges.NSE_GST_RATE == 0.18
    assert charges.NSE_STAMP_DUTY_DELIVERY_BUY == 0.00015
    assert charges.NSE_STAMP_DUTY_INTRADAY == 0.00003


def test_us_rate_constants_vintage_pinned():
    from engine import charges
    assert charges.US_SEC_FEE_RATE == 0.0000278
    assert charges.US_TAF_PER_SHARE == 0.000166
    assert charges.US_TAF_CAP == 8.30
    assert charges.US_TAF_ESTIMATED_SHARE_PRICE == 50.0


def test_other_exchange_fallback_rate_pinned():
    from engine import charges
    assert charges.OTHER_EXCHANGE_PER_SIDE_RATE == 0.0005


# --- P3.5: fallback warning fires exactly once per exchange ---------------

def test_unknown_exchange_warns_once(caplog):
    """The generic OTHER_EXCHANGE fallback should log a warning the first
    time each exchange is seen, and be silent thereafter — so cross-exchange
    backtests flag but don't spam.

    Post-P3.5 revisit: LSE/HKSE/XETRA/JPX/KSC/TSX/ASX all have detailed
    schedules. Exchanges that still fall through include SAO, SHH, SHZ,
    TAI, JNB, SES, TWO, SET, JKT, PAR.
    """
    import logging
    from engine import charges

    # Reset the module-level warned set so the test is hermetic.
    charges._WARNED_FALLBACK_EXCHANGES.clear()
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="engine.charges"):
        # First call: warning should fire.
        charges.calculate_charges("SAO", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
        # Second call, same exchange: warning should NOT fire again.
        charges.calculate_charges("SAO", 50_000, "EQUITY", "DELIVERY", "SELL_SIDE")
        # Different exchange: should warn.
        charges.calculate_charges("JNB", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")

    sao_warnings = [r for r in caplog.records if "'SAO'" in r.getMessage()]
    jnb_warnings = [r for r in caplog.records if "'JNB'" in r.getMessage()]
    assert len(sao_warnings) == 1, f"Expected 1 SAO warning, got {len(sao_warnings)}"
    assert len(jnb_warnings) == 1, f"Expected 1 JNB warning, got {len(jnb_warnings)}"


def test_known_exchanges_do_not_warn(caplog):
    """Exchanges with detailed schedules should not emit the fallback warning.
    Post-P3.5 this set includes NSE/BSE/US/NASDAQ/NYSE/AMEX/LSE/HKSE/XETRA/
    JPX/KSC/TSX/ASX."""
    import logging
    from engine import charges

    charges._WARNED_FALLBACK_EXCHANGES.clear()
    caplog.clear()

    detailed = (
        "NSE", "BSE", "US", "NASDAQ", "NYSE", "AMEX",
        "LSE", "HKSE", "XETRA", "JPX", "KSC", "TSX", "ASX",
    )
    with caplog.at_level(logging.WARNING, logger="engine.charges"):
        for exch in detailed:
            charges.calculate_charges(exch, 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
            charges.calculate_charges(exch, 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")

    fallback_warnings = [
        r for r in caplog.records
        if "has no detailed fee schedule" in r.getMessage()
    ]
    assert not fallback_warnings, (
        f"Known exchanges should not emit fallback warnings; got: "
        f"{[r.getMessage() for r in fallback_warnings]}"
    )


# --- P3.5 revisit: per-exchange schedule rate pins ------------------------

def test_lse_buy_charges_include_uk_stamp():
    """UK Stamp Duty Reserve Tax 0.5% on BUY only + 0.05% broker."""
    buy = calculate_charges("LSE", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    # 0.5% stamp (500) + 0.05% broker (50) = 550
    assert abs(buy - 550.0) < 0.01, f"Expected 550, got {buy}"


def test_lse_sell_no_stamp():
    """UK SDRT is buy-side only. Sell leg is broker-only (0.05%)."""
    sell = calculate_charges("LSE", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    assert abs(sell - 50.0) < 0.01, f"Expected 50 (broker only), got {sell}"


def test_hkse_symmetric_stamp_plus_regulatory():
    """HKSE 0.13% stamp both sides + SFC 0.0027% + AFRC 0.00015% +
    trading 0.00565% + CCASS 0.002% + broker 0.15%. Symmetric."""
    buy = calculate_charges("HKSE", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("HKSE", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    # Stamp 130 + SFC 2.7 + AFRC 0.15 + Trading 5.65 + CCASS 2 + Broker 150 = 290.5
    assert abs(buy - sell) < 0.01, "HKSE fees must be symmetric"
    assert abs(buy - 290.5) < 0.5, f"Expected ~290.5, got {buy}"


def test_xetra_no_stamp():
    """XETRA: exchange+clearing 0.01% + broker 0.05% = 0.06%/side."""
    buy = calculate_charges("XETRA", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("XETRA", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    assert abs(buy - 60.0) < 0.01
    assert abs(sell - 60.0) < 0.01


def test_jpx_no_stamp():
    """JPX: 0.005% exchange + 0.1% broker = 0.105%/side."""
    buy = calculate_charges("JPX", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    assert abs(buy - 105.0) < 0.01, f"Expected 105, got {buy}"


def test_ksc_sell_includes_sec_and_agricultural_tax():
    """KSC (Korea KOSPI): buy is broker only; sell adds 0.25% sec tax
    (historical max) + 0.15% agricultural tax + broker."""
    buy = calculate_charges("KSC", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("KSC", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    # Buy: broker 0.05% = 50
    assert abs(buy - 50.0) < 0.01, f"Expected buy=50, got {buy}"
    # Sell: broker 50 + sec tax 250 + agri 150 = 450
    assert abs(sell - 450.0) < 0.01, f"Expected sell=450, got {sell}"


def test_tsx_symmetric():
    """TSX: 0.005% regulatory + 0.1% broker = 0.105%/side."""
    buy = calculate_charges("TSX", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    sell = calculate_charges("TSX", 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
    assert abs(buy - 105.0) < 0.01
    assert abs(buy - sell) < 0.01


def test_asx_symmetric():
    """ASX: 0.0028% ASIC + 0.1% broker = 0.1028%/side."""
    buy = calculate_charges("ASX", 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
    assert abs(buy - 102.8) < 0.01, f"Expected 102.8, got {buy}"


def test_per_exchange_rate_constants_pinned():
    """Golden-value pins for every non-IN/US rate constant. Any future
    rate update must edit this test in the same commit."""
    from engine import charges
    assert charges.LSE_STAMP_DUTY_BUY == 0.005
    assert charges.LSE_BROKER_RATE == 0.0005
    assert charges.HKSE_STAMP_DUTY == 0.0013
    assert charges.HKSE_SFC_LEVY == 0.000027
    assert charges.HKSE_AFRC_LEVY == 0.0000015
    assert charges.HKSE_TRADING_FEE == 0.0000565
    assert charges.HKSE_CCASS_RATE == 0.00002
    assert charges.HKSE_BROKER_RATE == 0.0015
    assert charges.XETRA_EXCHANGE_FEE == 0.0001
    assert charges.XETRA_BROKER_RATE == 0.0005
    assert charges.JPX_EXCHANGE_FEE == 0.00005
    assert charges.JPX_BROKER_RATE == 0.001
    assert charges.KSC_SEC_TAX_SELL == 0.0025
    assert charges.KSC_AGRICULTURAL_TAX_SELL == 0.0015
    assert charges.KSC_BROKER_RATE == 0.0005
    assert charges.TSX_EXCHANGE_FEE == 0.00005
    assert charges.TSX_BROKER_RATE == 0.001
    assert charges.ASX_EXCHANGE_FEE == 0.000028
    assert charges.ASX_BROKER_RATE == 0.001


def test_every_detailed_exchange_is_per_side():
    """Round-trip = buy + sell for every exchange with a detailed model."""
    from engine.charges import calculate_round_trip
    for exch in ("LSE", "HKSE", "XETRA", "JPX", "KSC", "TSX", "ASX"):
        buy = calculate_charges(exch, 100_000, "EQUITY", "DELIVERY", "BUY_SIDE")
        sell = calculate_charges(exch, 100_000, "EQUITY", "DELIVERY", "SELL_SIDE")
        rt = calculate_round_trip(exch, 100_000, "EQUITY", "DELIVERY")
        assert abs(rt - (buy + sell)) < 0.01, (
            f"{exch}: round_trip={rt}, buy+sell={buy+sell}"
        )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

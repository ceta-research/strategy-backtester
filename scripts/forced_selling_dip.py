#!/usr/bin/env python3
"""Forced-Selling Detection: Idiosyncratic Dip + Volume Spike.

Buys quality stocks that drop idiosyncratically (relative to sector) with
abnormal volume, signaling forced liquidation (index rebalancing, fund
redemptions, tax-loss harvesting) rather than fundamental deterioration.

Signal:
  - Quality stock (2yr consecutive positive returns)
  - Fundamental overlay (ROE>15%, PE<25)
  - Stock drops X% MORE than its sector over N days (idiosyncratic dip)
  - Volume on signal day > Y x 20-day average (abnormal selling pressure)
  - Entry: next-day open (MOC execution)
  - Exit: peak recovery + 10% TSL, or 504d max hold

Supports NSE (native data) and US (FMP data) via --market flag.

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark, fetch_sector_map,
    compute_quality_universe, compute_regime_epochs,
    compute_sector_daily_returns, compute_volume_ratios,
    simulate_portfolio, compute_always_invested,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)

STRATEGY_NAME = "forced_selling_dip"

# Fixed best params from quality dip-buy experiments
CONSECUTIVE_YEARS = 2
PEAK_LOOKBACK = 63
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


# ── Entry Detection ─────────────────────────────────────────────────────────

def compute_forced_selling_entries(
    price_data, quality_universe, sector_map, sector_returns,
    volume_ratios, dip_threshold_pct, volume_mult,
    sector_lookback, peak_lookback=PEAK_LOOKBACK, start_epoch=None,
):
    """Find forced-selling entry signals.

    Signal: quality stock with idiosyncratic dip > threshold AND elevated volume.
    Idiosyncratic dip = stock N-day return minus sector N-day return.

    Args:
        price_data: dict[symbol, list[{epoch, open, close, volume}]]
        quality_universe: dict[epoch, set[symbol]]
        sector_map: dict[symbol, sector]
        sector_returns: dict[epoch, dict[sector, float]] daily sector returns
        volume_ratios: dict[symbol, dict[epoch, float]] volume/avg_volume
        dip_threshold_pct: min idiosyncratic dip % (e.g., 5 = stock dropped 5% more than sector)
        volume_mult: min volume/avg_volume ratio (e.g., 2.0)
        sector_lookback: days over which to measure idiosyncratic return
        peak_lookback: days for rolling peak (exit logic)
        start_epoch: only generate signals after this epoch

    Returns:
        list of entry dicts compatible with simulate_portfolio()
    """
    entries = []
    dip_threshold = dip_threshold_pct / 100.0
    min_bars = max(peak_lookback, sector_lookback) + 1

    for sym, bars in price_data.items():
        if len(bars) < min_bars + 1:
            continue

        sector = sector_map.get(sym, "Unknown")
        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]
        vol_ratios = volume_ratios.get(sym, {})

        for i in range(min_bars, len(bars) - 1):
            epoch = epochs[i]
            if start_epoch and epoch < start_epoch:
                continue

            # Quality check
            universe = quality_universe.get(epoch)
            if universe is None or sym not in universe:
                continue

            # Stock return over sector_lookback days
            lookback_close = closes[i - sector_lookback]
            if lookback_close <= 0:
                continue
            stock_return = closes[i] / lookback_close - 1.0

            # Sector cumulative return over same period (sum of daily returns)
            sector_cum_return = 0.0
            for j in range(i - sector_lookback + 1, i + 1):
                ep = epochs[j]
                day_sectors = sector_returns.get(ep, {})
                sector_cum_return += day_sectors.get(sector, 0.0)

            # Idiosyncratic return (negative = stock dropped more than sector)
            idio = stock_return - sector_cum_return

            if idio >= -dip_threshold:
                continue

            # Volume spike check
            vol_ratio = vol_ratios.get(epoch, 0.0)
            if vol_ratio < volume_mult:
                continue

            # Entry at next day's open (MOC execution)
            entry_price = opens[i + 1]
            if entry_price <= 0:
                continue

            # Rolling peak for exit logic
            window_start = max(0, i - peak_lookback + 1)
            peak_price = max(closes[window_start:i + 1])

            entries.append({
                "epoch": epoch,
                "symbol": sym,
                "peak_price": peak_price,
                "dip_pct": abs(idio),
                "entry_epoch": epochs[i + 1],
                "entry_price": entry_price,
            })

    entries.sort(key=lambda x: x["entry_epoch"])
    print(f"  Forced-selling entries: {len(entries)} signals "
          f"(idio_dip>{dip_threshold_pct}%, vol>{volume_mult}x, "
          f"sector_lb={sector_lookback}d)")
    return entries


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    market = os.environ.get("MARKET", "nse").lower()
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    from scripts.quality_dip_buy_lib import FMP_EXCHANGES

    MARKET_CONFIGS = {
        "nse": {"exchange": "NSE", "start": 1262304000, "benchmark": "NIFTYBEES", "capital": 10_000_000},
        "us":  {"exchange": "US",  "start": 1104537600, "benchmark": "SPY",       "capital": 10_000_000},
    }
    for exch, cfg in FMP_EXCHANGES.items():
        MARKET_CONFIGS[exch.lower()] = {
            "exchange": exch, "start": 1262304000, "benchmark": cfg["benchmark"],
            "capital": 10_000_000,
        }

    if market not in MARKET_CONFIGS:
        print(f"Unknown market: {market}. Supported: {', '.join(MARKET_CONFIGS.keys())}")
        return

    mc = MARKET_CONFIGS[market]
    exchange = mc["exchange"]
    start_epoch = mc["start"]
    end_epoch = 1773878400     # 2026-03-19
    benchmark_sym = mc["benchmark"]
    capital = mc["capital"]
    description = (f"Forced-selling dip on {exchange}: idiosyncratic dip + volume spike "
                   "on quality stocks with fundamental overlay.")

    source = os.environ.get("SOURCE", "native" if exchange == "NSE" else "fmp")
    if "--fmp" in sys.argv:
        source = "fmp"
    elif "--bhavcopy" in sys.argv:
        source = "bhavcopy"

    # Return cap: limits max single-trade return (caps XETRA/LSE adjClose artifacts)
    return_cap = float(os.environ.get("RETURN_CAP", "0"))
    if "--return-cap" in sys.argv:
        return_cap = float(sys.argv[sys.argv.index("--return-cap") + 1])

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME} ({market.upper()}): fetching data (source={source})")
    print("=" * 80)

    print(f"\nFetching {exchange} universe ({source})...")
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch, source=source)
    if not price_data:
        print("No data. Aborting.")
        return

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nFetching sector data...")
    sector_map = fetch_sector_map(cr, exchange)

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    # Pre-compute shared data
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    print("\nComputing sector daily returns...")
    sector_returns = compute_sector_daily_returns(price_data, sector_map)

    print("\nComputing volume ratios...")
    volume_ratios = compute_volume_ratios(price_data, lookback=20)

    # ── Sweep ──
    param_grid = list(product(
        [5, 7, 10],      # dip_threshold_pct (idiosyncratic)
        [1.5, 2.0, 3.0], # volume_multiplier
        [5, 20],          # sector_lookback_days
        [5, 10],          # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({market.upper()} forced-selling)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"regime={REGIME_SMA}, TSL={TSL_PCT}%, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(f"{STRATEGY_NAME}_{market}", "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    for idx, (dip, vol_mult, sec_lb, pos) in enumerate(param_grid):
        params = {
            "dip_threshold_pct": dip,
            "volume_multiplier": vol_mult,
            "sector_lookback": sec_lb,
            "max_positions": pos,
        }

        # Generate forced-selling entries
        entries = compute_forced_selling_entries(
            price_data, quality_universe, sector_map,
            sector_returns, volume_ratios,
            dip, vol_mult, sec_lb,
            peak_lookback=PEAK_LOOKBACK, start_epoch=start_epoch)

        # Filter by fundamentals (ROE>15%, PE<25)
        entries = filter_entries_by_fundamentals(
            entries, fundamentals, ROE_THRESHOLD, 0, PE_THRESHOLD,
            missing_mode="skip")

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital,
            max_positions=pos,
            tsl_pct=TSL_PCT,
            max_hold_days=MAX_HOLD_DAYS,
            exchange=exchange,
            regime_epochs=regime_epochs,
            strategy_name=f"{STRATEGY_NAME}_{market}",
            description=description,
            params=params,
            start_epoch=start_epoch,
            max_single_return=return_cap,
        )

        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [{idx+1}/{total}] dip={dip}% vol={vol_mult}x sec_lb={sec_lb}d "
              f"pos={pos} | CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} T={trades}")

    # ── Always-invested adjustment ──
    print(f"\n{'='*80}")
    print(f"  ALWAYS-INVESTED ADJUSTMENT (idle cash earns {benchmark_sym})")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} dip={params['dip_threshold_pct']}% "
                  f"vol={params['volume_multiplier']}x "
                  f"sec_lb={params['sector_lookback']}d | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

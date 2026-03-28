#!/usr/bin/env python3
"""Quality Dip-Buy on NSE: baseline standalone port.

Buy dips in stocks with consecutive years of positive returns.
Entry: price drops X% from rolling peak in a quality stock, buy at next open.
Exit: peak recovery + trailing stop-loss, or max hold days.

Always-invested adjustment: idle cash earns NIFTYBEES returns.

Validates against pipeline v3 results (14.8% CAGR, Calmar 0.37).

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
    compute_quality_universe, compute_dip_entries, compute_regime_epochs,
    compute_rsi_series, simulate_portfolio, compute_always_invested,
)

CAPITAL = 10_000_000   # 1 crore
STRATEGY_NAME = "quality_dip_buy_nse"
DESCRIPTION = ("Quality dip-buy on NSE: buy dips in stocks with 2+ years of "
               "positive returns. Peak recovery + TSL exit. Always-invested adjustment.")


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

    source = os.environ.get("SOURCE", "native" if exchange == "NSE" else "fmp")
    if "--fmp" in sys.argv:
        source = "fmp"
    elif "--bhavcopy" in sys.argv:
        source = "bhavcopy"

    cr = CetaResearch()

    # ── Fetch data ──
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

    # ── Pre-compute RSI for all symbols ──
    print("\nComputing RSI(14) for all symbols...")
    rsi_data = {}
    for sym, bars in price_data.items():
        closes = [b["close"] for b in bars]
        rsi_data[sym] = compute_rsi_series(closes, period=14)

    # ── Sweep ──
    param_grid = list(product(
        [2],              # consecutive_years
        [0, 0.10],        # min_yearly_return (0% or 10%)
        [5, 7],           # dip_threshold_pct (as integer %)
        [63],             # peak_lookback_days
        [0, 200],         # regime_sma (0=off, 200=on)
        [0, 30],          # rsi_threshold (0=off, 30=on)
        [10],             # tsl_pct
        [504],            # max_hold_days
        [5, 10, 15],      # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  MOC execution | Real NSE charges | 5bps slippage")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (yrs, min_ret, dip, peak, regime_sma, rsi_thresh,
              tsl, hold, pos) in enumerate(param_grid):
        params = {
            "consecutive_years": yrs,
            "min_yearly_return": min_ret,
            "dip_threshold_pct": dip,
            "peak_lookback": peak,
            "regime_sma": regime_sma,
            "rsi_threshold": rsi_thresh,
            "tsl_pct": tsl,
            "max_hold_days": hold,
            "max_positions": pos,
        }

        # Compute quality universe
        quality_universe = compute_quality_universe(
            price_data, yrs, min_ret, rescreen_days=63, start_epoch=start_epoch)

        # Compute dip entries
        entries = compute_dip_entries(
            price_data, quality_universe, peak, dip / 100.0, start_epoch=start_epoch)

        # Compute regime filter
        regime_epochs = compute_regime_epochs(benchmark, regime_sma) if regime_sma > 0 else set()

        # Run simulation
        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital,
            max_positions=pos,
            tsl_pct=tsl,
            max_hold_days=hold,
            exchange=exchange,
            regime_epochs=regime_epochs if regime_sma > 0 else None,
            rsi_data=rsi_data if rsi_thresh > 0 else None,
            rsi_threshold=rsi_thresh,
            sector_map=sector_map,
            max_per_sector=0,
            strategy_name=STRATEGY_NAME,
            description=DESCRIPTION,
            params=params,
            start_epoch=start_epoch,
        )

        sweep.add_config(params, r)
        # Stash day_wise_log for always-invested (keyed by config index)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [{idx+1}/{total}] yrs={yrs} min={min_ret:.0%} dip={dip}% "
              f"peak={peak} regime={regime_sma} rsi={rsi_thresh} pos={pos} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ── Always-invested adjustment for top configs ──
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (idle cash earns NIFTYBEES)")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} | Original: CAGR={s.get('cagr',0)*100:+.1f}% "
                  f"MDD={s.get('max_drawdown',0)*100:.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} | "
                  f"Adjusted: CAGR={adj['cagr_adj']*100:+.1f}% "
                  f"MDD={adj['max_drawdown_adj']*100:.1f}% "
                  f"Cal={adj['calmar_adj']:.2f}")

    # ── Output ──
    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

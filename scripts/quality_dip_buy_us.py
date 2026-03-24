#!/usr/bin/env python3
"""Quality Dip-Buy on US stocks: test if NSE edge exists on US market.

Same strategy as NSE but on NASDAQ/NYSE stocks with mktCap > $1B.
Uses SPY as regime instrument and always-invested benchmark.
Near-zero transaction costs (SEC fee + FINRA TAF only).

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
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_dip_entries, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
)

CAPITAL = 10_000_000  # $10M (comparable scale)
STRATEGY_NAME = "quality_dip_buy_us"
DESCRIPTION = ("Quality dip-buy on US stocks (NASDAQ/NYSE, mktCap > $1B). "
               "SPY regime filter. Near-zero costs.")


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: fetching data")
    print("=" * 80)

    print("\nFetching US universe...")
    price_data = fetch_universe(cr, "US", start_epoch, end_epoch,
                                mktcap_threshold=1_000_000_000)
    if not price_data:
        print("No data. Aborting.")
        return

    print("\nFetching SPY benchmark...")
    benchmark = fetch_benchmark(cr, "SPY", "US", start_epoch, end_epoch,
                                warmup_days=250)

    # ── Sweep ──
    param_grid = list(product(
        [2, 3],           # consecutive_years
        [5, 7, 10],       # dip_threshold_pct
        [63, 126],        # peak_lookback_days
        [0, 200],         # regime_sma
        [10, 0],          # tsl_pct
        [504],            # max_hold_days
        [5, 10],          # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  MOC execution | US charges (near-zero) | 5bps slippage")
    print(f"{'='*80}")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", "US", CAPITAL,
                        slippage_bps=5, description=DESCRIPTION)

    for idx, (yrs, dip, peak, regime_sma, tsl, hold, pos) in enumerate(param_grid):
        params = {
            "consecutive_years": yrs,
            "dip_threshold_pct": dip,
            "peak_lookback": peak,
            "regime_sma": regime_sma,
            "tsl_pct": tsl,
            "max_hold_days": hold,
            "max_positions": pos,
        }

        quality_universe = compute_quality_universe(
            price_data, yrs, 0, rescreen_days=63, start_epoch=start_epoch)

        entries = compute_dip_entries(
            price_data, quality_universe, peak, dip / 100.0, start_epoch=start_epoch)

        regime_epochs = compute_regime_epochs(benchmark, regime_sma) if regime_sma > 0 else set()

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=CAPITAL,
            max_positions=pos,
            tsl_pct=tsl,
            max_hold_days=hold,
            exchange="US",
            regime_epochs=regime_epochs if regime_sma > 0 else None,
            strategy_name=STRATEGY_NAME,
            description=DESCRIPTION,
            params=params,
            start_epoch=start_epoch,
        )

        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  [{idx+1}/{total}] yrs={yrs} dip={dip}% peak={peak} "
              f"regime={regime_sma} tsl={tsl} pos={pos} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # ── Always-invested (idle cash earns SPY) ──
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (idle cash earns SPY)")
    print(f"{'='*80}")

    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, CAPITAL)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} | Original: CAGR={s.get('cagr',0)*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} | "
                  f"Adjusted: CAGR={adj['cagr_adj']*100:+.1f}% "
                  f"Cal={adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

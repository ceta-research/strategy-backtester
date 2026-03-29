#!/usr/bin/env python3
"""Momentum breakout strategy.

Buy stocks making new N-day highs with strong trailing momentum.
Opposite of dip-buy: buys strength instead of weakness.

SQL data shows 63d-high + strong momentum gives:
  - 12.31% avg 63d return (vs 7.61% for dip-buy)
  - 6.83% MEDIAN 63d return (vs 2.57% for dip-buy)

Sweep parameters:
  - breakout_window: [63, 126, 252] (new N-day high)
  - momentum_lookback: [63, 126] days
  - momentum_threshold: [0.20, 0.30, 0.50] (min 126d return)
  - max_positions: [5, 10]
  - tsl_pct: [7, 10, 15]
  - max_hold_days: [126, 252, 504]

Always runs on bhavcopy with 5 bps slippage.
"""

import sys
import os
import time
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
    CetaResearch,
)

STRATEGY_NAME = "momentum_breakout"


def compute_breakout_entries(price_data, breakout_window, momentum_lookback,
                             momentum_threshold, start_epoch=None):
    """Generate breakout entry signals.

    Signal: stock makes a new N-day high AND has trailing momentum above threshold.
    Entry: next-day open (MOC execution).

    Args:
        price_data: dict[symbol, list[{epoch, open, close, volume}]]
        breakout_window: N days to check for new high
        momentum_lookback: days for momentum calculation
        momentum_threshold: minimum momentum return (e.g., 0.30 = +30%)
        start_epoch: only generate signals from this epoch

    Returns:
        list[dict] with entry signals (same format as compute_dip_entries)
    """
    entries = []
    lookback_needed = max(breakout_window, momentum_lookback)

    for symbol, bars in price_data.items():
        if len(bars) < lookback_needed + 2:
            continue

        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]

        for i in range(lookback_needed, len(bars) - 1):
            if start_epoch and epochs[i] < start_epoch:
                continue

            current_close = closes[i]
            if current_close <= 0:
                continue

            # Check if new N-day high
            window_start = max(0, i - breakout_window)
            window_high = max(closes[window_start:i])  # exclude current bar
            if current_close <= window_high:
                continue

            # Check momentum threshold
            if i >= momentum_lookback:
                past_close = closes[i - momentum_lookback]
                if past_close <= 0:
                    continue
                momentum = (current_close - past_close) / past_close
                if momentum < momentum_threshold:
                    continue
            else:
                continue

            # Entry at next day's open (MOC execution)
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            entries.append({
                "epoch": epochs[i],
                "symbol": symbol,
                "peak_price": current_close,  # entry is at the high
                "dip_pct": 0.0,  # no dip, buying strength
                "entry_epoch": epochs[i + 1],
                "entry_price": next_open,
            })

    entries.sort(key=lambda x: x["entry_epoch"])
    print(f"  Breakout entries: {len(entries)} signals "
          f"(window={breakout_window}d, mom>={momentum_threshold*100:.0f}%)")
    return entries


def main():
    exchange = "NSE"
    start_epoch = 1262304000   # 2010-01-01
    end_epoch = 1773878400     # 2026-03-19
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Momentum breakout sweep on BHAVCOPY")
    print("=" * 80)

    print("\nFetching universe (turnover >= 70M INR)...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, 200)

    # Sweep parameters
    breakout_windows = [63, 126, 252]
    momentum_lookbacks = [126]
    momentum_thresholds = [0.20, 0.30, 0.50]
    max_positions_list = [5, 10]
    tsl_pcts = [7, 10, 15]
    max_hold_list = [126, 252, 504]
    regime_options = [True, False]

    param_grid = list(product(
        breakout_windows,
        momentum_thresholds,
        max_positions_list,
        tsl_pcts,
        max_hold_list,
        regime_options,
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  Breakout: {breakout_windows}d, Mom thresh: {momentum_thresholds}")
    print(f"  Positions: {max_positions_list}, TSL: {tsl_pcts}%, Hold: {max_hold_list}d")
    print(f"  Regime: on/off")
    print(f"{'='*80}")

    description = ("Momentum breakout: buy stocks making new N-day highs "
                    "with strong trailing momentum on bhavcopy.")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Cache breakout entries by (window, threshold) to avoid recomputation
    entry_cache = {}

    for idx, (bw, mom_thresh, pos, tsl, hold, use_regime) in enumerate(param_grid):
        cache_key = (bw, mom_thresh)
        if cache_key not in entry_cache:
            entry_cache[cache_key] = compute_breakout_entries(
                price_data, bw, 126, mom_thresh, start_epoch=start_epoch)

        entries = entry_cache[cache_key]

        params = {
            "breakout_window": bw,
            "momentum_threshold": mom_thresh,
            "max_positions": pos,
            "tsl_pct": tsl,
            "max_hold_days": hold,
            "regime_filter": use_regime,
        }

        r, dwl = simulate_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos,
            tsl_pct=tsl, max_hold_days=hold,
            exchange=exchange,
            regime_epochs=regime_epochs if use_regime else None,
            strategy_name=STRATEGY_NAME, description=description,
            params=params, start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        regime_flag = "R" if use_regime else "-"
        print(f"  [{idx+1}/{total}] bw={bw}d mom>{mom_thresh*100:.0f}% pos={pos} "
              f"tsl={tsl}% hold={hold}d {regime_flag} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    # Always-invested adjustment
    print(f"\n{'='*80}")
    print("  ALWAYS-INVESTED ADJUSTMENT (top 10)")
    print(f"{'='*80}")
    sorted_configs = sweep._sorted("calmar_ratio")
    for i, (params, r) in enumerate(sorted_configs[:10]):
        dwl = getattr(r, '_day_wise_log', None)
        if not dwl:
            continue
        adj = compute_always_invested(dwl, benchmark, capital)
        if adj:
            s = r.to_dict()["summary"]
            print(f"  #{i+1} bw={params['breakout_window']}d "
                  f"mom>{params['momentum_threshold']*100:.0f}% "
                  f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
                  f"hold={params['max_hold_days']}d "
                  f"{'R' if params['regime_filter'] else '-'} | "
                  f"CAGR={(s.get('cagr') or 0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={(s.get('calmar_ratio') or 0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    # Top by CAGR
    print(f"\n{'='*80}")
    print("  TOP 10 BY CAGR")
    print(f"{'='*80}")
    sorted_by_cagr = sweep._sorted("cagr")
    for i, (params, r) in enumerate(sorted_by_cagr[:10]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  #{i+1} CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades} | "
              f"bw={params['breakout_window']}d mom>{params['momentum_threshold']*100:.0f}% "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"hold={params['max_hold_days']}d {'R' if params['regime_filter'] else '-'}")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

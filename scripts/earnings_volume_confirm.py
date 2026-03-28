#!/usr/bin/env python3
"""Earnings Beat + Volume Confirmation Strategy (Strategy 2c).

Extension of earnings_surprise_dip (2a) with volume confirmation:
  - Earnings beat day must have HIGH volume (genuine buying interest)
  - Post-earnings dip day must have LOW volume relative to earnings day
    (sellers exhausted, not continued distribution)
  - Volume ratio filter: dip_day_volume < threshold * earnings_day_volume

Thesis: when a stock beats earnings on high volume and then dips on low
volume, the dip is more likely a temporary pullback (profit-taking, sector
rotation) than continued selling. Low dip volume = exhaustion.

Supports NSE and US via --market flag.

Outputs standardized result.json (see docs/BACKTEST_GUIDE.md).
"""

import sys
import os
import bisect
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_regime_epochs,
    simulate_portfolio, compute_always_invested,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)
from scripts.earnings_surprise_dip import fetch_earnings_surprises

STRATEGY_NAME = "earnings_volume_confirm"

# Fixed best params
CONSECUTIVE_YEARS = 2
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


def compute_earnings_volume_entries(
    price_data, earnings, quality_universe,
    surprise_threshold_pct, dip_threshold_pct,
    post_earnings_window, volume_ratio_max,
    start_epoch=None,
):
    """Find entries: earnings beat + dip with volume confirmation.

    Same as compute_post_earnings_dip_entries but adds:
      - Earnings day volume must be > 1.5x 20-day average (high interest)
      - Dip day volume must be < volume_ratio_max * earnings_day_volume (exhaustion)

    Returns list of entry dicts compatible with simulate_portfolio().
    """
    entries = []
    dip_threshold = dip_threshold_pct / 100.0
    vol_lookback = 20

    for sym, events in earnings.items():
        bars = price_data.get(sym)
        if not bars or len(bars) < 30:
            continue

        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]
        volumes = [b["volume"] for b in bars]

        for event in events:
            if event["surprise_pct"] < surprise_threshold_pct:
                continue

            earnings_epoch = event["epoch"]
            if start_epoch and earnings_epoch < start_epoch:
                continue

            # Find bar index for earnings date
            earn_idx = bisect.bisect_left(epochs, earnings_epoch)
            if earn_idx >= len(epochs):
                continue

            # Check quality at earnings date
            universe = quality_universe.get(epochs[earn_idx])
            if universe is None or sym not in universe:
                found_quality = False
                for offset in range(-5, 6):
                    check_idx = earn_idx + offset
                    if 0 <= check_idx < len(epochs):
                        u = quality_universe.get(epochs[check_idx])
                        if u and sym in u:
                            found_quality = True
                            break
                if not found_quality:
                    continue

            # Check earnings day volume is above average (genuine interest)
            if earn_idx < vol_lookback:
                continue
            avg_vol = sum(volumes[earn_idx - vol_lookback:earn_idx]) / vol_lookback
            if avg_vol <= 0:
                continue
            earn_vol = volumes[earn_idx]
            if earn_vol < avg_vol * 1.5:
                continue  # earnings day needs high volume

            # Find post-earnings peak (highest close in first 5 trading days)
            peak_end = min(earn_idx + 5, len(bars) - 1)
            if peak_end <= earn_idx:
                continue
            post_peak = max(closes[earn_idx:peak_end + 1])
            if post_peak <= 0:
                continue

            # Look for dip with low volume (exhaustion)
            scan_end = min(earn_idx + post_earnings_window, len(bars) - 1)
            for i in range(earn_idx + 5, scan_end):
                if i + 1 >= len(bars):
                    break

                dip_from_peak = (post_peak - closes[i]) / post_peak
                if dip_from_peak < dip_threshold:
                    continue

                # Volume confirmation: dip day volume should be LOW
                dip_vol = volumes[i]
                if earn_vol > 0 and dip_vol > volume_ratio_max * earn_vol:
                    continue  # dip day has too much volume (continued selling)

                # Entry at next day's open
                entry_price = opens[i + 1]
                if entry_price <= 0:
                    continue

                entries.append({
                    "epoch": epochs[i],
                    "symbol": sym,
                    "peak_price": post_peak,
                    "dip_pct": dip_from_peak,
                    "entry_epoch": epochs[i + 1],
                    "entry_price": entry_price,
                })
                break  # One entry per earnings event

    entries.sort(key=lambda x: x["entry_epoch"])
    print(f"  Earnings volume entries: {len(entries)} signals "
          f"(surprise>{surprise_threshold_pct}%, dip>{dip_threshold_pct}%, "
          f"window={post_earnings_window}d, vol_ratio<{volume_ratio_max})")
    return entries


def main():
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    if market == "nse":
        exchange = "NSE"
        start_epoch = 1262304000   # 2010-01-01
        end_epoch = 1773878400     # 2026-03-17
        benchmark_sym = "NIFTYBEES"
        capital = 10_000_000
        description = ("Earnings beat + volume confirmation on NSE: buy quality "
                       "stocks that beat earnings on high volume and dip on low volume.")
    elif market == "us":
        exchange = "US"
        start_epoch = 1104537600   # 2005-01-01
        end_epoch = 1773878400     # 2026-03-17
        benchmark_sym = "SPY"
        capital = 10_000_000
        description = ("Earnings beat + volume confirmation on US: buy quality "
                       "stocks that beat earnings on high volume and dip on low volume.")
    else:
        print(f"Unknown market: {market}. Use --market nse or --market us")
        return

    source = os.environ.get("SOURCE", "native" if exchange == "NSE" else "fmp")
    if "--fmp" in sys.argv:
        source = "fmp"
    elif "--bhavcopy" in sys.argv:
        source = "bhavcopy"

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

    print("\nFetching earnings surprises...")
    earnings = fetch_earnings_surprises(cr, exchange, start_epoch, end_epoch)
    if not earnings:
        print("No earnings data. Aborting.")
        return

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # ── Sweep ──
    param_grid = list(product(
        [5, 10],          # surprise_threshold_pct
        [5, 7, 10],       # dip_threshold_pct
        [10, 20, 30],     # post_earnings_window
        [0.3, 0.5, 0.7],  # volume_ratio_max (dip_vol / earn_vol)
        [5, 10],          # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({market.upper()} earnings + volume)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"regime={REGIME_SMA}, TSL={TSL_PCT}%, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(f"{STRATEGY_NAME}_{market}", "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    for idx, (surprise, dip, window, vol_ratio, pos) in enumerate(param_grid):
        params = {
            "surprise_threshold_pct": surprise,
            "dip_threshold_pct": dip,
            "post_earnings_window": window,
            "volume_ratio_max": vol_ratio,
            "max_positions": pos,
        }

        entries = compute_earnings_volume_entries(
            price_data, earnings, quality_universe,
            surprise, dip, window, vol_ratio, start_epoch=start_epoch)

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
        )

        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        if idx % 10 == 0 or idx == total - 1:
            print(f"  [{idx+1}/{total}] surp={surprise}% dip={dip}% win={window}d "
                  f"vol<{vol_ratio} pos={pos} | CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
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
            print(f"  #{i+1} surp={params['surprise_threshold_pct']}% "
                  f"dip={params['dip_threshold_pct']}% "
                  f"vol<{params['volume_ratio_max']} | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    # ── Volume ratio comparison ──
    print(f"\n{'='*80}")
    print("  VOLUME RATIO COMPARISON")
    print(f"{'='*80}")

    for vr in [0.3, 0.5, 0.7]:
        vr_configs = [(p, r) for p, r in sweep.configs if p["volume_ratio_max"] == vr]
        if vr_configs:
            calmars = [(r.to_dict()["summary"].get("calmar_ratio") or 0) for _, r in vr_configs]
            trades_list = [(r.to_dict()["summary"].get("total_trades") or 0) for _, r in vr_configs]
            avg_calmar = sum(calmars) / len(calmars)
            avg_trades = sum(trades_list) / len(trades_list)
            print(f"  vol<{vr}: avg Calmar={avg_calmar:.2f}, avg trades={avg_trades:.0f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

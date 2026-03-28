#!/usr/bin/env python3
"""Earnings Surprise + Post-Earnings Dip Strategy.

Buys quality stocks that BEAT earnings estimates and then dip within a window.
Post-earnings announcement drift is one of the most robust anomalies in finance.
The unexploited variant: stocks that beat and then dip on "sell the news" or
sector rotation, creating a window where a fundamentally improving stock is
temporarily cheap.

Signal:
  - Quality stock (2yr consecutive positive returns)
  - Fundamental overlay (ROE>15%, PE<25)
  - Earnings beat: epsActual > epsEstimated * (1 + surprise_threshold)
  - Post-earnings dip: price drops X% from post-earnings peak within N days
  - Entry: next-day open (MOC execution)
  - Exit: peak recovery + 10% TSL, or 504d max hold

Data: fmp.earnings_surprises (deduplicated, JOIN with fmp.profile for exchange)

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

STRATEGY_NAME = "earnings_surprise_dip"

# Fixed best params from quality dip-buy experiments
CONSECUTIVE_YEARS = 2
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
ROE_THRESHOLD = 15
PE_THRESHOLD = 25


# ── Earnings Data ───────────────────────────────────────────────────────────

def fetch_earnings_surprises(cr, exchange, start_epoch, end_epoch):
    """Fetch deduplicated earnings surprises from fmp.earnings_surprises.

    Joins with fmp.profile for exchange filtering (earnings table has no exchange column).
    Deduplicates with ROW_NUMBER() partitioned by (symbol, dateEpoch).

    Args:
        cr: CetaResearch client
        exchange: "NSE" or "US"
        start_epoch: start of backtest period
        end_epoch: end of backtest period

    Returns:
        dict[symbol, list[{epoch, eps_actual, eps_estimated, surprise_pct}]]
        sorted by epoch within each symbol.
    """
    if exchange == "NSE":
        exchange_filter = "p.exchange = 'NSE'"
    elif exchange in ("US", "NASDAQ", "NYSE"):
        exchange_filter = "p.exchange IN ('NASDAQ', 'NYSE')"
    else:
        print(f"  Unsupported exchange for earnings: {exchange}")
        return {}

    sql = f"""
    WITH ranked AS (
        SELECT e.symbol,
               CAST(e.dateEpoch AS BIGINT) AS dateEpoch,
               e.epsActual,
               e.epsEstimated,
               ROW_NUMBER() OVER (
                   PARTITION BY e.symbol, CAST(e.dateEpoch AS BIGINT)
                   ORDER BY e.epsActual DESC NULLS LAST
               ) as rn
        FROM fmp.earnings_surprises e
        JOIN fmp.profile p ON e.symbol = p.symbol
        WHERE {exchange_filter}
          AND CAST(e.dateEpoch AS BIGINT) >= {start_epoch}
          AND CAST(e.dateEpoch AS BIGINT) <= {end_epoch}
          AND e.epsEstimated IS NOT NULL
          AND ABS(e.epsEstimated) > 0.01
    )
    SELECT symbol, dateEpoch, epsActual, epsEstimated
    FROM ranked
    WHERE rn = 1
    ORDER BY symbol, dateEpoch
    """

    print(f"  Fetching earnings surprises ({exchange})...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  WARNING: No earnings data fetched")
        return {}

    earnings = {}
    for r in results:
        sym = r["symbol"]
        # Strip .NS suffix for NSE
        if exchange == "NSE" and sym.endswith(".NS"):
            sym = sym[:-3]

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        eps_actual = r.get("epsActual")
        eps_estimated = r.get("epsEstimated")
        if eps_actual is None or eps_estimated is None:
            continue

        eps_actual = float(eps_actual)
        eps_estimated = float(eps_estimated)
        if abs(eps_estimated) < 0.01:
            continue

        surprise_pct = (eps_actual - eps_estimated) / abs(eps_estimated) * 100

        if sym not in earnings:
            earnings[sym] = []
        earnings[sym].append({
            "epoch": epoch,
            "eps_actual": eps_actual,
            "eps_estimated": eps_estimated,
            "surprise_pct": surprise_pct,
        })

    # Sort by epoch within each symbol
    for sym in earnings:
        earnings[sym].sort(key=lambda x: x["epoch"])

    total_events = sum(len(v) for v in earnings.values())
    print(f"  Earnings: {len(earnings)} symbols, {total_events} events")
    return earnings


# ── Post-Earnings Dip Detection ─────────────────────────────────────────────

def compute_post_earnings_dip_entries(
    price_data, earnings, quality_universe,
    surprise_threshold_pct, dip_threshold_pct,
    post_earnings_window, start_epoch=None,
):
    """Find entry signals: earnings beat + subsequent dip within window.

    For each earnings event where surprise >= threshold:
      1. Find the post-earnings peak (highest close in first 5 days after earnings)
      2. Look for a dip from that peak within the post_earnings_window
      3. If dip >= threshold and symbol is in quality universe, generate entry

    Args:
        price_data: dict[symbol, list[{epoch, open, close}]]
        earnings: dict[symbol, list[{epoch, surprise_pct, ...}]] from fetch_earnings_surprises()
        quality_universe: dict[epoch, set[symbol]]
        surprise_threshold_pct: min earnings beat % (e.g., 5 = 5% beat)
        dip_threshold_pct: min post-earnings dip % from peak (e.g., 5 = 5%)
        post_earnings_window: trading days to look for dip after earnings
        start_epoch: only generate signals after this epoch

    Returns:
        list of entry dicts compatible with simulate_portfolio()
    """
    entries = []
    dip_threshold = dip_threshold_pct / 100.0

    for sym, events in earnings.items():
        bars = price_data.get(sym)
        if not bars or len(bars) < 30:
            continue

        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]

        for event in events:
            if event["surprise_pct"] < surprise_threshold_pct:
                continue

            earnings_epoch = event["epoch"]
            if start_epoch and earnings_epoch < start_epoch:
                continue

            # Find the bar index for earnings date (or closest trading day after)
            earn_idx = bisect.bisect_left(epochs, earnings_epoch)
            if earn_idx >= len(epochs):
                continue

            # Check quality at earnings date
            universe = quality_universe.get(epochs[earn_idx])
            if universe is None or sym not in universe:
                # Try nearby epochs (quality rescreen is every 63 days)
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

            # Find post-earnings peak (highest close in first 5 trading days)
            peak_end = min(earn_idx + 5, len(bars) - 1)
            if peak_end <= earn_idx:
                continue
            post_peak = max(closes[earn_idx:peak_end + 1])
            if post_peak <= 0:
                continue

            # Look for dip from post-earnings peak within window
            scan_end = min(earn_idx + post_earnings_window, len(bars) - 1)
            for i in range(earn_idx + 5, scan_end):
                if i + 1 >= len(bars):
                    break

                dip_from_peak = (post_peak - closes[i]) / post_peak
                if dip_from_peak < dip_threshold:
                    continue

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
    print(f"  Post-earnings dip entries: {len(entries)} signals "
          f"(surprise>{surprise_threshold_pct}%, dip>{dip_threshold_pct}%, "
          f"window={post_earnings_window}d)")
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
    description = (f"Earnings surprise + post-earnings dip on {exchange}: buy quality "
                   "stocks that beat earnings and then dip.")

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

    print("\nFetching earnings surprises...")
    earnings = fetch_earnings_surprises(cr, exchange, start_epoch, end_epoch)
    if not earnings:
        print("No earnings data. Aborting.")
        return

    print("\nFetching fundamentals...")
    fundamentals = fetch_fundamentals(cr, exchange)

    # Pre-compute shared data
    print("\nComputing quality universe...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=start_epoch)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)

    # ── Sweep ──
    param_grid = list(product(
        [5, 10, 20],     # surprise_threshold_pct (min earnings beat %)
        [5, 7, 10],      # dip_threshold_pct (post-earnings dip)
        [10, 20, 30],    # post_earnings_window (trading days)
        [5, 10],         # max_positions
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({market.upper()} earnings surprise + dip)")
    print(f"  Fixed: {CONSECUTIVE_YEARS}yr quality, ROE>{ROE_THRESHOLD}% PE<{PE_THRESHOLD}, "
          f"regime={REGIME_SMA}, TSL={TSL_PCT}%, hold={MAX_HOLD_DAYS}d")
    print(f"{'='*80}")

    sweep = SweepResult(f"{STRATEGY_NAME}_{market}", "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    for idx, (surprise, dip, window, pos) in enumerate(param_grid):
        params = {
            "surprise_threshold_pct": surprise,
            "dip_threshold_pct": dip,
            "post_earnings_window": window,
            "max_positions": pos,
        }

        # Generate post-earnings dip entries
        entries = compute_post_earnings_dip_entries(
            price_data, earnings, quality_universe,
            surprise, dip, window, start_epoch=start_epoch)

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
        print(f"  [{idx+1}/{total}] surp={surprise}% dip={dip}% win={window}d "
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
            print(f"  #{i+1} surp={params['surprise_threshold_pct']}% "
                  f"dip={params['dip_threshold_pct']}% "
                  f"win={params['post_earnings_window']}d | "
                  f"CAGR={s.get('cagr',0)*100:+.1f}% -> {adj['cagr_adj']*100:+.1f}% "
                  f"Cal={s.get('calmar_ratio',0):.2f} -> {adj['calmar_adj']:.2f}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        best.print_summary()


if __name__ == "__main__":
    main()

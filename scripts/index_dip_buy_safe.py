#!/usr/bin/env python3
"""Enhanced index dip-buy with crash safety filters.

Multi-tier buying + speed-of-decline protection + dual timeframe direction +
volatility scaling.  Sweeps per index to find optimal parameters.

Data sources:
  - India (NIFTYBEES, BANKBEES): nse.nse_charting_day
  - Global indices / US ETFs:     fmp.stock_eod (adjClose)
"""

import sys
import os
import math
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch


# ── Symbols ──────────────────────────────────────────────────────────────────

SYMBOLS = [
    # (symbol, label, source)  source = "nse" or "fmp"
    ("NIFTYBEES", "NIFTYBEES", "nse"),
    ("BANKBEES", "BANKBEES", "nse"),
    ("^GSPC", "S&P 500", "fmp"),
    ("^NSEI", "NIFTY 50", "fmp"),
    ("^BSESN", "Sensex", "fmp"),
    ("^FTSE", "FTSE 100", "fmp"),
    ("^N225", "Nikkei 225", "fmp"),
    ("^HSI", "Hang Seng", "fmp"),
    ("^GDAXI", "DAX", "fmp"),
    ("SPY", "SPY ETF", "fmp"),
    ("QQQ", "QQQ ETF", "fmp"),
]


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class Config:
    # Dip detection
    peak_lookback: int = 50          # rolling high window
    dip_threshold_pct: float = 5.0   # tier-1 buy trigger

    # Position sizing
    buy_fraction: float = 0.5        # fraction of available cash per buy
    n_tiers: int = 1                 # 1 = single buy, 2/3 = average down at 2x/3x threshold
    min_days_between_buys: int = 5
    min_cash_reserve: float = 0.05   # keep 5% reserve

    # Crash safety — speed of decline
    crash_pause_daily_pct: float = 3.0    # pause buying N days after single-day drop > X%
    crash_pause_days: int = 5
    crash_5d_threshold: float = 8.0       # reduce position 50% if 5-day cumulative drop > X%
    crash_10d_threshold: float = 15.0     # go to cash if 10-day cumulative drop > X%

    # Dual timeframe direction
    use_direction_filter: bool = True
    sma_short: int = 20
    sma_long: int = 200
    # Regime zones: both_up=1.0, short_down_long_up=1.0 (buy dip!),
    #   both_down=0.3, short_up_long_down=0.5
    regime_both_down_mult: float = 0.3
    regime_bear_rally_mult: float = 0.5

    # Volatility scaling
    use_vol_scaling: bool = True
    vol_window: int = 20
    vol_avg_window: int = 60
    vol_high_mult: float = 2.0     # halve size when vol > 2x average
    vol_extreme_mult: float = 3.0  # quarter size when vol > 3x average

    # Exit
    exit_mode: str = "never"       # "never", "recovery_ma", "smart"
    exit_ma_period: int = 20
    exit_profit_pct: float = 10.0  # for smart: only exit if profit > X%

    # Capital
    start_capital: float = 10_000_000


# ── Data Fetching ────────────────────────────────────────────────────────────

def fetch_data(cr, symbol, source, start_epoch, end_epoch):
    """Fetch OHLCV data, return list of dicts sorted by epoch."""
    warmup_epoch = start_epoch - 300 * 86400

    if source == "nse":
        sql = f"""SELECT date_epoch, open, high, low, close, volume
                  FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}'
                    AND date_epoch >= {warmup_epoch} AND date_epoch <= {end_epoch}
                  ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, open, high, low, adjClose as close, volume
                  FROM fmp.stock_eod
                  WHERE symbol = '{symbol}'
                    AND dateEpoch >= {warmup_epoch} AND dateEpoch <= {end_epoch}
                  ORDER BY dateEpoch"""

    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(5)
            else:
                return [], 0

    if not results:
        return [], 0

    data = []
    for r in results:
        c = float(r.get("close") or 0)
        if c <= 0:
            continue
        data.append({
            "epoch": int(r["date_epoch"]),
            "open": float(r.get("open") or c),
            "high": float(r.get("high") or c),
            "low": float(r.get("low") or c),
            "close": c,
            "volume": float(r.get("volume") or 0),
        })

    data.sort(key=lambda x: x["epoch"])

    # Find sim start index
    start_idx = 0
    for i, d in enumerate(data):
        if d["epoch"] >= start_epoch:
            start_idx = i
            break

    return data, start_idx


# ── Indicators ───────────────────────────────────────────────────────────────

def compute_sma(values, period):
    sma = [0.0] * len(values)
    running = 0.0
    for i in range(len(values)):
        running += values[i]
        if i >= period:
            running -= values[i - period]
        sma[i] = running / min(i + 1, period)
    return sma


def compute_rsi(closes, period):
    rsi = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsi
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        avg_gain += max(change, 0)
        avg_loss += max(-change, 0)
    avg_gain /= period
    avg_loss /= period
    for i in range(period, len(closes)):
        if i > period:
            change = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        rsi[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
    return rsi


def compute_realized_vol(closes, window):
    """Annualized realized volatility from daily returns."""
    vol = [0.0] * len(closes)
    for i in range(1, len(closes)):
        start = max(1, i - window + 1)
        rets = []
        for j in range(start, i + 1):
            if closes[j - 1] > 0:
                rets.append(math.log(closes[j] / closes[j - 1]))
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol[i] = math.sqrt(var) * math.sqrt(252)
    return vol


def compute_cumulative_return(closes, window):
    """N-day cumulative return (negative = drawdown)."""
    cum_ret = [0.0] * len(closes)
    for i in range(window, len(closes)):
        if closes[i - window] > 0:
            cum_ret[i] = (closes[i] - closes[i - window]) / closes[i - window] * 100
    return cum_ret


# ── Simulation ───────────────────────────────────────────────────────────────

def simulate(data, start_idx, cfg: Config):
    """Run dip-buy simulation with crash safety filters.

    Returns (day_values, stats_dict).
    """
    closes = [d["close"] for d in data]
    n = len(closes)

    # Precompute indicators
    sma_short = compute_sma(closes, cfg.sma_short)
    sma_long = compute_sma(closes, cfg.sma_long)
    rsi2 = compute_rsi(closes, 2)
    rsi5 = compute_rsi(closes, 5)
    realized_vol = compute_realized_vol(closes, cfg.vol_window)
    avg_vol = compute_sma(realized_vol, cfg.vol_avg_window)
    cum_ret_1d = compute_cumulative_return(closes, 1)
    cum_ret_5d = compute_cumulative_return(closes, 5)
    cum_ret_10d = compute_cumulative_return(closes, 10)

    cash = cfg.start_capital
    positions = []  # list of (price, qty, cost)
    last_buy_idx = -999
    total_buys = 0
    total_sells = 0
    crash_pause_until = -1  # index until which buying is paused

    values = []

    for i in range(start_idx, n):
        close = closes[i]

        # ── CRASH SAFETY: speed of decline ──
        # Single-day crash pause
        if cum_ret_1d[i] < -cfg.crash_pause_daily_pct:
            crash_pause_until = max(crash_pause_until, i + cfg.crash_pause_days)

        # 10-day catastrophic: liquidate everything
        if cfg.crash_10d_threshold > 0 and cum_ret_10d[i] < -cfg.crash_10d_threshold:
            if positions:
                total_qty = sum(p[1] for p in positions)
                cash += total_qty * close
                total_sells += len(positions)
                positions = []
            crash_pause_until = max(crash_pause_until, i + cfg.crash_pause_days * 2)

        # ── EXIT LOGIC ──
        if positions and cfg.exit_mode != "never":
            should_exit = False

            if cfg.exit_mode == "smart":
                # Exit on overbought
                exit_score = 0
                if rsi2[i] > 85:
                    exit_score += 2
                if rsi5[i] > 70:
                    exit_score += 1
                ext_pct = (close - sma_short[i]) / sma_short[i] * 100 if sma_short[i] > 0 else 0
                if ext_pct > 10:
                    exit_score += 1

                avg_entry = sum(p[2] for p in positions) / sum(p[1] for p in positions)
                profit_pct = (close - avg_entry) / avg_entry * 100

                if exit_score >= 3 and profit_pct > cfg.exit_profit_pct:
                    should_exit = True

            elif cfg.exit_mode == "recovery_ma":
                exit_ma = compute_sma(closes[:i + 1], cfg.exit_ma_period)
                avg_entry = sum(p[2] for p in positions) / sum(p[1] for p in positions)
                if close > exit_ma[-1] and close > avg_entry:
                    should_exit = True

            if should_exit:
                total_qty = sum(p[1] for p in positions)
                cash += total_qty * close
                total_sells += len(positions)
                positions = []

        # ── ENTRY LOGIC ──
        days_since = i - last_buy_idx
        min_cash = cfg.start_capital * cfg.min_cash_reserve
        buying_paused = i <= crash_pause_until

        if not buying_paused and days_since >= cfg.min_days_between_buys and cash > min_cash:
            # Measure dip from rolling peak
            peak_start = max(0, i - cfg.peak_lookback + 1)
            rolling_peak = max(closes[peak_start:i + 1])
            dip_pct = (rolling_peak - close) / rolling_peak * 100 if rolling_peak > 0 else 0

            # Determine which tier we're in
            tier = 0
            if dip_pct >= cfg.dip_threshold_pct:
                tier = 1
            if cfg.n_tiers >= 2 and dip_pct >= cfg.dip_threshold_pct * 2:
                tier = 2
            if cfg.n_tiers >= 3 and dip_pct >= cfg.dip_threshold_pct * 3:
                tier = 3

            if tier > 0:
                # ── DIRECTION FILTER ──
                direction_mult = 1.0
                if cfg.use_direction_filter:
                    short_up = close > sma_short[i]
                    long_up = close > sma_long[i]

                    if short_up and long_up:
                        direction_mult = 1.0        # normal bull
                    elif not short_up and long_up:
                        direction_mult = 1.0        # dip in uptrend — best zone!
                    elif not short_up and not long_up:
                        direction_mult = cfg.regime_both_down_mult   # bear market
                    else:  # short_up and not long_up
                        direction_mult = cfg.regime_bear_rally_mult  # bear rally

                # ── VOLATILITY SCALING ──
                vol_mult = 1.0
                if cfg.use_vol_scaling and avg_vol[i] > 0 and realized_vol[i] > 0:
                    vol_ratio = realized_vol[i] / avg_vol[i]
                    if vol_ratio > cfg.vol_extreme_mult:
                        vol_mult = 0.25
                    elif vol_ratio > cfg.vol_high_mult:
                        vol_mult = 0.5

                # ── 5-day crash: halve position ──
                crash_5d_mult = 1.0
                if cfg.crash_5d_threshold > 0 and cum_ret_5d[i] < -cfg.crash_5d_threshold:
                    crash_5d_mult = 0.5

                # Compute buy amount
                base_fraction = cfg.buy_fraction * tier  # more aggressive at deeper dips
                base_fraction = min(base_fraction, 1.0)
                effective_fraction = base_fraction * direction_mult * vol_mult * crash_5d_mult
                buy_amount = (cash - min_cash) * effective_fraction

                if buy_amount > 0 and close > 0:
                    qty = buy_amount / close
                    positions.append((close, qty, buy_amount))
                    cash -= buy_amount
                    total_buys += 1
                    last_buy_idx = i

        # Portfolio value
        pos_val = sum(p[1] * close for p in positions)
        values.append(cash + pos_val)

    return values, total_buys, total_sells


# ── Buy-and-hold benchmark ───────────────────────────────────────────────────

def simulate_buy_hold(data, start_idx):
    closes = [d["close"] for d in data]
    start_price = closes[start_idx]
    capital = 10_000_000
    qty = capital / start_price
    return [qty * closes[i] for i in range(start_idx, len(closes))]


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_stats(values, data, start_idx):
    if len(values) < 2:
        return None
    start_val, end_val = values[0], values[-1]
    start_epoch = data[start_idx]["epoch"]
    end_epoch = data[start_idx + len(values) - 1]["epoch"]
    years = (end_epoch - start_epoch) / (365.25 * 86400)
    if years <= 0 or end_val <= 0 or start_val <= 0:
        return None

    total_return = end_val / start_val
    cagr = (total_return ** (1 / years) - 1) * 100

    peak = values[0]
    max_dd = 0
    for v in values:
        peak = max(peak, v)
        dd = (v - peak) / peak * 100
        max_dd = min(max_dd, dd)

    calmar = cagr / abs(max_dd) if max_dd != 0 else 0

    # Year-wise
    yearly = {}
    for j, v in enumerate(values):
        epoch = data[start_idx + j]["epoch"]
        yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v, "peak": v, "trough": v}
        yearly[yr]["last"] = v
        yearly[yr]["peak"] = max(yearly[yr]["peak"], v)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], v)

    return {
        "cagr": cagr, "max_dd": max_dd, "calmar": calmar,
        "total_return": total_return, "years": years, "yearly": yearly,
    }


# ── Sweep ────────────────────────────────────────────────────────────────────

def build_sweep_configs():
    """Generate configs to sweep. Returns list of (Config, label)."""
    configs = []

    for peak_lb in [20, 50, 100]:
        for dip_thresh in [3, 5, 8, 10, 15]:
            for buy_frac in [0.3, 0.5, 0.7]:
                for n_tiers in [1, 2, 3]:
                    for exit_mode in ["never", "smart"]:
                        for use_dir in [True, False]:
                            for use_vol in [True, False]:
                                for crash_daily in [3.0, 5.0]:
                                    configs.append(Config(
                                        peak_lookback=peak_lb,
                                        dip_threshold_pct=dip_thresh,
                                        buy_fraction=buy_frac,
                                        n_tiers=n_tiers,
                                        exit_mode=exit_mode,
                                        use_direction_filter=use_dir,
                                        use_vol_scaling=use_vol,
                                        crash_pause_daily_pct=crash_daily,
                                    ))

    return configs


def build_focused_sweep():
    """Smaller focused sweep for faster iteration."""
    configs = []

    for peak_lb in [50, 100]:
        for dip_thresh in [5, 8, 10]:
            for buy_frac in [0.3, 0.5, 0.7]:
                for n_tiers in [1, 3]:
                    for exit_mode in ["never", "smart"]:
                        # Safety on vs off
                        for safety in ["full", "none"]:
                            if safety == "full":
                                configs.append(Config(
                                    peak_lookback=peak_lb,
                                    dip_threshold_pct=dip_thresh,
                                    buy_fraction=buy_frac,
                                    n_tiers=n_tiers,
                                    exit_mode=exit_mode,
                                    use_direction_filter=True,
                                    use_vol_scaling=True,
                                    crash_pause_daily_pct=3.0,
                                    crash_5d_threshold=8.0,
                                    crash_10d_threshold=15.0,
                                ))
                            else:
                                configs.append(Config(
                                    peak_lookback=peak_lb,
                                    dip_threshold_pct=dip_thresh,
                                    buy_fraction=buy_frac,
                                    n_tiers=n_tiers,
                                    exit_mode=exit_mode,
                                    use_direction_filter=False,
                                    use_vol_scaling=False,
                                    crash_pause_daily_pct=99.0,  # never triggers
                                    crash_5d_threshold=0,
                                    crash_10d_threshold=0,
                                ))

    return configs


def sweep_single_index(data, start_idx, label, configs):
    """Sweep all configs on one index. Returns sorted results."""
    results = []

    for cfg in configs:
        vals, buys, sells = simulate(data, start_idx, cfg)
        stats = compute_stats(vals, data, start_idx)
        if stats and abs(stats["max_dd"]) > 0.01:
            results.append({
                "cfg": cfg, "buys": buys, "sells": sells, **stats,
            })

    results.sort(key=lambda r: r["calmar"], reverse=True)
    return results


def print_top_results(results, label, n=15):
    """Print top N results for an index."""
    print(f"\n{'='*150}")
    print(f"  {label} — TOP {min(n, len(results))} by Calmar")
    print(f"{'='*150}")
    print(f"{'#':<3} {'Peak':>4} {'Dip%':>5} {'BuyF':>5} {'Tier':>4} {'Exit':>6} "
          f"{'Dir':>3} {'Vol':>3} {'CrD':>4} "
          f"{'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Buy':>4} {'Sel':>4}")
    print("-" * 100)

    for i, r in enumerate(results[:n]):
        c = r["cfg"]
        d = "Y" if c.use_direction_filter else "N"
        v = "Y" if c.use_vol_scaling else "N"
        print(f"{i+1:<3} {c.peak_lookback:>4} {c.dip_threshold_pct:>5.0f} "
              f"{c.buy_fraction:>5.1f} {c.n_tiers:>4} {c.exit_mode:>6} "
              f"{d:>3} {v:>3} {c.crash_pause_daily_pct:>4.0f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{r['total_return']:>5.1f}x {r['buys']:>4} {r['sells']:>4}")


def print_yearwise(results, data, start_idx, label):
    """Print year-wise for best config."""
    if not results:
        return
    best = results[0]
    yearly = best["yearly"]
    print(f"\n  {label} — BEST CONFIG year-wise:")
    c = best["cfg"]
    print(f"  peak={c.peak_lookback}, dip={c.dip_threshold_pct}%, buy_frac={c.buy_fraction}, "
          f"tiers={c.n_tiers}, exit={c.exit_mode}, "
          f"dir={'ON' if c.use_direction_filter else 'OFF'}, "
          f"vol={'ON' if c.use_vol_scaling else 'OFF'}")
    print(f"  CAGR={best['cagr']:.1f}%, MaxDD={best['max_dd']:.1f}%, "
          f"Calmar={best['calmar']:.2f}, Growth={best['total_return']:.1f}x")

    print(f"\n  {'Year':<6} {'Return':>9} {'Max DD':>9}")
    print(f"  {'-'*28}")
    for yr in sorted(yearly.keys()):
        y = yearly[yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
        print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")


def print_safety_comparison(data, start_idx, label):
    """Compare safety-on vs safety-off for the same base config."""
    base_params = dict(
        peak_lookback=50, dip_threshold_pct=5, buy_fraction=0.5,
        n_tiers=1, exit_mode="never", min_days_between_buys=5,
    )

    no_safety = Config(**base_params, use_direction_filter=False,
                       use_vol_scaling=False, crash_pause_daily_pct=99,
                       crash_5d_threshold=0, crash_10d_threshold=0)

    with_safety = Config(**base_params, use_direction_filter=True,
                         use_vol_scaling=True, crash_pause_daily_pct=3.0,
                         crash_5d_threshold=8.0, crash_10d_threshold=15.0)

    # Also test direction-only and vol-only
    dir_only = Config(**base_params, use_direction_filter=True,
                      use_vol_scaling=False, crash_pause_daily_pct=99,
                      crash_5d_threshold=0, crash_10d_threshold=0)

    vol_only = Config(**base_params, use_direction_filter=False,
                      use_vol_scaling=True, crash_pause_daily_pct=99,
                      crash_5d_threshold=0, crash_10d_threshold=0)

    crash_only = Config(**base_params, use_direction_filter=False,
                        use_vol_scaling=False, crash_pause_daily_pct=3.0,
                        crash_5d_threshold=8.0, crash_10d_threshold=15.0)

    configs = [
        ("No Safety", no_safety),
        ("Dir Only", dir_only),
        ("Vol Only", vol_only),
        ("Crash Only", crash_only),
        ("Full Safety", with_safety),
    ]

    # Buy-and-hold baseline
    vals_bh = simulate_buy_hold(data, start_idx)
    stats_bh = compute_stats(vals_bh, data, start_idx)

    print(f"\n  {label} — SAFETY FILTER COMPARISON (base: peak=50, dip=5%, frac=0.5, never-sell)")
    print(f"  {'Mode':<14} {'CAGR':>7} {'MaxDD':>7} {'Calm':>6} {'Grwth':>6} {'Buy':>4} {'Sel':>4}")
    print(f"  {'-'*55}")

    if stats_bh:
        print(f"  {'Buy&Hold':<14} {stats_bh['cagr']:>6.1f}% {stats_bh['max_dd']:>6.1f}% "
              f"{stats_bh['calmar']:>6.2f} {stats_bh['total_return']:>5.1f}x {'—':>4} {'—':>4}")

    for name, cfg in configs:
        vals, buys, sells = simulate(data, start_idx, cfg)
        stats = compute_stats(vals, data, start_idx)
        if stats:
            print(f"  {name:<14} {stats['cagr']:>6.1f}% {stats['max_dd']:>6.1f}% "
                  f"{stats['calmar']:>6.2f} {stats['total_return']:>5.1f}x {buys:>4} {sells:>4}")


# ── Cross-Index Summary ──────────────────────────────────────────────────────

def print_cross_index_summary(all_results):
    """Compare best config per index against buy-and-hold."""
    print(f"\n{'='*160}")
    print(f"  CROSS-INDEX SUMMARY — Best dip-buy config vs Buy-and-Hold per index")
    print(f"{'='*160}")
    print(f"  {'Index':<14} {'BH CAGR':>8} {'BH DD':>7} {'BH Calm':>8} "
          f"{'Dip CAGR':>9} {'Dip DD':>7} {'Dip Calm':>9} "
          f"{'Alpha':>7} {'Safety':>7} {'Config':>40}")
    print(f"  {'-'*140}")

    for label, bh_stats, results in all_results:
        if not results or not bh_stats:
            print(f"  {label:<14} {'—':>8} {'—':>7} {'—':>8} {'—':>9} {'—':>7} {'—':>9} {'—':>7} {'—':>7}")
            continue

        best = results[0]
        c = best["cfg"]
        alpha = best["cagr"] - bh_stats["cagr"]
        safety = "Y" if (c.use_direction_filter or c.use_vol_scaling) else "N"
        cfg_str = f"p{c.peak_lookback}/d{c.dip_threshold_pct:.0f}/f{c.buy_fraction}/t{c.n_tiers}/{c.exit_mode[:3]}"

        print(f"  {label:<14} "
              f"{bh_stats['cagr']:>+7.1f}% {bh_stats['max_dd']:>6.1f}% {bh_stats['calmar']:>7.2f}   "
              f"{best['cagr']:>+8.1f}% {best['max_dd']:>6.1f}% {best['calmar']:>8.2f}   "
              f"{alpha:>+6.1f}% {safety:>6}   {cfg_str:>40}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()
    configs = build_focused_sweep()
    print(f"Sweep size: {len(configs)} configs per index")

    all_results = []  # (label, bh_stats, sorted_results)

    for symbol, label, source in SYMBOLS:
        print(f"\n{'='*80}")
        print(f"  Fetching {label} ({symbol}) from {source}...")
        data, start_idx = fetch_data(cr, symbol, source, start_epoch, end_epoch)
        if not data or start_idx >= len(data) - 100:
            print(f"  Insufficient data, skipping")
            all_results.append((label, None, []))
            continue

        n_rows = len(data) - start_idx
        print(f"  {n_rows} trading days")

        # Buy-and-hold baseline
        vals_bh = simulate_buy_hold(data, start_idx)
        stats_bh = compute_stats(vals_bh, data, start_idx)

        # Safety comparison (fixed base config)
        print_safety_comparison(data, start_idx, label)

        # Full sweep
        print(f"\n  Sweeping {len(configs)} configs...")
        results = sweep_single_index(data, start_idx, label, configs)
        print(f"  {len(results)} valid results")

        print_top_results(results, label)
        print_yearwise(results, data, start_idx, label)

        all_results.append((label, stats_bh, results))

    # Cross-index summary
    print_cross_index_summary(all_results)


if __name__ == "__main__":
    main()

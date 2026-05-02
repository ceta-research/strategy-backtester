"""Pair Trading Phase 2 — Intraday backtest on cointegrated Nifty 50 pairs.

Strategy:
  - Universe: top N qualifying pairs from pair_discovery_phase1.json
  - Walk-forward β refit: every 90 days, fit OLS Y ~ α + β·X on prior 252 days
  - Daily spread distribution: rolling 60 trading days mean & std
  - Intraday signal at each 5-min bar:
      z(t) = (intraday_spread(t) - rolling_mean) / rolling_std
      where intraday_spread(t) = log(Y_bar.close) − β · log(X_bar.close)
  - Entry: first bar of day where |z| > entry_threshold AND no open position
      • z > +entry → SHORT Y, LONG β·X  (spread is high → expect to fall)
      • z < −entry → LONG Y,  SHORT β·X (spread is low → expect to rise)
  - Exit (in priority order, checked each subsequent bar):
      • |z| < exit_target  → target hit, close at limit
      • |z| > exit_stop    → stop hit, close at market
      • bar_minute >= eod  → EOD square-off, close at bar close
  - Position: max_pairs concurrent. Per-pair $V split: $V/(1+β) on Y, β·$V/(1+β) on X.

Cost model: nse_intraday_charges on each leg separately (paid both legs RT).

OOS validation: 2025 is the held-out year; pair selection used 2022-2024 only.

Self-contained. Loads pair list from pair_discovery_phase1.json on prod.
"""
import sys, json, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import statistics

import numpy as np
import polars as pl
import statsmodels.api as sm

sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import (
    load_daily_data, load_minute_data,
    nse_intraday_charges, SECONDS_IN_ONE_DAY,
)

OR_OFFSET = 555         # 09:15 IST as bar_minute
NSE_CLOSE_MINUTE = 930  # 15:30 IST


# ── Configs ──────────────────────────────────────────────────────────────

CONFIG = {
    "label": "Intraday Pair v0",
    "start_date": "2022-04-01",   # need 60+90 days warm-up before first signal
    "end_date": "2025-12-31",
    "in_sample_end": "2024-12-31",  # 2025 is OOS
    "initial_capital": 1_000_000,
    "max_pairs": 5,
    "n_pairs_to_use": 20,         # top N from discovery
    "bar_aggregation_minutes": 5,
    "z_window_days": 60,          # rolling daily z-score window
    "beta_refit_window_days": 252, # 1 year of daily data for β refit
    "beta_refit_every_n_days": 90, # refit quarterly
    "entry_threshold": 2.0,
    "exit_target": 0.5,
    "exit_stop": 4.0,
    "eod_exit_minute": 900,       # 15:00 IST (30min before close)
    "slippage_bps_list": [0, 3],
    "discovery_json": "/home/swas/backtester/pair_discovery_phase1.json",
}


# ── Load discovered pairs ────────────────────────────────────────────────

def load_top_pairs(json_path: str, n_top: int) -> list[dict]:
    """Load top N qualifying pairs from discovery output."""
    with open(json_path) as f:
        d = json.load(f)
    qual = d.get("qualifying", [])
    if not qual:
        raise RuntimeError("No qualifying pairs in discovery output.")
    # Already ranked by score in discovery; take top N
    top = qual[:n_top]
    print(f"  Loaded {len(top)} pairs (top {n_top} by composite score):")
    for i, r in enumerate(top, 1):
        print(f"    {i:2d}. {r['pair']:<22} p={r['p_adf']:.4f} β={r['beta']:.3f} "
              f"hl={r['half_life_days']:.1f}d hurst={r['hurst']:.3f}")
    return top


# ── Resample minute → 5-min OHLC ─────────────────────────────────────────

def resample_to_5min(df_minute: pl.DataFrame, agg_min: int) -> pl.DataFrame:
    """Aggregate 1-min bars to N-min bars per (symbol, day).

    Returns columns: symbol, date_key, bar5_minute, open, high, low, close, volume
    where bar5_minute = OR_OFFSET + i * agg_min for i in 0..(N_bars-1).
    """
    df = df_minute.with_columns([
        ((pl.col("bar_minute") - OR_OFFSET) // agg_min * agg_min + OR_OFFSET)
        .alias("bar5_minute")
    ])
    aggd = (df.group_by(["symbol", "date_key", "bar5_minute"])
              .agg([
                  pl.col("open").first().alias("open"),
                  pl.col("high").max().alias("high"),
                  pl.col("low").min().alias("low"),
                  pl.col("close").last().alias("close"),
                  pl.col("volume").sum().alias("volume"),
              ])
              .sort(["symbol", "date_key", "bar5_minute"]))
    return aggd


# ── Build daily closes from minute data (intraday-consistent prices) ──────

def build_daily_closes(df_5min: pl.DataFrame) -> pl.DataFrame:
    """Last 5-min bar's close per (symbol, date_key) ≈ daily close.

    Important: we use minute-derived closes (not NSE daily) for spread
    history so that the intraday spread at a 5-min bar is comparable to
    the daily distribution from the same data source.
    """
    daily = (df_5min.group_by(["symbol", "date_key"])
                    .agg(pl.col("close").last().alias("close"))
                    .sort(["symbol", "date_key"]))
    return daily


# ── Walk-forward β refit ─────────────────────────────────────────────────

def refit_beta(daily_y: np.ndarray, daily_x: np.ndarray) -> tuple[float, float]:
    """OLS log(y) = α + β·log(x) on aligned arrays. Returns (alpha, beta)."""
    log_x = np.log(daily_x)
    log_y = np.log(daily_y)
    X = sm.add_constant(log_x)
    res = sm.OLS(log_y, X).fit()
    return float(res.params[0]), float(res.params[1])


# ── Per-pair simulator ───────────────────────────────────────────────────

class PairState:
    """Holds the rolling state for one pair across the whole sim."""

    def __init__(self, pair_info: dict):
        self.y_sym = pair_info["y"] + ".NS"  # FMP format for minute data
        self.x_sym = pair_info["x"] + ".NS"
        self.label = pair_info["pair"]
        # Will be set/updated by refit_beta
        self.alpha = pair_info.get("alpha", 0.0)
        self.beta = pair_info.get("beta", 1.0)
        # Open position state
        self.in_position = False
        self.position_side = None    # "long_y" or "short_y"
        self.entry_y_price = None
        self.entry_x_price = None
        self.entry_time = None       # (date_key, bar5_minute)
        self.entry_z = None
        self.notional_y = 0.0
        self.notional_x = 0.0


def compute_z_now(spread_now: float, history: list[float]) -> tuple[float, float, float]:
    """Z-score of spread_now vs trailing history list. Returns (z, mean, std)."""
    if len(history) < 5:
        return 0.0, 0.0, 1.0
    arr = np.asarray(history)
    mu = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))
    if sd < 1e-9:
        return 0.0, mu, sd
    return float((spread_now - mu) / sd), mu, sd


def open_position(pair: PairState, side: str, y_price: float, x_price: float,
                  per_pair_capital: float, slip: float, t_key: int, t_min: int, z: float):
    """Set position state. side ∈ {long_y, short_y}.
       long_y  => long Y, short β·X (when z < -threshold)
       short_y => short Y, long β·X (when z > +threshold)
    Notional split: V_y = capital/(1+β), V_x = β · V_y. β taken from pair.
    """
    beta = abs(pair.beta) if pair.beta != 0 else 1.0
    v_y = per_pair_capital / (1.0 + beta)
    v_x = beta * v_y

    # Slippage on entry: long fills at slightly higher, short at slightly lower
    if side == "long_y":
        entry_y = y_price * (1 + slip / 10000)   # buying Y
        entry_x = x_price * (1 - slip / 10000)   # selling X
    else:  # short_y
        entry_y = y_price * (1 - slip / 10000)   # selling Y
        entry_x = x_price * (1 + slip / 10000)   # buying X

    pair.in_position = True
    pair.position_side = side
    pair.entry_y_price = entry_y
    pair.entry_x_price = entry_x
    pair.entry_time = (t_key, t_min)
    pair.entry_z = z
    pair.notional_y = v_y
    pair.notional_x = v_x


def close_position(pair: PairState, y_price: float, x_price: float,
                   slip: float, exit_type: str) -> tuple[dict, float]:
    """Close position. Returns (trade_record, pnl)."""
    side = pair.position_side
    v_y, v_x = pair.notional_y, pair.notional_x

    if side == "long_y":
        # Long Y, short X — exit: sell Y (lower price w/ slippage), buy X (higher)
        exit_y = y_price * (1 - slip / 10000)
        exit_x = x_price * (1 + slip / 10000)
        ret_y = (exit_y - pair.entry_y_price) / pair.entry_y_price   # long Y P&L
        ret_x = (pair.entry_x_price - exit_x) / pair.entry_x_price   # short X P&L
    else:  # short_y
        exit_y = y_price * (1 + slip / 10000)
        exit_x = x_price * (1 - slip / 10000)
        ret_y = (pair.entry_y_price - exit_y) / pair.entry_y_price   # short Y P&L
        ret_x = (exit_x - pair.entry_x_price) / pair.entry_x_price   # long X P&L

    pnl_gross = ret_y * v_y + ret_x * v_x
    # Costs: full intraday charge on each leg (charged on order value at entry)
    cost_y = nse_intraday_charges(v_y)
    cost_x = nse_intraday_charges(v_x)
    pnl = pnl_gross - cost_y - cost_x

    rec = {
        "pair": pair.label,
        "side": side,
        "entry_z": round(pair.entry_z, 3),
        "entry_t": pair.entry_time,
        "exit_t": None,  # filled by caller
        "entry_y": round(pair.entry_y_price, 2),
        "entry_x": round(pair.entry_x_price, 2),
        "exit_y": round(exit_y, 2),
        "exit_x": round(exit_x, 2),
        "v_y": round(v_y, 0),
        "v_x": round(v_x, 0),
        "pnl_gross": round(pnl_gross, 2),
        "costs": round(cost_y + cost_x, 2),
        "pnl": round(pnl, 2),
        "exit_type": exit_type,
    }
    pair.in_position = False
    pair.position_side = None
    return rec, pnl


# ── Main simulator ───────────────────────────────────────────────────────

def run_sim(df_5min_idx: dict, daily_closes: dict, top_pairs: list[dict],
            config: dict) -> tuple[list, list]:
    """Walk through trading days, manage positions, return (trades, equity)."""
    slip = config["slippage_bps"]
    eod = config["eod_exit_minute"]
    z_window = config["z_window_days"]
    beta_window = config["beta_refit_window_days"]
    refit_every = config["beta_refit_every_n_days"]
    entry_thr = config["entry_threshold"]
    exit_target = config["exit_target"]
    exit_stop = config["exit_stop"]
    max_pairs = config["max_pairs"]

    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    pair_states = [PairState(p) for p in top_pairs]

    # Pair-specific daily-spread history (for rolling mean/std)
    # spread_history[pair_idx] = list of (date_key, spread)
    spread_history = [[] for _ in pair_states]

    # Pre-populate spread history from pre-start daily closes (FIX: avoid
    # under-windowed std on first ~60 sim days inflating |z|).
    print("  Pre-populating spread history from pre-start data...")
    for pi, ps in enumerate(pair_states):
        y_dc = daily_closes.get(ps.y_sym, {})
        x_dc = daily_closes.get(ps.x_sym, {})
        aligned_dks = sorted(set(y_dc.keys()) & set(x_dc.keys()))
        pre_start = [dk for dk in aligned_dks
                     if dk * SECONDS_IN_ONE_DAY < start_ep]
        # Use up to z_window worth of pre-start data
        for dk in pre_start[-z_window:]:
            s = float(np.log(y_dc[dk]) - ps.beta * np.log(x_dc[dk]))
            spread_history[pi].append((dk, s))
    pop_lens = [len(h) for h in spread_history]
    print(f"    Pre-populated: min={min(pop_lens)}, max={max(pop_lens)}, "
          f"median={int(np.median(pop_lens))} entries per pair")

    # All trading days in range
    all_date_keys = sorted({
        dk for sym_idx in df_5min_idx.values()
        for dk in sym_idx.keys()
    })
    all_date_keys = [dk for dk in all_date_keys
                     if start_ep <= dk * SECONDS_IN_ONE_DAY <= end_ep]
    print(f"  Trading days in range: {len(all_date_keys)}")

    margin = float(config["initial_capital"])
    equity = [(start_ep, margin)]
    trades = []
    days_since_refit = refit_every  # force refit at first day
    daily_pnl_log = []

    for di, date_key in enumerate(all_date_keys):
        day_epoch = date_key * SECONDS_IN_ONE_DAY

        # ── Quarterly β refit ────────────────────────────────────────
        if days_since_refit >= refit_every:
            days_since_refit = 0
            for pi, ps in enumerate(pair_states):
                # Get prior beta_window daily closes for both syms
                y_dc = daily_closes.get(ps.y_sym, {})
                x_dc = daily_closes.get(ps.x_sym, {})
                # Aligned daily closes from earlier date_keys
                aligned = [(dk, y_dc[dk], x_dc[dk])
                           for dk in sorted(y_dc.keys())
                           if dk < date_key and dk in x_dc][-beta_window:]
                if len(aligned) >= 60:
                    y_arr = np.array([a[1] for a in aligned], dtype=float)
                    x_arr = np.array([a[2] for a in aligned], dtype=float)
                    try:
                        a, b = refit_beta(y_arr, x_arr)
                        ps.alpha, ps.beta = a, b
                    except Exception:
                        pass
        days_since_refit += 1

        # ── Update daily spread history (for z) ──────────────────────
        for pi, ps in enumerate(pair_states):
            y_dc = daily_closes.get(ps.y_sym, {})
            x_dc = daily_closes.get(ps.x_sym, {})
            # Yesterday's close for spread
            prior_dks = [dk for dk in sorted(y_dc.keys())
                         if dk < date_key and dk in x_dc]
            if prior_dks:
                last_dk = prior_dks[-1]
                if not spread_history[pi] or spread_history[pi][-1][0] != last_dk:
                    spread = (np.log(y_dc[last_dk])
                              - ps.beta * np.log(x_dc[last_dk]))
                    spread_history[pi].append((last_dk, spread))
                    if len(spread_history[pi]) > z_window * 2:
                        spread_history[pi] = spread_history[pi][-z_window * 2:]

        # ── Walk through 5-min bars on this day ──────────────────────
        # Build list of (bar_minute, per-symbol price dict)
        # Collect all bars for all pairs' symbols on this day
        all_syms_today = set()
        for ps in pair_states:
            if ps.y_sym in df_5min_idx and date_key in df_5min_idx[ps.y_sym]:
                all_syms_today.add(ps.y_sym)
            if ps.x_sym in df_5min_idx and date_key in df_5min_idx[ps.x_sym]:
                all_syms_today.add(ps.x_sym)

        # bar_minute → {sym: bar dict}
        bars_by_minute: dict[int, dict[str, dict]] = {}
        for sym in all_syms_today:
            sym_day_bars = df_5min_idx[sym][date_key]
            for bar in sym_day_bars:
                bars_by_minute.setdefault(bar["bar5_minute"], {})[sym] = bar

        if not bars_by_minute:
            continue

        sorted_minutes = sorted(bars_by_minute.keys())

        # Rolling daily-stat per pair (mean, std) computed once at day start
        pair_daily_stats = {}
        for pi, ps in enumerate(pair_states):
            hist = [s for (dk, s) in spread_history[pi][-z_window:]]
            if len(hist) >= 20:
                pair_daily_stats[pi] = (
                    float(np.mean(hist)),
                    float(np.std(hist, ddof=1) or 1.0),
                )
            else:
                pair_daily_stats[pi] = None  # not enough history

        # Track per-pair: did we open today already? (one entry/pair/day)
        opened_today = set()

        for minute in sorted_minutes:
            bars = bars_by_minute[minute]

            # ── EOD square-off ───────────────────────────────────────
            if minute >= eod:
                for pi, ps in enumerate(pair_states):
                    if ps.in_position and ps.y_sym in bars and ps.x_sym in bars:
                        rec, pnl = close_position(
                            ps, bars[ps.y_sym]["close"], bars[ps.x_sym]["close"],
                            slip, "eod",
                        )
                        rec["exit_t"] = (date_key, minute)
                        rec["year"] = datetime.fromtimestamp(
                            day_epoch, tz=timezone.utc).year
                        margin += pnl
                        trades.append(rec)
                continue  # skip new entries past EOD threshold

            # ── Process exits for open positions ──────────────────────
            for pi, ps in enumerate(pair_states):
                if not ps.in_position:
                    continue
                if ps.y_sym not in bars or ps.x_sym not in bars:
                    continue
                stats = pair_daily_stats.get(pi)
                if stats is None:
                    continue
                mu, sd = stats
                y_p = bars[ps.y_sym]["close"]
                x_p = bars[ps.x_sym]["close"]
                spread = np.log(y_p) - ps.beta * np.log(x_p)
                z = (spread - mu) / sd if sd > 0 else 0
                # Target: z back inside |exit_target|
                exit_type = None
                if abs(z) < exit_target:
                    exit_type = "target"
                elif abs(z) > exit_stop:
                    exit_type = "stop"
                if exit_type:
                    rec, pnl = close_position(ps, y_p, x_p, slip, exit_type)
                    rec["exit_t"] = (date_key, minute)
                    rec["exit_z"] = round(float(z), 3)
                    rec["year"] = datetime.fromtimestamp(
                        day_epoch, tz=timezone.utc).year
                    margin += pnl
                    trades.append(rec)

            # ── Process new entries ──────────────────────────────────
            current_open = sum(1 for ps in pair_states if ps.in_position)
            if current_open >= max_pairs:
                continue

            entry_candidates = []
            for pi, ps in enumerate(pair_states):
                if ps.in_position or pi in opened_today:
                    continue
                if ps.y_sym not in bars or ps.x_sym not in bars:
                    continue
                stats = pair_daily_stats.get(pi)
                if stats is None:
                    continue
                mu, sd = stats
                y_p = bars[ps.y_sym]["close"]
                x_p = bars[ps.x_sym]["close"]
                spread = np.log(y_p) - ps.beta * np.log(x_p)
                z = (spread - mu) / sd if sd > 0 else 0
                if abs(z) > entry_thr:
                    entry_candidates.append((abs(z), pi, z, y_p, x_p))

            # Sort by |z| descending — strongest signal first
            entry_candidates.sort(reverse=True)
            slots_open = max_pairs - current_open
            for cand in entry_candidates[:slots_open]:
                _, pi, z_val, y_p, x_p = cand
                ps = pair_states[pi]
                side = "short_y" if z_val > 0 else "long_y"
                per_pair_capital = margin / max_pairs
                open_position(ps, side, y_p, x_p, per_pair_capital,
                              slip, date_key, minute, z_val)
                opened_today.add(pi)

        # Force-close any positions still open after EOD (data-gap safety)
        for pi, ps in enumerate(pair_states):
            if ps.in_position:
                # Use last available bar prices for both legs on this day
                y_last = None
                x_last = None
                for m in reversed(sorted_minutes):
                    if y_last is None and ps.y_sym in bars_by_minute[m]:
                        y_last = bars_by_minute[m][ps.y_sym]["close"]
                    if x_last is None and ps.x_sym in bars_by_minute[m]:
                        x_last = bars_by_minute[m][ps.x_sym]["close"]
                    if y_last is not None and x_last is not None:
                        break
                if y_last and x_last:
                    rec, pnl = close_position(ps, y_last, x_last, slip,
                                              "eod_force_close")
                    rec["exit_t"] = (date_key, sorted_minutes[-1])
                    rec["year"] = datetime.fromtimestamp(
                        day_epoch, tz=timezone.utc).year
                    margin += pnl
                    trades.append(rec)
                else:
                    # No valid prices — wipe position state, log warning
                    ps.in_position = False
                    ps.position_side = None

        # ── End-of-day equity snapshot ───────────────────────────────
        equity.append((day_epoch, margin))
        if (di + 1) % 100 == 0:
            day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
            in_pos = sum(1 for ps in pair_states if ps.in_position)
            print(f"    Day {di+1}/{len(all_date_keys)} ({day_str}): "
                  f"margin={margin:,.0f}, trades={len(trades)}, "
                  f"open_now={in_pos}")

    return trades, equity


# ── Metrics ──────────────────────────────────────────────────────────────

def metrics(equity, trades, initial_capital):
    if not equity or len(equity) < 2:
        return dict(cagr=0, mdd=0, sharpe=0, calmar=0, trades=0, win_rate=0,
                    final=initial_capital)
    initial = equity[0][1]
    final = equity[-1][1]
    days = (equity[-1][0] - equity[0][0]) / SECONDS_IN_ONE_DAY
    years = max(days / 365.25, 1e-9)
    cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 else -100
    peak, max_dd = initial, 0.0
    daily_rets = []
    prev = initial
    for _, eq in equity:
        peak = max(peak, eq)
        max_dd = min(max_dd, (eq - peak) / peak)
        if prev > 0:
            daily_rets.append((eq - prev) / prev)
        prev = eq
    if len(daily_rets) > 1:
        sd = statistics.stdev(daily_rets)
        sharpe = (statistics.mean(daily_rets) / sd) * (252 ** 0.5) if sd > 0 else 0
    else:
        sharpe = 0
    calmar = (cagr / abs(max_dd * 100)) if max_dd < 0 else 0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return dict(
        cagr=round(cagr, 2), mdd=round(max_dd * 100, 2),
        sharpe=round(sharpe, 3), calmar=round(calmar, 3),
        trades=len(trades),
        win_rate=round(wins / len(trades) * 100, 1) if trades else 0,
        final=round(final, 2),
    )


# ── Driver ───────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("PAIR TRADING — Phase 2 — Intraday Z-Score Spread")
    print("=" * 70)
    print(f"Config: {json.dumps({k: v for k, v in CONFIG.items() if k != 'slippage_bps_list'}, indent=2)}")

    print("\nLoading top pairs from discovery...")
    top_pairs = load_top_pairs(CONFIG["discovery_json"], CONFIG["n_pairs_to_use"])

    # Collect set of FMP symbols needed
    needed_syms = set()
    for p in top_pairs:
        needed_syms.add(p["y"] + ".NS")
        needed_syms.add(p["x"] + ".NS")
    print(f"\n  Symbols needed (FMP format): {len(needed_syms)}")

    print("\nLoading minute data for needed symbols...")
    df_minute = load_minute_data(symbols=needed_syms)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64)])

    print(f"\nResampling to {CONFIG['bar_aggregation_minutes']}-min bars...")
    t0 = time.time()
    df_5min = resample_to_5min(df_minute, CONFIG["bar_aggregation_minutes"])
    print(f"  5-min bars: {df_5min.height:,} rows ({time.time()-t0:.1f}s)")

    print("\nBuilding daily closes (from minute data, intraday-consistent)...")
    daily_df = build_daily_closes(df_5min)
    # Index: daily_closes[symbol][date_key] = close_price
    daily_closes: dict[str, dict[int, float]] = defaultdict(dict)
    for row in daily_df.to_dicts():
        daily_closes[row["symbol"]][row["date_key"]] = float(row["close"])
    print(f"  Daily closes for {len(daily_closes)} symbols")

    print("\nIndexing 5-min bars by symbol → date_key → list[bar]...")
    df_5min_idx: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    t0 = time.time()
    for row in df_5min.to_dicts():
        df_5min_idx[row["symbol"]][row["date_key"]].append(row)
    # Sort each list by bar5_minute (already sorted from query, but ensure)
    for sym in df_5min_idx:
        for dk in df_5min_idx[sym]:
            df_5min_idx[sym][dk].sort(key=lambda b: b["bar5_minute"])
    print(f"  Indexed in {time.time()-t0:.1f}s")

    # OOS boundary
    in_sample_end_ep = int(datetime.strptime(CONFIG["in_sample_end"], "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())

    all_results = {}
    for slip in CONFIG["slippage_bps_list"]:
        print(f"\n{'=' * 70}")
        print(f"Running sim @ slip={slip}bps")
        print(f"{'=' * 70}")
        cfg = {**CONFIG, "slippage_bps": slip}
        t_run = time.time()
        trades, equity = run_sim(df_5min_idx, daily_closes, top_pairs, cfg)
        elapsed = time.time() - t_run
        m = metrics(equity, trades, CONFIG["initial_capital"])

        # OOS-only metrics (2025): pair selection used 2022-2024, so 2025 is
        # the honest read. In-sample numbers are upward-biased by selection.
        oos_equity = [(t, v) for (t, v) in equity if t > in_sample_end_ep]
        # Re-anchor OOS equity to start at 1M for clean OOS metrics
        if oos_equity:
            anchor = next((v for (t, v) in equity if t > in_sample_end_ep), None)
            # Find the LAST IS-equity point before OOS start as the anchor base
            is_anchor = next(
                (v for (t, v) in reversed(equity) if t <= in_sample_end_ep),
                CONFIG["initial_capital"],
            )
            scale = CONFIG["initial_capital"] / is_anchor if is_anchor > 0 else 1
            oos_equity_norm = [(in_sample_end_ep, CONFIG["initial_capital"])]
            for (t, v) in oos_equity:
                oos_equity_norm.append((t, v * scale))
            oos_trades = [t for t in trades if t.get("year", 0) >= 2025]
            m_oos = metrics(oos_equity_norm, oos_trades, CONFIG["initial_capital"])
        else:
            m_oos = None

        # Per-year breakdown
        per_year = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
        for t in trades:
            y = t.get("year", 0)
            per_year[y]["pnl"] += t["pnl"]
            per_year[y]["trades"] += 1
            if t["pnl"] > 0:
                per_year[y]["wins"] += 1
        py = {y: {
            "trades": v["trades"],
            "pnl_pct_of_initial": round(v["pnl"] / CONFIG["initial_capital"] * 100, 2),
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
        } for y, v in sorted(per_year.items())}

        # Exit type distribution
        exits = defaultdict(int)
        for t in trades:
            exits[t["exit_type"]] += 1

        # Per-pair PnL breakdown
        per_pair = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
        for t in trades:
            p = t["pair"]
            per_pair[p]["pnl"] += t["pnl"]
            per_pair[p]["trades"] += 1
            if t["pnl"] > 0:
                per_pair[p]["wins"] += 1
        pp = sorted(
            [(name, v["pnl"] / CONFIG["initial_capital"] * 100, v["trades"],
              v["wins"] / v["trades"] * 100 if v["trades"] else 0)
             for name, v in per_pair.items()],
            key=lambda x: -x[1],
        )

        all_results[slip] = {
            "metrics": m, "metrics_oos": m_oos, "per_year": py,
            "exit_types": dict(exits), "per_pair": pp,
        }

        print(f"\n  RESULT slip={slip}bps (full 2022-2025 — IS-biased):")
        print(f"    CAGR={m['cagr']}% MDD={m['mdd']}% Sharpe={m['sharpe']} "
              f"Calmar={m['calmar']} Trades={m['trades']} WR={m['win_rate']}%")
        if m_oos:
            print(f"  RESULT slip={slip}bps (OOS 2025 only — honest read):")
            print(f"    CAGR={m_oos['cagr']}% MDD={m_oos['mdd']}% Sharpe={m_oos['sharpe']} "
                  f"Calmar={m_oos['calmar']} Trades={m_oos['trades']} WR={m_oos['win_rate']}%")
        print(f"  (sim {elapsed:.0f}s)")

    out_path = "/home/swas/backtester/intraday_pair_v0.json"
    with open(out_path, "w") as f:
        json.dump({k: v for k, v in all_results.items()}, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")

    # ── Summary tables ───────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("INTRADAY PAIR — SUMMARY (Full 2022-2025, IS-biased)")
    print(f"{'=' * 70}")
    print(f"\n{'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 70)
    for slip in CONFIG["slippage_bps_list"]:
        m = all_results[slip]["metrics"]
        print(f"{slip:>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% "
              f"{m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    print(f"\n{'=' * 70}")
    print("INTRADAY PAIR — OOS 2025 ONLY (honest, unbiased)")
    print(f"{'=' * 70}")
    print(f"\n{'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 70)
    for slip in CONFIG["slippage_bps_list"]:
        m = all_results[slip].get("metrics_oos")
        if m:
            print(f"{slip:>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% "
                  f"{m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    print("\nPer-year breakdown:")
    for slip in CONFIG["slippage_bps_list"]:
        print(f"\nslip={slip}bps:")
        for y, v in all_results[slip]["per_year"].items():
            print(f"  {y}: {v['trades']:>4d} trades, "
                  f"PnL={v['pnl_pct_of_initial']:>+6.2f}%, WR={v['win_rate']}%")
        print(f"  Exit types: {all_results[slip]['exit_types']}")

    # Top + bottom pairs by PnL contribution
    print("\nTop-10 / Bottom-5 pairs by PnL @ slip=0:")
    pp = all_results[0]["per_pair"]
    for name, pnl_pct, n_t, wr in pp[:10]:
        print(f"  + {name:<24} pnl={pnl_pct:>+6.2f}%  trades={n_t:>4d}  WR={wr:>5.1f}%")
    if len(pp) > 10:
        print("  ...")
        for name, pnl_pct, n_t, wr in pp[-5:]:
            print(f"  - {name:<24} pnl={pnl_pct:>+6.2f}%  trades={n_t:>4d}  WR={wr:>5.1f}%")

    # Decision gate
    m0 = all_results[0]["metrics"]
    m3 = all_results[3]["metrics"]
    print(f"\n{'=' * 70}")
    print("DECISION GATE")
    print(f"{'=' * 70}")
    cagr0, mdd0 = m0["cagr"], abs(m0["mdd"])
    cagr3, mdd3 = m3["cagr"], abs(m3["mdd"])
    print(f"  Aspirational (10% CAGR / 2% MDD): "
          f"slip0 CAGR={cagr0}% / MDD={mdd0}%  | slip3 CAGR={cagr3}% / MDD={mdd3}%")
    if cagr3 > 10 and mdd3 < 2:
        print("  ✓ ASPIRATIONAL TARGET HIT (10/2 at 3bps).")
    elif cagr3 > 5 and mdd3 < 5:
        print("  ✓ DEPLOYABLE (Calmar > 1, after costs). Proceed to walk-forward / OOS analysis.")
    elif cagr0 > 0:
        print(f"  ~ Positive at 0bps but {'destroyed by costs' if cagr3 <= 0 else 'below deployable bar'}. "
              f"Tune entry/exit thresholds, expand pair count, or pivot.")
    else:
        print("  ✗ Negative at 0bps. Cointegration found in-sample but mean reversion "
              "doesn't pay intraday. Pivot to multi-day pair holding or different strategy.")


if __name__ == "__main__":
    main()

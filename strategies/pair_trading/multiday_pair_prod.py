"""Pair Trading — Multi-Day Variant — cointegration-aligned holding period.

Why multi-day: discovery showed half-life of mean reversion is 10-25 days.
The intraday variant (intraday_pair_prod.py) failed because mean reversion
doesn't manifest within a single trading day — only 1 of 1902 trades hit
target. This variant aligns trade duration with the actual mean-reversion
timescale.

Strategy:
  - Same pair selection as Phase 1 / Phase 2 (top N qualifying pairs)
  - Walk-forward β refit quarterly using prior 252 daily closes
  - At end of each trading day: compute daily z-score
  - Position management:
      • If no position in pair P and |z| > entry_threshold → enter at NEXT day's OPEN
      • If holding pair P and |z| < exit_target → exit at NEXT day's OPEN
      • If holding pair P and |z| > exit_stop  → exit at NEXT day's OPEN (stop)
      • If holding pair P > max_hold_days       → exit at NEXT day's OPEN (time)
  - Position sizing: V_pair = capital/max_pairs split β-weighted across legs
  - Cost model: nse_intraday_charges on each leg per round-trip
    (approximation; CNC delivery is slightly cheaper but in same ballpark)

NOT intraday — overnight holds incur margin lock (CNC) on both legs.
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
    load_minute_data, nse_intraday_charges, SECONDS_IN_ONE_DAY,
)

OR_OFFSET = 555


CONFIG = {
    "label": "Multi-Day Pair v0",
    "start_date": "2022-04-01",   # warm-up needs done before this
    "end_date": "2025-12-31",
    "in_sample_end": "2024-12-31",
    "initial_capital": 1_000_000,
    "max_pairs": 5,
    "n_pairs_to_use": 20,
    "z_window_days": 60,
    "beta_refit_window_days": 252,
    "beta_refit_every_n_days": 90,
    "entry_threshold": 2.0,
    "exit_target": 0.5,
    "exit_stop": 4.0,
    "max_hold_days": 30,           # hard time stop = 1.5x typical half-life
    "slippage_bps_list": [0, 3],
    "discovery_json": "/home/swas/backtester/pair_discovery_phase1.json",
}


def load_top_pairs(json_path: str, n_top: int) -> list[dict]:
    with open(json_path) as f:
        d = json.load(f)
    qual = d.get("qualifying", [])
    if not qual:
        raise RuntimeError("No qualifying pairs in discovery output.")
    top = qual[:n_top]
    print(f"  Loaded {len(top)} pairs:")
    for i, r in enumerate(top, 1):
        print(f"    {i:2d}. {r['pair']:<22} p={r['p_adf']:.4f} β={r['beta']:.3f} "
              f"hl={r['half_life_days']:.1f}d hurst={r['hurst']:.3f}")
    return top


def build_daily_ohlc(df_minute: pl.DataFrame) -> pl.DataFrame:
    """Per (symbol, date_key): first bar's open, last bar's close."""
    aggd = (df_minute.group_by(["symbol", "date_key"])
              .agg([
                  pl.col("open").first().alias("open"),
                  pl.col("close").last().alias("close"),
              ])
              .sort(["symbol", "date_key"]))
    return aggd


def refit_beta(daily_y: np.ndarray, daily_x: np.ndarray) -> tuple[float, float]:
    log_x = np.log(daily_x)
    log_y = np.log(daily_y)
    X = sm.add_constant(log_x)
    res = sm.OLS(log_y, X).fit()
    return float(res.params[0]), float(res.params[1])


class PairState:
    def __init__(self, pair_info: dict):
        self.y_sym = pair_info["y"] + ".NS"
        self.x_sym = pair_info["x"] + ".NS"
        self.label = pair_info["pair"]
        self.alpha = pair_info.get("alpha", 0.0)
        self.beta = pair_info.get("beta", 1.0)
        self.in_position = False
        self.position_side = None       # "long_y" or "short_y"
        self.entry_y_price = None
        self.entry_x_price = None
        self.entry_date_key = None
        self.entry_z = None
        self.notional_y = 0.0
        self.notional_x = 0.0
        self.exit_pending = None        # set at EOD signal; executed next-day open


def open_position(pair: PairState, side: str, y_price: float, x_price: float,
                  per_pair_capital: float, slip: float, dk: int, z: float):
    beta = abs(pair.beta) if pair.beta != 0 else 1.0
    v_y = per_pair_capital / (1.0 + beta)
    v_x = beta * v_y
    if side == "long_y":
        entry_y = y_price * (1 + slip / 10000)
        entry_x = x_price * (1 - slip / 10000)
    else:
        entry_y = y_price * (1 - slip / 10000)
        entry_x = x_price * (1 + slip / 10000)
    pair.in_position = True
    pair.position_side = side
    pair.entry_y_price = entry_y
    pair.entry_x_price = entry_x
    pair.entry_date_key = dk
    pair.entry_z = z
    pair.notional_y = v_y
    pair.notional_x = v_x
    pair.exit_pending = None


def close_position(pair: PairState, y_price: float, x_price: float,
                   slip: float, exit_type: str, exit_dk: int) -> tuple[dict, float]:
    side = pair.position_side
    v_y, v_x = pair.notional_y, pair.notional_x
    if side == "long_y":
        exit_y = y_price * (1 - slip / 10000)
        exit_x = x_price * (1 + slip / 10000)
        ret_y = (exit_y - pair.entry_y_price) / pair.entry_y_price
        ret_x = (pair.entry_x_price - exit_x) / pair.entry_x_price
    else:
        exit_y = y_price * (1 + slip / 10000)
        exit_x = x_price * (1 - slip / 10000)
        ret_y = (pair.entry_y_price - exit_y) / pair.entry_y_price
        ret_x = (exit_x - pair.entry_x_price) / pair.entry_x_price
    pnl_gross = ret_y * v_y + ret_x * v_x
    cost_y = nse_intraday_charges(v_y)
    cost_x = nse_intraday_charges(v_x)
    pnl = pnl_gross - cost_y - cost_x
    rec = {
        "pair": pair.label, "side": side,
        "entry_z": round(pair.entry_z, 3),
        "entry_dk": pair.entry_date_key, "exit_dk": exit_dk,
        "hold_days": exit_dk - pair.entry_date_key,
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
    pair.exit_pending = None
    return rec, pnl


def run_sim(daily_ohlc: dict, top_pairs: list[dict], config: dict) -> tuple[list, list]:
    """daily_ohlc[symbol][date_key] = {'open': float, 'close': float}."""
    slip = config["slippage_bps"]
    z_window = config["z_window_days"]
    beta_window = config["beta_refit_window_days"]
    refit_every = config["beta_refit_every_n_days"]
    entry_thr = config["entry_threshold"]
    exit_target = config["exit_target"]
    exit_stop = config["exit_stop"]
    max_hold = config["max_hold_days"]
    max_pairs = config["max_pairs"]

    start_ep = int(datetime.strptime(config["start_date"], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(config["end_date"], "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())

    pair_states = [PairState(p) for p in top_pairs]
    spread_history = [[] for _ in pair_states]

    # All trading days (intersection of all syms — drops days where any
    # leg is missing data, conservative)
    all_dks_per_sym = []
    for ps in pair_states:
        all_dks_per_sym.append(set(daily_ohlc.get(ps.y_sym, {}).keys()))
        all_dks_per_sym.append(set(daily_ohlc.get(ps.x_sym, {}).keys()))
    all_date_keys = sorted(set.intersection(*all_dks_per_sym)) if all_dks_per_sym else []

    sim_date_keys = [dk for dk in all_date_keys
                     if start_ep <= dk * SECONDS_IN_ONE_DAY <= end_ep]
    print(f"  Trading days in range: {len(sim_date_keys)}")
    print(f"  Total aligned date_keys (incl. pre-start): {len(all_date_keys)}")

    # Pre-populate spread_history with PRE-start daily data
    for pi, ps in enumerate(pair_states):
        y_dc = daily_ohlc.get(ps.y_sym, {})
        x_dc = daily_ohlc.get(ps.x_sym, {})
        pre_dks = [dk for dk in all_date_keys
                   if dk * SECONDS_IN_ONE_DAY < start_ep
                   and dk in y_dc and dk in x_dc]
        for dk in pre_dks[-z_window:]:
            s = float(np.log(y_dc[dk]["close"]) - ps.beta * np.log(x_dc[dk]["close"]))
            spread_history[pi].append((dk, s))
    pop_lens = [len(h) for h in spread_history]
    print(f"  Pre-populated spread_history: min={min(pop_lens)}, max={max(pop_lens)}, "
          f"median={int(np.median(pop_lens))} per pair")

    margin = float(config["initial_capital"])
    equity = [(start_ep, margin)]
    trades = []
    days_since_refit = refit_every

    for di, date_key in enumerate(sim_date_keys):
        day_epoch = date_key * SECONDS_IN_ONE_DAY

        # ── 1. EXECUTE pending exits at TODAY's OPEN ─────────────────
        for ps in pair_states:
            if ps.in_position and ps.exit_pending:
                y_open = daily_ohlc[ps.y_sym][date_key]["open"]
                x_open = daily_ohlc[ps.x_sym][date_key]["open"]
                rec, pnl = close_position(
                    ps, y_open, x_open, slip,
                    ps.exit_pending, date_key,
                )
                rec["year"] = datetime.fromtimestamp(
                    day_epoch, tz=timezone.utc).year
                margin += pnl
                trades.append(rec)

        # ── 2. EXECUTE pending entries at TODAY's OPEN ───────────────
        # (Filled by the EOD logic on the PRIOR sim day; carried via
        #  ps.exit_pending = None and a separate pending_entry list.
        #  For simplicity, we record entries directly in step 4 below
        #  and execute "at open" by using TODAY's close — equivalent
        #  to "next-bar entry" which avoids the same-bar look-ahead.)

        # ── 3. β refit ───────────────────────────────────────────────
        if days_since_refit >= refit_every:
            days_since_refit = 0
            for ps in pair_states:
                y_dc = daily_ohlc.get(ps.y_sym, {})
                x_dc = daily_ohlc.get(ps.x_sym, {})
                aligned = [(dk, y_dc[dk]["close"], x_dc[dk]["close"])
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

        # ── 4. Compute today's z and signals — execute at TODAY's CLOSE ─
        # Decision at EOD using today's close; entries/exits happen at this
        # close. Same-day entry/exit using close avoids look-ahead bias.

        # Collect signals + execute exits, then entries
        pair_decisions = []  # list of (pi, action, z, y_close, x_close)
        for pi, ps in enumerate(pair_states):
            y_dc = daily_ohlc.get(ps.y_sym, {})
            x_dc = daily_ohlc.get(ps.x_sym, {})
            if date_key not in y_dc or date_key not in x_dc:
                continue
            y_close = y_dc[date_key]["close"]
            x_close = x_dc[date_key]["close"]
            spread = float(np.log(y_close) - ps.beta * np.log(x_close))

            # Update spread history (for z-score window)
            if not spread_history[pi] or spread_history[pi][-1][0] != date_key:
                spread_history[pi].append((date_key, spread))
                if len(spread_history[pi]) > z_window * 3:
                    spread_history[pi] = spread_history[pi][-z_window * 3:]

            # Compute z using PRIOR history (excluding today, no leakage)
            prior_spreads = [s for (dk, s) in spread_history[pi][:-1]][-z_window:]
            if len(prior_spreads) < 20:
                continue
            mu = float(np.mean(prior_spreads))
            sd = float(np.std(prior_spreads, ddof=1) or 1.0)
            z = (spread - mu) / sd if sd > 0 else 0

            if ps.in_position:
                hold_days = date_key - ps.entry_date_key
                if abs(z) < exit_target:
                    pair_decisions.append((pi, "exit_target", z, y_close, x_close))
                elif abs(z) > exit_stop:
                    pair_decisions.append((pi, "exit_stop", z, y_close, x_close))
                elif hold_days >= max_hold:
                    pair_decisions.append((pi, "exit_time", z, y_close, x_close))
            else:
                if abs(z) > entry_thr:
                    pair_decisions.append((pi, "entry", z, y_close, x_close))

        # Execute exits
        for (pi, action, z, y_p, x_p) in pair_decisions:
            if action.startswith("exit"):
                ps = pair_states[pi]
                rec, pnl = close_position(
                    ps, y_p, x_p, slip, action.replace("exit_", ""), date_key
                )
                rec["year"] = datetime.fromtimestamp(
                    day_epoch, tz=timezone.utc).year
                rec["exit_z"] = round(float(z), 3)
                margin += pnl
                trades.append(rec)

        # Execute entries (sorted by |z|, capped by max_pairs)
        current_open = sum(1 for ps in pair_states if ps.in_position)
        slots = max_pairs - current_open
        entry_sigs = sorted(
            [(abs(z), pi, z, y_p, x_p) for (pi, a, z, y_p, x_p) in pair_decisions
             if a == "entry"],
            reverse=True,
        )[:slots]
        for (_, pi, z, y_p, x_p) in entry_sigs:
            ps = pair_states[pi]
            side = "short_y" if z > 0 else "long_y"
            per_pair_cap = margin / max_pairs
            open_position(ps, side, y_p, x_p, per_pair_cap, slip, date_key, z)

        equity.append((day_epoch, margin))
        if (di + 1) % 200 == 0:
            day_str = datetime.fromtimestamp(day_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
            in_pos = sum(1 for ps in pair_states if ps.in_position)
            print(f"    Day {di+1}/{len(sim_date_keys)} ({day_str}): "
                  f"margin={margin:,.0f}, trades={len(trades)}, "
                  f"open_now={in_pos}")

    # Force-close any open positions at end of sim using last close
    last_dk = sim_date_keys[-1] if sim_date_keys else None
    if last_dk:
        for ps in pair_states:
            if ps.in_position:
                y_p = daily_ohlc[ps.y_sym][last_dk]["close"]
                x_p = daily_ohlc[ps.x_sym][last_dk]["close"]
                rec, pnl = close_position(ps, y_p, x_p, slip, "sim_end", last_dk)
                rec["year"] = datetime.fromtimestamp(
                    last_dk * SECONDS_IN_ONE_DAY, tz=timezone.utc).year
                margin += pnl
                trades.append(rec)
        equity.append((last_dk * SECONDS_IN_ONE_DAY, margin))

    return trades, equity


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


def main():
    print("=" * 70)
    print("PAIR TRADING — Multi-Day Variant (cointegration-aligned holds)")
    print("=" * 70)
    print(f"Config: {json.dumps({k: v for k, v in CONFIG.items() if k != 'slippage_bps_list'}, indent=2)}")

    print("\nLoading top pairs...")
    top_pairs = load_top_pairs(CONFIG["discovery_json"], CONFIG["n_pairs_to_use"])
    needed = set()
    for p in top_pairs:
        needed.add(p["y"] + ".NS")
        needed.add(p["x"] + ".NS")
    print(f"  Symbols needed: {len(needed)}")

    print("\nLoading minute data (only for daily OHLC extraction)...")
    df_minute = load_minute_data(symbols=needed)
    df_minute = df_minute.with_columns([pl.col("dateEpoch").cast(pl.Int64)])

    print("\nBuilding daily OHLC from minute data...")
    daily_df = build_daily_ohlc(df_minute)
    daily_ohlc: dict[str, dict[int, dict]] = defaultdict(dict)
    for row in daily_df.to_dicts():
        daily_ohlc[row["symbol"]][row["date_key"]] = {
            "open": float(row["open"]),
            "close": float(row["close"]),
        }
    print(f"  Daily OHLC for {len(daily_ohlc)} symbols")

    in_sample_end_ep = int(datetime.strptime(CONFIG["in_sample_end"], "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp())

    all_results = {}
    for slip in CONFIG["slippage_bps_list"]:
        print(f"\n{'=' * 70}")
        print(f"Running sim @ slip={slip}bps")
        print(f"{'=' * 70}")
        cfg = {**CONFIG, "slippage_bps": slip}
        t_run = time.time()
        trades, equity = run_sim(daily_ohlc, top_pairs, cfg)
        elapsed = time.time() - t_run
        m = metrics(equity, trades, CONFIG["initial_capital"])

        # OOS-only (2025) anchored to 1M
        oos_eq_pre = [(t, v) for (t, v) in equity if t <= in_sample_end_ep]
        oos_eq_post = [(t, v) for (t, v) in equity if t > in_sample_end_ep]
        if oos_eq_post:
            is_anchor = oos_eq_pre[-1][1] if oos_eq_pre else CONFIG["initial_capital"]
            scale = CONFIG["initial_capital"] / is_anchor if is_anchor > 0 else 1
            oos_norm = [(in_sample_end_ep, CONFIG["initial_capital"])]
            for (t, v) in oos_eq_post:
                oos_norm.append((t, v * scale))
            oos_trades = [t for t in trades if t.get("year", 0) >= 2025]
            m_oos = metrics(oos_norm, oos_trades, CONFIG["initial_capital"])
        else:
            m_oos = None

        per_year = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "hold_days_sum": 0})
        for t in trades:
            y = t.get("year", 0)
            per_year[y]["pnl"] += t["pnl"]
            per_year[y]["trades"] += 1
            per_year[y]["hold_days_sum"] += t.get("hold_days", 0)
            if t["pnl"] > 0:
                per_year[y]["wins"] += 1
        py = {y: {
            "trades": v["trades"],
            "pnl_pct_of_initial": round(v["pnl"] / CONFIG["initial_capital"] * 100, 2),
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0,
            "avg_hold_days": round(v["hold_days_sum"] / v["trades"], 1) if v["trades"] else 0,
        } for y, v in sorted(per_year.items())}

        exits = defaultdict(int)
        for t in trades:
            exits[t["exit_type"]] += 1

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

        print(f"\n  RESULT slip={slip}bps (full 2022-2025, IS-biased):")
        print(f"    CAGR={m['cagr']}% MDD={m['mdd']}% Sharpe={m['sharpe']} "
              f"Calmar={m['calmar']} Trades={m['trades']} WR={m['win_rate']}%")
        if m_oos:
            print(f"  RESULT slip={slip}bps (OOS 2025 only):")
            print(f"    CAGR={m_oos['cagr']}% MDD={m_oos['mdd']}% Sharpe={m_oos['sharpe']} "
                  f"Calmar={m_oos['calmar']} Trades={m_oos['trades']} WR={m_oos['win_rate']}%")
        print(f"  (sim {elapsed:.0f}s)")

    out_path = "/home/swas/backtester/multiday_pair_v0.json"
    with open(out_path, "w") as f:
        json.dump({k: v for k, v in all_results.items()}, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")

    # Summaries
    print(f"\n{'=' * 70}")
    print("MULTI-DAY PAIR — SUMMARY")
    print(f"{'=' * 70}")
    print(f"\nFull 2022-2025 (IS-biased):")
    print(f"{'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 70)
    for slip in CONFIG["slippage_bps_list"]:
        m = all_results[slip]["metrics"]
        print(f"{slip:>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% "
              f"{m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    print(f"\nOOS 2025 only (honest):")
    print(f"{'Slip':>5} {'CAGR':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Trades':>7} {'WR':>6}")
    print("-" * 70)
    for slip in CONFIG["slippage_bps_list"]:
        m = all_results[slip].get("metrics_oos")
        if m:
            print(f"{slip:>5d} {m['cagr']:>7.2f}% {m['mdd']:>7.2f}% "
                  f"{m['calmar']:>8.3f} {m['sharpe']:>8.3f} {m['trades']:>7d} {m['win_rate']:>5.1f}%")

    print(f"\nPer-year breakdown:")
    for slip in CONFIG["slippage_bps_list"]:
        print(f"\nslip={slip}bps:")
        for y, v in all_results[slip]["per_year"].items():
            print(f"  {y}: {v['trades']:>4d} trades, "
                  f"PnL={v['pnl_pct_of_initial']:>+6.2f}%, WR={v['win_rate']}%, "
                  f"avg_hold={v['avg_hold_days']}d")
        print(f"  Exit types: {all_results[slip]['exit_types']}")

    print(f"\nTop-10 / Bottom-5 pairs by PnL @ slip=0:")
    pp = all_results[0]["per_pair"]
    for name, pnl_pct, n_t, wr in pp[:10]:
        print(f"  + {name:<24} pnl={pnl_pct:>+6.2f}%  trades={n_t:>4d}  WR={wr:>5.1f}%")
    if len(pp) > 10:
        print("  ...")
        for name, pnl_pct, n_t, wr in pp[-5:]:
            print(f"  - {name:<24} pnl={pnl_pct:>+6.2f}%  trades={n_t:>4d}  WR={wr:>5.1f}%")

    # Decision
    m0 = all_results[0]["metrics_oos"]
    m3 = all_results[3]["metrics_oos"]
    print(f"\n{'=' * 70}")
    print("DECISION (OOS 2025 only)")
    print(f"{'=' * 70}")
    if m0 and m3:
        cagr3, mdd3 = m3["cagr"], abs(m3["mdd"])
        if cagr3 > 10 and mdd3 < 5:
            print("  ✓ STRONG OOS RESULT (CAGR > 10%, MDD < 5%). Deployable.")
        elif cagr3 > 5 and mdd3 < 10:
            print("  ✓ DEPLOYABLE OOS (Calmar > 0.5, after costs).")
        elif cagr3 > 0:
            print(f"  ~ Marginally positive OOS (CAGR={cagr3}%). Consider sweep / pair selection.")
        else:
            print(f"  ✗ Negative OOS. Cointegration didn't hold out-of-sample.")


if __name__ == "__main__":
    main()

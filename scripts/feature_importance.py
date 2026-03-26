#!/usr/bin/env python3
"""Feature importance analysis for momentum-dip champion strategy.

Reconstructs all filtered entries from the champion pipeline (not just
capital-allocated ones), simulates each independently with TSL exit logic,
then analyzes which features predict winner vs loser trades.

This answers: "what should we filter on next to improve the strategy?"

Runs on CR compute. Requires scikit-learn (graceful fallback for sections 1-3).
"""

import sys
import os
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

import pandas as pd
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark, fetch_sector_map,
    compute_quality_universe, compute_momentum_universe,
    compute_dip_entries, compute_regime_epochs,
    compute_volume_ratios, compute_realized_vol,
    compute_rsi_series, _find_epoch_idx,
    CetaResearch,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, get_fundamental_at, filter_entries_by_fundamentals,
)
from scripts.momentum_dip_de_positions import intersect_universes

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.inspection import permutation_importance
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

# ── Champion Config (fixed) ──────────────────────────────────────────────────

MOMENTUM_LOOKBACK = 63
MOMENTUM_PERCENTILE = 0.30
DIP_THRESHOLD_PCT = 5
CONSECUTIVE_YEARS = 2
ROE_THRESHOLD = 15
DE_THRESHOLD = 1.0
PE_THRESHOLD = 25
REGIME_SMA = 200
TSL_PCT = 10
MAX_HOLD_DAYS = 504
PEAK_LOOKBACK = 63
FILING_LAG_DAYS = 45

EXCHANGE = "NSE"
BENCHMARK = "NIFTYBEES"
START_EPOCH = 1262304000   # 2010-01-01
END_EPOCH = 1773878400     # 2026-03-17

NUMERIC_FEATURES = [
    "dip_pct", "momentum_score", "momentum_percentile",
    "volume_ratio", "realized_vol_60d", "rsi_14",
    "roe", "de_ratio", "pe_ratio",
    "gross_profit_ratio", "revenue_growth", "current_ratio",
    "regime_bull",
]


# ── Extended Fundamentals ────────────────────────────────────────────────────

def fetch_extended_fundamentals(cr, exchange):
    """Fetch gross profitability, revenue, current ratio from FMP.

    Returns dict[symbol, list[{epoch, gross_profit_ratio, revenue, current_ratio}]]
    sorted by epoch, same shape as fetch_fundamentals() for get_fundamental_at().
    """
    suffix_filter = "i.symbol LIKE '%.NS'" if exchange == "NSE" else "1=1"
    sql = f"""
    SELECT i.symbol, CAST(i.dateEpoch AS BIGINT) AS dateEpoch,
           i.grossProfit, i.revenue,
           b.totalAssets, b.totalCurrentAssets, b.totalCurrentLiabilities
    FROM fmp.income_statement i
    JOIN fmp.balance_sheet b ON i.symbol = b.symbol
        AND i.fiscalYear = b.fiscalYear AND i.period = b.period
    WHERE {suffix_filter} AND i.period = 'FY'
      AND b.totalAssets IS NOT NULL AND b.totalAssets > 0
    ORDER BY i.symbol, i.dateEpoch
    """
    print("  Fetching extended fundamentals (income_statement + balance_sheet)...")
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  WARNING: No extended fundamental data fetched")
        return {}

    raw = {}  # symbol -> list of records (unsorted)
    for r in results:
        sym = r["symbol"]
        if exchange == "NSE" and sym.endswith(".NS"):
            sym = sym[:-3]

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        gp = r.get("grossProfit")
        ta = r.get("totalAssets")
        rev = r.get("revenue")
        tca = r.get("totalCurrentAssets")
        tcl = r.get("totalCurrentLiabilities")

        gp_ratio = (float(gp) / float(ta)) if (gp is not None and ta and float(ta) > 0) else None
        curr_ratio = (float(tca) / float(tcl)) if (tca is not None and tcl and float(tcl) > 0) else None

        if sym not in raw:
            raw[sym] = []
        raw[sym].append({
            "epoch": epoch,
            "gross_profit_ratio": gp_ratio,
            "revenue": float(rev) if rev is not None else None,
            "current_ratio": curr_ratio,
        })

    # Sort by epoch, compute revenue growth from consecutive FY rows
    ext = {}
    for sym, records in raw.items():
        records.sort(key=lambda x: x["epoch"])
        for i, rec in enumerate(records):
            if i > 0 and records[i - 1]["revenue"] and records[i - 1]["revenue"] > 0 and rec["revenue"]:
                rec["revenue_growth"] = (rec["revenue"] - records[i - 1]["revenue"]) / abs(records[i - 1]["revenue"])
            else:
                rec["revenue_growth"] = None
        ext[sym] = records

    print(f"  Extended fundamentals: {len(ext)} symbols, "
          f"{sum(len(v) for v in ext.values())} data points")
    return ext


# ── Momentum Score Helpers ───────────────────────────────────────────────────

def compute_momentum_score_at(price_data, symbol, epoch, lookback=63):
    """Trailing return for one symbol at one epoch. Returns float or None."""
    bars = price_data.get(symbol)
    if not bars:
        return None
    idx = _find_epoch_idx(bars, epoch)
    if idx is None or idx < lookback:
        return None
    older_close = bars[idx - lookback]["close"]
    if older_close <= 0:
        return None
    return bars[idx]["close"] / older_close - 1.0


def compute_momentum_percentile_at(price_data, epoch, lookback=63):
    """Momentum percentile rank for ALL symbols at given epoch.

    Returns dict[symbol, float] where float is 0.0 (worst) to 1.0 (best).
    """
    scores = []
    for sym, bars in price_data.items():
        idx = _find_epoch_idx(bars, epoch)
        if idx is None or idx < lookback:
            continue
        older_close = bars[idx - lookback]["close"]
        if older_close <= 0:
            continue
        mom = bars[idx]["close"] / older_close - 1.0
        scores.append((sym, mom))

    if not scores:
        return {}
    scores.sort(key=lambda x: x[1])
    n = len(scores)
    return {sym: (rank + 1) / n for rank, (sym, _) in enumerate(scores)}


# ── RSI Helper ───────────────────────────────────────────────────────────────

def compute_rsi_at(price_data, symbol, epoch, period=14):
    """RSI at a specific epoch for a symbol. Returns float or None."""
    bars = price_data.get(symbol)
    if not bars:
        return None
    idx = _find_epoch_idx(bars, epoch)
    if idx is None or idx < period + 1:
        return None
    # Use a window with warmup
    start = max(0, idx - period - 50)
    closes = [b["close"] for b in bars[start:idx + 1]]
    rsi_values = compute_rsi_series(closes, period)
    return rsi_values[-1] if rsi_values else None


# ── Forward Simulation ───────────────────────────────────────────────────────

def simulate_entry_forward(bars, entry_idx, entry_price, peak_price,
                           tsl_pct=10, max_hold_bars=504):
    """Simulate TSL exit logic for a single entry. Returns outcome dict.

    Mirrors simulate_portfolio() exit logic (quality_dip_buy_lib.py:1005-1037):
    - Track trail_high from entry_price
    - Once close >= peak_price, set reached_peak = True
    - After reached_peak, if close <= trail_high * (1 - tsl/100), exit (TSL)
    - After max_hold_bars, force exit
    """
    trail_high = entry_price
    reached_peak = False
    max_dd = 0.0

    end_idx = min(entry_idx + max_hold_bars, len(bars) - 1)
    exit_idx = end_idx
    exit_reason = "max_hold"

    for i in range(entry_idx + 1, end_idx + 1):
        close = bars[i]["close"]
        if close <= 0:
            continue

        if close > trail_high:
            trail_high = close

        # Track max drawdown from entry
        dd = (entry_price - close) / entry_price if entry_price > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # Peak recovery
        if close >= peak_price:
            reached_peak = True

        # TSL exit (after peak recovery)
        if reached_peak and close <= trail_high * (1 - tsl_pct / 100.0):
            exit_idx = i
            exit_reason = "tsl"
            break

    exit_price = bars[exit_idx]["close"]
    pnl_pct = (exit_price / entry_price - 1.0) * 100 if entry_price > 0 else 0
    hold_days = (bars[exit_idx]["epoch"] - bars[entry_idx]["epoch"]) / 86400

    return {
        "pnl_pct": round(pnl_pct, 2),
        "exit_reason": exit_reason,
        "hold_days": round(hold_days, 1),
        "max_dd_pct": round(max_dd * 100, 2),
        "peak_recovered": reached_peak,
        "exit_price": round(exit_price, 4),
    }


# ── Feature Enrichment ───────────────────────────────────────────────────────

def build_enriched_trades(entries, price_data, fundamentals, ext_fundamentals,
                          volume_ratios, realized_vol, sector_map, regime_epochs,
                          percentile_cache):
    """Enrich all entries with features and forward-simulated outcomes."""
    trades = []
    skipped = 0

    for entry in entries:
        sym = entry["symbol"]
        bars = price_data.get(sym)
        if not bars:
            skipped += 1
            continue

        entry_idx = _find_epoch_idx(bars, entry["entry_epoch"])
        if entry_idx is None or entry_idx >= len(bars) - 20:
            skipped += 1
            continue

        # Forward simulation
        outcome = simulate_entry_forward(
            bars, entry_idx, entry["entry_price"], entry["peak_price"],
            tsl_pct=TSL_PCT, max_hold_bars=MAX_HOLD_DAYS)

        signal_epoch = entry["epoch"]

        # Technical features
        mom_score = compute_momentum_score_at(price_data, sym, signal_epoch, MOMENTUM_LOOKBACK)
        mom_pctile = (percentile_cache.get(signal_epoch) or {}).get(sym)
        vol_ratio = (volume_ratios.get(sym) or {}).get(signal_epoch)
        rv_60 = (realized_vol.get(sym) or {}).get(signal_epoch)
        rsi = compute_rsi_at(price_data, sym, signal_epoch, 14)

        # Fundamental features (with 45-day lag)
        fund = get_fundamental_at(fundamentals, sym, signal_epoch, lag_days=FILING_LAG_DAYS)
        roe = fund["roe"] if fund else None
        de = fund["de"] if fund else None
        pe = fund["pe"] if fund else None

        ext = get_fundamental_at(ext_fundamentals, sym, signal_epoch, lag_days=FILING_LAG_DAYS)
        gp_ratio = ext.get("gross_profit_ratio") if ext else None
        rev_growth = ext.get("revenue_growth") if ext else None
        curr_ratio = ext.get("current_ratio") if ext else None

        # Context
        sector = sector_map.get(sym, "Unknown")
        is_bull = 1 if (regime_epochs and signal_epoch in regime_epochs) else 0
        entry_year = datetime.fromtimestamp(signal_epoch, tz=timezone.utc).year

        trades.append({
            "symbol": sym,
            "signal_epoch": signal_epoch,
            "entry_epoch": entry["entry_epoch"],
            "entry_price": entry["entry_price"],
            "peak_price": entry["peak_price"],
            # Outcome
            "pnl_pct": outcome["pnl_pct"],
            "winner": 1 if outcome["pnl_pct"] > 0 else 0,
            "exit_reason": outcome["exit_reason"],
            "hold_days": outcome["hold_days"],
            "max_dd_pct": outcome["max_dd_pct"],
            "peak_recovered": outcome["peak_recovered"],
            # Technical
            "dip_pct": round(entry["dip_pct"] * 100, 2),  # convert to %
            "momentum_score": round(mom_score, 4) if mom_score is not None else None,
            "momentum_percentile": round(mom_pctile, 4) if mom_pctile is not None else None,
            "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "realized_vol_60d": round(rv_60, 5) if rv_60 is not None else None,
            "rsi_14": round(rsi, 1) if rsi is not None else None,
            # Fundamental
            "roe": round(roe, 2) if roe is not None else None,
            "de_ratio": round(de, 2) if de is not None else None,
            "pe_ratio": round(pe, 2) if pe is not None else None,
            "gross_profit_ratio": round(gp_ratio, 4) if gp_ratio is not None else None,
            "revenue_growth": round(rev_growth, 4) if rev_growth is not None else None,
            "current_ratio": round(curr_ratio, 2) if curr_ratio is not None else None,
            # Context
            "sector": sector,
            "regime_bull": is_bull,
            "entry_year": entry_year,
        })

    print(f"  Enriched {len(trades)} trades ({skipped} skipped)")
    return trades


# ── Analysis Functions ────────────────────────────────────────────────────────

def analyze_correlations(df, features):
    """Spearman rank correlation of each feature with pnl_pct."""
    print("\n" + "=" * 70)
    print("  SECTION 1: FEATURE CORRELATIONS WITH PNL%")
    print("=" * 70)

    correlations = []
    for feat in features:
        valid = df[[feat, "pnl_pct"]].dropna()
        if len(valid) < 20:
            correlations.append({"feature": feat, "spearman_rho": None, "n": len(valid)})
            continue
        rho = valid[feat].rank().corr(valid["pnl_pct"].rank())
        correlations.append({"feature": feat, "spearman_rho": round(rho, 4), "n": len(valid)})

    correlations.sort(key=lambda x: abs(x["spearman_rho"] or 0), reverse=True)
    print(f"\n  {'Feature':<22} {'Spearman rho':>12} {'N':>6}")
    print(f"  {'-' * 22} {'-' * 12} {'-' * 6}")
    for c in correlations:
        rho_str = f"{c['spearman_rho']:>+12.4f}" if c["spearman_rho"] is not None else "         N/A"
        print(f"  {c['feature']:<22} {rho_str} {c['n']:>6}")
    return correlations


def analyze_winner_loser(df, features):
    """Compare feature distributions between winners and losers."""
    print("\n" + "=" * 70)
    print("  SECTION 2: WINNER vs LOSER FEATURE COMPARISON")
    print("=" * 70)

    winners = df[df["winner"] == 1]
    losers = df[df["winner"] == 0]
    print(f"\n  Winners: {len(winners)} ({len(winners) / len(df) * 100:.1f}%)  "
          f"Losers: {len(losers)} ({len(losers) / len(df) * 100:.1f}%)")
    print(f"  Avg PnL: winners {winners['pnl_pct'].mean():+.1f}%, "
          f"losers {losers['pnl_pct'].mean():+.1f}%")

    rows = []
    print(f"\n  {'Feature':<22} {'W mean':>10} {'L mean':>10} {'Diff%':>8} {'W med':>10} {'L med':>10}")
    print(f"  {'-' * 22} {'-' * 10} {'-' * 10} {'-' * 8} {'-' * 10} {'-' * 10}")

    for feat in features:
        w_vals = winners[feat].dropna()
        l_vals = losers[feat].dropna()
        if len(w_vals) < 5 or len(l_vals) < 5:
            continue
        diff_pct = ((w_vals.mean() - l_vals.mean()) / abs(l_vals.mean()) * 100) if l_vals.mean() != 0 else 0
        row = {
            "feature": feat,
            "winner_mean": round(w_vals.mean(), 4),
            "loser_mean": round(l_vals.mean(), 4),
            "diff_pct": round(diff_pct, 1),
            "winner_median": round(w_vals.median(), 4),
            "loser_median": round(l_vals.median(), 4),
        }
        rows.append(row)
        print(f"  {feat:<22} {row['winner_mean']:>10.3f} {row['loser_mean']:>10.3f} "
              f"{row['diff_pct']:>+7.1f}% {row['winner_median']:>10.3f} {row['loser_median']:>10.3f}")
    return rows


def analyze_quartiles(df, features):
    """Bin each feature into quartiles, compute win rate and avg pnl per bin."""
    print("\n" + "=" * 70)
    print("  SECTION 3: QUARTILE ANALYSIS (win rate by feature quartile)")
    print("=" * 70)

    results = []
    for feat in features:
        valid = df[[feat, "pnl_pct", "winner"]].dropna()
        if len(valid) < 20:
            continue

        try:
            valid = valid.copy()
            valid["quartile"] = pd.qcut(valid[feat], q=4, labels=["Q1", "Q2", "Q3", "Q4"],
                                        duplicates="drop")
        except ValueError:
            continue

        print(f"\n  {feat}:")
        print(f"    {'Quartile':<10} {'Range':>25} {'N':>5} {'WinRate':>8} {'AvgPnl':>8}")

        feat_result = {"feature": feat, "quartiles": []}
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            subset = valid[valid["quartile"] == q]
            if len(subset) == 0:
                continue
            qr = {
                "quartile": q,
                "n": int(len(subset)),
                "win_rate": round(subset["winner"].mean() * 100, 1),
                "avg_pnl": round(subset["pnl_pct"].mean(), 1),
                "range": f"{subset[feat].min():.4f} - {subset[feat].max():.4f}",
            }
            feat_result["quartiles"].append(qr)
            print(f"    {q:<10} {qr['range']:>25} {qr['n']:>5} {qr['win_rate']:>7.1f}% {qr['avg_pnl']:>+7.1f}%")
        results.append(feat_result)
    return results


def analyze_random_forest(df, features):
    """Random forest feature importance + permutation importance."""
    if not HAS_SKLEARN:
        print("\n  [SKIPPED] scikit-learn not available. Install for RF analysis.")
        return None

    print("\n" + "=" * 70)
    print("  SECTION 4: RANDOM FOREST FEATURE IMPORTANCE")
    print("=" * 70)

    valid = df[features + ["winner"]].dropna()
    if len(valid) < 50:
        print(f"  Insufficient data ({len(valid)} rows with all features). Need >= 50.")
        return None

    X = valid[features].values
    y = valid["winner"].values
    print(f"\n  Training on {len(valid)} complete rows ({valid['winner'].sum()} winners, "
          f"{(1 - valid['winner']).sum():.0f} losers)")

    rf = RandomForestClassifier(n_estimators=200, max_depth=5, random_state=42, n_jobs=1)
    rf.fit(X, y)

    # Gini importance
    gini_imp = sorted(zip(features, rf.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Gini Importance (train accuracy: {rf.score(X, y) * 100:.1f}%):")
    print(f"  {'Feature':<22} {'Importance':>12}")
    print(f"  {'-' * 22} {'-' * 12}")
    for feat, imp in gini_imp:
        bar = "#" * int(imp * 50)
        print(f"  {feat:<22} {imp:>12.4f} {bar}")

    # Permutation importance
    perm = permutation_importance(rf, X, y, n_repeats=10, random_state=42, n_jobs=1)
    perm_imp = sorted(zip(features, perm.importances_mean, perm.importances_std),
                      key=lambda x: x[1], reverse=True)
    print(f"\n  Permutation Importance:")
    print(f"  {'Feature':<22} {'Mean':>10} {'Std':>10}")
    print(f"  {'-' * 22} {'-' * 10} {'-' * 10}")
    for feat, mean, std in perm_imp:
        print(f"  {feat:<22} {mean:>10.4f} {std:>10.4f}")

    return {
        "gini": [(f, round(float(i), 4)) for f, i in gini_imp],
        "permutation": [(f, round(float(m), 4), round(float(s), 4)) for f, m, s in perm_imp],
        "train_accuracy": round(rf.score(X, y) * 100, 1),
        "n_samples": len(valid),
    }


def analyze_exit_reasons(df):
    """Breakdown by exit reason."""
    print("\n" + "=" * 70)
    print("  SECTION 5: EXIT REASON BREAKDOWN")
    print("=" * 70)

    for reason in df["exit_reason"].unique():
        subset = df[df["exit_reason"] == reason]
        print(f"\n  {reason}: {len(subset)} trades ({len(subset) / len(df) * 100:.1f}%)")
        print(f"    Win rate: {subset['winner'].mean() * 100:.1f}%")
        print(f"    Avg PnL: {subset['pnl_pct'].mean():+.1f}%")
        print(f"    Avg hold days: {subset['hold_days'].mean():.0f}")


def analyze_sectors(df):
    """Sector breakdown."""
    print("\n" + "=" * 70)
    print("  SECTION 6: SECTOR BREAKDOWN")
    print("=" * 70)

    sector_stats = df.groupby("sector").agg(
        n=("pnl_pct", "count"),
        win_rate=("winner", "mean"),
        avg_pnl=("pnl_pct", "mean"),
        avg_dd=("max_dd_pct", "mean"),
    ).sort_values("n", ascending=False)

    print(f"\n  {'Sector':<28} {'N':>5} {'WinRate':>8} {'AvgPnl':>8} {'AvgDD':>8}")
    print(f"  {'-' * 28} {'-' * 5} {'-' * 8} {'-' * 8} {'-' * 8}")
    for sector, row in sector_stats.iterrows():
        if row["n"] >= 5:
            print(f"  {sector:<28} {row['n']:>5.0f} {row['win_rate'] * 100:>7.1f}% "
                  f"{row['avg_pnl']:>+7.1f}% {row['avg_dd']:>7.1f}%")


def print_recommendations(correlations, wl_comparison, rf_results):
    """Synthesize findings into actionable filter recommendations."""
    print("\n" + "=" * 70)
    print("  SECTION 7: ACTIONABLE RECOMMENDATIONS")
    print("=" * 70)

    # Gather signals from each method
    signals = {}  # feature -> list of (method, signal_strength)

    # From correlations
    for c in correlations:
        if c["spearman_rho"] is not None and abs(c["spearman_rho"]) >= 0.05:
            feat = c["feature"]
            if feat not in signals:
                signals[feat] = []
            signals[feat].append(("correlation", abs(c["spearman_rho"])))

    # From winner/loser comparison
    for wl in wl_comparison:
        if abs(wl["diff_pct"]) >= 5:
            feat = wl["feature"]
            if feat not in signals:
                signals[feat] = []
            signals[feat].append(("winner_loser_diff", abs(wl["diff_pct"]) / 100))

    # From RF permutation importance
    if rf_results and rf_results.get("permutation"):
        for feat, mean, std in rf_results["permutation"]:
            if mean > 0.005:
                if feat not in signals:
                    signals[feat] = []
                signals[feat].append(("rf_permutation", mean))

    # Rank by number of methods + combined strength
    ranked = []
    for feat, sigs in signals.items():
        ranked.append({
            "feature": feat,
            "n_methods": len(sigs),
            "combined_score": sum(s for _, s in sigs),
            "methods": ", ".join(f"{m}" for m, _ in sigs),
        })
    ranked.sort(key=lambda x: (x["n_methods"], x["combined_score"]), reverse=True)

    print(f"\n  Features ranked by signal strength across methods:")
    print(f"  {'Feature':<22} {'Methods':>3} {'Score':>8} {'Sources'}")
    print(f"  {'-' * 22} {'-' * 3} {'-' * 8} {'-' * 30}")
    for r in ranked:
        print(f"  {r['feature']:<22} {r['n_methods']:>3} {r['combined_score']:>8.4f} {r['methods']}")

    print("\n  NEXT STEPS:")
    if ranked:
        top = ranked[:3]
        for i, r in enumerate(top):
            action = ""
            if r["feature"] == "gross_profit_ratio":
                action = "Test grossProfit/totalAssets as replacement or supplement to ROE>15%"
            elif r["feature"] == "volume_ratio":
                action = "Add volume spike filter: volume > 1.5x or 2x 20d avg on dip day"
            elif r["feature"] == "realized_vol_60d":
                action = "Test low-vol pre-filter OR adaptive dip threshold (dip > k*vol)"
            elif r["feature"] == "revenue_growth":
                action = "Add revenue growth > 0% or > 5% as fundamental gate"
            elif r["feature"] == "current_ratio":
                action = "Add current ratio > 1.0 or > 1.5 as liquidity gate"
            elif r["feature"] == "momentum_score":
                action = "Test tighter momentum threshold (top 25%?) or momentum as weight"
            elif r["feature"] == "dip_pct":
                action = "Test deeper dip threshold (7%? 10%?) or adaptive per-stock threshold"
            elif r["feature"] == "de_ratio":
                action = "Test tighter D/E threshold (< 0.5? < 0.3?)"
            elif r["feature"] == "pe_ratio":
                action = "Test tighter P/E threshold (< 20? < 15?)"
            elif r["feature"] == "rsi_14":
                action = "Test RSI < 30 or RSI < 40 as oversold confirmation"
            elif r["feature"] == "roe":
                action = "Test higher ROE threshold (> 20%? > 25%?)"
            else:
                action = f"Investigate {r['feature']} further"
            print(f"  {i + 1}. {r['feature']}: {action}")
    else:
        print("  No strong signals found. Consider structural changes (dynamic sizing, multi-timeframe).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cr = CetaResearch()

    # ── Phase 1: Data Fetch ──
    print("=" * 70)
    print("  FEATURE IMPORTANCE ANALYSIS")
    print("  Champion: 63d mom, top 30%, 5% dip, D/E<1.0, 10 pos, 10% TSL")
    print("=" * 70)

    print("\n  Phase 1: Fetching data...")
    price_data = fetch_universe(cr, EXCHANGE, START_EPOCH, END_EPOCH)
    benchmark = fetch_benchmark(cr, BENCHMARK, EXCHANGE, START_EPOCH, END_EPOCH, warmup_days=250)
    fundamentals = fetch_fundamentals(cr, EXCHANGE)
    ext_fundamentals = fetch_extended_fundamentals(cr, EXCHANGE)
    sector_map = fetch_sector_map(cr, EXCHANGE)

    # ── Phase 2: Generate Entries (champion pipeline) ──
    print("\n  Phase 2: Computing entry universe (champion config)...")
    quality_universe = compute_quality_universe(
        price_data, CONSECUTIVE_YEARS, 0, rescreen_days=63, start_epoch=START_EPOCH)
    momentum_universe = compute_momentum_universe(
        price_data, MOMENTUM_LOOKBACK, MOMENTUM_PERCENTILE,
        rescreen_days=63, start_epoch=START_EPOCH)
    combined_universe = intersect_universes(quality_universe, momentum_universe)

    entries = compute_dip_entries(
        price_data, combined_universe, PEAK_LOOKBACK,
        DIP_THRESHOLD_PCT / 100.0, start_epoch=START_EPOCH)
    print(f"  Raw dip entries: {len(entries)}")

    # Apply fundamental filters
    entries = filter_entries_by_fundamentals(
        entries, fundamentals, ROE_THRESHOLD, DE_THRESHOLD, PE_THRESHOLD,
        missing_mode="skip")
    print(f"  After fundamental filter: {len(entries)}")

    # Apply regime filter
    regime_epochs = compute_regime_epochs(benchmark, REGIME_SMA)
    entries = [e for e in entries if not regime_epochs or e["epoch"] in regime_epochs]
    print(f"  After regime filter: {len(entries)}")

    # Deduplicate: keep only first entry per symbol within any 20-day window.
    # compute_dip_entries() fires every day a stock stays below peak threshold,
    # so the same dip episode can produce 30+ entries. We want one per episode.
    MIN_GAP_DAYS = 20
    min_gap_seconds = MIN_GAP_DAYS * 86400
    entries.sort(key=lambda x: (x["symbol"], x["entry_epoch"]))
    deduped = []
    last_entry_by_sym = {}
    for e in entries:
        sym = e["symbol"]
        if sym in last_entry_by_sym:
            if e["entry_epoch"] - last_entry_by_sym[sym] < min_gap_seconds:
                continue
        last_entry_by_sym[sym] = e["entry_epoch"]
        deduped.append(e)
    # Re-sort by entry_epoch for analysis
    deduped.sort(key=lambda x: x["entry_epoch"])
    print(f"  After dedup (min {MIN_GAP_DAYS}d gap): {len(deduped)} unique dip episodes")
    entries = deduped

    # ── Phase 3: Compute Technical Indicators ──
    print("\n  Phase 3: Computing technical indicators...")
    volume_ratios = compute_volume_ratios(price_data, 20)
    realized_vol = compute_realized_vol(price_data, 60)

    # Batch compute momentum percentiles for unique signal epochs
    unique_epochs = sorted(set(e["epoch"] for e in entries))
    print(f"  Computing momentum percentiles for {len(unique_epochs)} unique signal epochs...")
    percentile_cache = {}
    for ep in unique_epochs:
        percentile_cache[ep] = compute_momentum_percentile_at(price_data, ep, MOMENTUM_LOOKBACK)

    # ── Phase 4: Build Enriched Trades ──
    print("\n  Phase 4: Simulating individual entry outcomes...")
    trades = build_enriched_trades(
        entries, price_data, fundamentals, ext_fundamentals,
        volume_ratios, realized_vol, sector_map, regime_epochs,
        percentile_cache)

    if not trades:
        print("  ERROR: No trades generated. Aborting.")
        return

    # ── Phase 5: Analysis ──
    df = pd.DataFrame(trades)

    print(f"\n  Dataset: {len(df)} entries, {int(df['winner'].sum())} winners "
          f"({df['winner'].mean() * 100:.1f}%), {int((1 - df['winner']).sum())} losers")
    print(f"  Avg PnL: {df['pnl_pct'].mean():+.1f}%, Median: {df['pnl_pct'].median():+.1f}%")
    print(f"\n  Feature coverage:")
    for f in NUMERIC_FEATURES:
        n = df[f].notna().sum()
        print(f"    {f:<22} {n:>5}/{len(df)} ({n / len(df) * 100:.0f}%)")

    corr = analyze_correlations(df, NUMERIC_FEATURES)
    wl = analyze_winner_loser(df, NUMERIC_FEATURES)
    qrt = analyze_quartiles(df, NUMERIC_FEATURES)
    rf = analyze_random_forest(df, NUMERIC_FEATURES)
    analyze_exit_reasons(df)
    analyze_sectors(df)
    print_recommendations(corr, wl, rf)

    # ── Phase 6: Save Results ──
    output = {
        "strategy": "momentum_dip_champion_feature_importance",
        "config": {
            "momentum_lookback": MOMENTUM_LOOKBACK,
            "momentum_percentile": MOMENTUM_PERCENTILE,
            "dip_threshold_pct": DIP_THRESHOLD_PCT,
            "tsl_pct": TSL_PCT,
            "roe": ROE_THRESHOLD, "de": DE_THRESHOLD, "pe": PE_THRESHOLD,
        },
        "dataset": {
            "total_entries": len(df),
            "winners": int(df["winner"].sum()),
            "losers": int((1 - df["winner"]).sum()),
            "win_rate": round(df["winner"].mean() * 100, 1),
            "avg_pnl": round(df["pnl_pct"].mean(), 2),
            "median_pnl": round(df["pnl_pct"].median(), 2),
        },
        "correlations": corr,
        "winner_loser_comparison": wl,
        "quartile_analysis": [{"feature": q["feature"], "quartiles": q["quartiles"]} for q in qrt],
        "random_forest": rf,
        "enriched_trades": trades,
    }

    with open("result.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    size_kb = os.path.getsize("result.json") / 1024
    print(f"\n  Saved result.json ({size_kb:.0f} KB, {len(trades)} enriched trades)")


if __name__ == "__main__":
    main()

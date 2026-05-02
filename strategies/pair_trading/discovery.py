"""Pair Trading Phase 1 — Cointegration discovery on Nifty 50.

Methodology:
  - Load NSE daily closes for Nifty 50, 2022-01-01 to 2024-12-31 (3 yrs in-sample)
  - Use log prices (numerically stable, β ~ O(1))
  - For each pair (X, Y) with sufficient data:
      1. OLS log(Y) = α + β log(X) + ε
      2. ADF test on residuals (Engle-Granger step 2)
      3. Half-life of mean reversion (OU process)
      4. Hurst exponent (variance-ratio method)
      5. β stability across 3 sub-windows (2022, 2023, 2024)
  - Filter:
      p_adf < 0.05, half-life ∈ [1, 30] days, hurst < 0.5,
      β stability (CV across windows) < 0.30
  - Output: top ~30 pairs ranked by combined score

2025 is reserved as out-of-sample. Discovery uses ONLY 2022-2024.

Self-contained. Imports load_daily_data from intraday_breakout_prod.
"""
import sys, json, time
from datetime import datetime, timezone
from itertools import combinations

import numpy as np
import polars as pl
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller

sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import load_daily_data, SECONDS_IN_ONE_DAY


NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "ULTRACEMCO", "WIPRO",
]


# ── Cointegration & spread metrics ───────────────────────────────────────

def adf_pvalue(series: np.ndarray) -> float:
    """ADF test p-value. p < 0.05 = stationary, p > 0.05 = unit root (I(1))."""
    try:
        _, pvalue, _, _, _, _ = adfuller(series, autolag="AIC")
        return float(pvalue)
    except Exception:
        return 1.0


def cointegration_test(log_x: np.ndarray, log_y: np.ndarray):
    """Engle-Granger 2-step. Returns (pvalue, beta, alpha, residuals).

    Step 1: OLS log_y = α + β log_x + ε
    Step 2: ADF on ε (residuals)

    NOTE: caller must independently verify both series are I(1) — otherwise
    a spurious "cointegration" can arise when one series is already stationary
    (β≈0, residuals ≈ near-stationary series, ADF rejects falsely).
    """
    X = sm.add_constant(log_x)
    res = sm.OLS(log_y, X).fit()
    alpha, beta = float(res.params[0]), float(res.params[1])
    resid = res.resid
    _, pvalue, _, _, _, _ = adfuller(resid, autolag="AIC")
    return float(pvalue), beta, alpha, resid


def half_life_ou(spread: np.ndarray) -> float:
    """OU half-life: fit Δs_t = a + λ * s_{t-1} → half-life = ln(2) / -λ.

    Returns inf if not mean-reverting (λ ≥ 0).
    """
    s_lag = spread[:-1]
    s_diff = np.diff(spread)
    X = sm.add_constant(s_lag)
    try:
        res = sm.OLS(s_diff, X).fit()
        lam = float(res.params[1])
    except Exception:
        return float("inf")
    if lam >= 0:
        return float("inf")
    return float(np.log(2) / -lam)


def hurst_exponent(series: np.ndarray, max_lag: int = 20) -> float:
    """Hurst from variance-ratio method. <0.5 = mean-reverting, >0.5 = trending.

    Computed on price series — for residuals, this measures whether the spread
    walks like Brownian motion (H=0.5) or reverts (H<0.5).
    """
    series = np.asarray(series)
    lags = range(2, min(max_lag, len(series) // 4))
    if len(list(lags)) < 5:
        return 0.5
    tau = []
    valid_lags = []
    for lag in lags:
        diffs = series[lag:] - series[:-lag]
        if len(diffs) > 1:
            std = np.std(diffs)
            if std > 1e-12:
                tau.append(std)
                valid_lags.append(lag)
    if len(tau) < 5:
        return 0.5
    poly = np.polyfit(np.log(valid_lags), np.log(tau), 1)
    return float(poly[0])  # slope = H (variance scales as lag^(2H))


def beta_stability(log_x: np.ndarray, log_y: np.ndarray, window_idxs: list) -> float:
    """CV of β across sub-windows. Lower = more stable cointegration."""
    betas = []
    for (i_start, i_end) in window_idxs:
        if i_end - i_start < 50:
            continue
        x = log_x[i_start:i_end]
        y = log_y[i_start:i_end]
        try:
            X = sm.add_constant(x)
            res = sm.OLS(y, X).fit()
            betas.append(float(res.params[1]))
        except Exception:
            continue
    if len(betas) < 2:
        return float("inf")
    mean_b = np.mean(betas)
    if abs(mean_b) < 1e-6:
        return float("inf")
    return float(np.std(betas) / abs(mean_b))


# ── Discovery driver ─────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("PAIR TRADING — Phase 1 — Cointegration Discovery on Nifty 50")
    print("=" * 70)

    # In-sample: 2022-2024. Out-of-sample: 2025 (held out for Phase 2).
    start_date, end_date = "2022-01-01", "2024-12-31"
    start_ep = int(datetime.strptime(start_date, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    end_ep = int(datetime.strptime(end_date, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    print(f"\nIn-sample: {start_date} → {end_date} (3 yrs); 2025 reserved as OOS.")

    print("\nLoading daily data...")
    df_daily = load_daily_data(start_ep - 30 * SECONDS_IN_ONE_DAY, end_ep)
    df_n50 = df_daily.filter(pl.col("instrument").is_in(NIFTY_50))
    df_n50 = df_n50.filter(
        (pl.col("date_epoch") >= start_ep) & (pl.col("date_epoch") <= end_ep)
    )

    avail = df_n50["instrument"].unique().to_list()
    missing = sorted(set(NIFTY_50) - set(avail))
    print(f"  Coverage: {len(avail)}/50 stocks have daily data in window")
    if missing:
        print(f"  Missing: {missing}")

    # Pivot to wide: rows = date, columns = instrument, values = close
    pivot = (df_n50.select(["date_epoch", "instrument", "close"])
             .pivot(values="close", index="date_epoch", on="instrument"))
    pivot = pivot.sort("date_epoch")

    # Drop instruments with too many nulls (need ≥80% data coverage)
    n_dates = pivot.height
    keep_cols = ["date_epoch"]
    for col in pivot.columns:
        if col == "date_epoch":
            continue
        non_null = pivot[col].drop_nulls().len()
        if non_null >= 0.80 * n_dates:
            keep_cols.append(col)
    pivot = pivot.select(keep_cols)
    print(f"  After 80% coverage filter: {len(keep_cols) - 1} stocks, {n_dates} dates")

    # Drop dates with ANY null (need fully-aligned series for OLS)
    pivot_clean = pivot.drop_nulls()
    n_clean = pivot_clean.height
    print(f"  After date-alignment: {n_clean} usable dates")
    if n_clean < 252:
        print(f"  ERROR: Insufficient aligned dates ({n_clean} < 252).")
        sys.exit(1)

    syms = [c for c in pivot_clean.columns if c != "date_epoch"]
    n_pairs = len(syms) * (len(syms) - 1) // 2
    print(f"\nTesting {n_pairs} pairs ({len(syms)} stocks × (n-1)/2)...")

    # 3 sub-windows for β stability (each ≈1/3 of total)
    third = n_clean // 3
    window_idxs = [(0, third), (third, 2 * third), (2 * third, n_clean)]

    # Pre-compute log prices for all syms
    log_data = {sym: np.log(pivot_clean[sym].to_numpy().astype(float)) for sym in syms}

    # Engle-Granger prerequisite: each individual log-price must be I(1)
    # (i.e., NON-stationary on its own). Otherwise pair "cointegration"
    # is spurious — β≈0 picks the already-stationary series as residual.
    print("\nIndividual unit-root (ADF) test on each log-price series:")
    i1_pvals = {}
    non_i1 = []
    for sym in syms:
        p = adf_pvalue(log_data[sym])
        i1_pvals[sym] = p
        if p < 0.05:
            non_i1.append((sym, p))
    print(f"  {len(syms)} stocks tested; {len(non_i1)} stationary (excluded from pair tests):")
    for sym, p in sorted(non_i1, key=lambda x: x[1]):
        print(f"    {sym}: ADF p={p:.4f} (stationary, not I(1) — spurious-coint risk)")
    i1_syms = [s for s in syms if i1_pvals[s] >= 0.05]
    print(f"  {len(i1_syms)} I(1) stocks survive for pair testing.")
    n_pairs_after_i1 = len(i1_syms) * (len(i1_syms) - 1) // 2
    print(f"  → testing {n_pairs_after_i1} pairs (was {n_pairs}).")

    t0 = time.time()
    results = []
    n_done = 0
    for x_sym, y_sym in combinations(i1_syms, 2):
        n_done += 1
        log_x = log_data[x_sym]
        log_y = log_data[y_sym]
        try:
            pval, beta, alpha, resid = cointegration_test(log_x, log_y)
        except Exception:
            continue

        # Quick prefilter: skip if not even loosely cointegrated
        if pval > 0.10:
            continue

        try:
            hl = half_life_ou(resid)
            hu = hurst_exponent(resid)
            bstab = beta_stability(log_x, log_y, window_idxs)
        except Exception:
            continue

        results.append({
            "y": y_sym, "x": x_sym, "pair": f"{y_sym}/{x_sym}",
            "p_adf": pval, "beta": beta, "alpha": alpha,
            "half_life_days": hl, "hurst": hu, "beta_cv": bstab,
            "spread_std": float(np.std(resid)),
            "spread_mean": float(np.mean(resid)),
        })

        if n_done % 200 == 0:
            elapsed = time.time() - t0
            eta = elapsed * (n_pairs_after_i1 - n_done) / n_done
            print(f"  {n_done}/{n_pairs_after_i1} pairs tested ({elapsed:.0f}s, ETA {eta:.0f}s), "
                  f"{len(results)} candidates so far")

    print(f"\nTotal: {len(results)} pairs with p < 0.10 (out of {n_pairs_after_i1})")

    # Strict filter
    qualifying = [
        r for r in results
        if r["p_adf"] < 0.05
        and 1 <= r["half_life_days"] <= 30
        and r["hurst"] < 0.5
        and r["beta_cv"] < 0.30
    ]
    print(f"After strict filter (p<0.05, hl 1-30d, hurst<0.5, beta_cv<0.30): {len(qualifying)}")

    # Composite score: lower p, shorter half-life, lower hurst, lower β-cv all favored
    # Normalize each on its own and sum (lower = better)
    if qualifying:
        for r in qualifying:
            r["score"] = (
                r["p_adf"] * 100  # 0–5 contribution
                + r["half_life_days"] * 0.1  # 0.1–3 contribution
                + r["hurst"]  # 0–0.5 contribution
                + r["beta_cv"]  # 0–0.3 contribution
            )
        qualifying.sort(key=lambda r: r["score"])
    else:
        qualifying = sorted(results, key=lambda r: r["p_adf"])

    print(f"\nTop {min(30, len(qualifying))} pairs:")
    print(f"{'Rank':<4} {'Pair':<20} {'p_adf':>8} {'beta':>8} {'half_life':>10} "
          f"{'hurst':>7} {'beta_cv':>8} {'spread_std':>11}")
    print("-" * 86)
    for i, r in enumerate(qualifying[:30], 1):
        print(f"{i:<4} {r['pair']:<20} {r['p_adf']:>8.4f} {r['beta']:>8.3f} "
              f"{r['half_life_days']:>10.2f} {r['hurst']:>7.3f} {r['beta_cv']:>8.3f} "
              f"{r['spread_std']:>11.4f}")

    # Save results
    out = {
        "in_sample": [start_date, end_date],
        "n_stocks_total": len(syms),
        "n_stocks_i1": len(i1_syms),
        "stationary_excluded": [{"sym": s, "adf_p": p} for s, p in non_i1],
        "n_pairs_tested": n_pairs_after_i1,
        "n_pre_filter": len(results),
        "n_qualifying": len(qualifying),
        "qualifying": qualifying,
        "all_with_p_lt_0_10": results,
    }
    out_path = "/home/swas/backtester/pair_discovery_phase1.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved: {out_path}")

    print(f"\nElapsed: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

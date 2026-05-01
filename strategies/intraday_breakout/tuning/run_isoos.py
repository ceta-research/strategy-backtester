"""IS/OOS validation for gap-up variant.

Tests:
  - IS: 2023-2024 (train), OOS: 2022, 2025 (out-of-sample)
  - IS: 2022-2024 (train), OOS: 2025 (forward test)
  - Full: 2022-2025 (reference)
  - Walk-forward: train on year N, test on year N+1
"""
import sys, json
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import run_pipeline
from collections import defaultdict

base = {
    "initial_capital": 1000000,
    "prefetch_days": 500,
    "top_n": 50,
    "min_avg_turnover": 500000000,
    "n_day_high": 3,
    "n_day_ma": 10,
    "internal_regime_sma_period": 50,
    "internal_regime_threshold": 0.4,
    "internal_regime_exit_threshold": 0.35,
    "max_entry_bar": 15,
    "max_positions": 5,
    "eod_exit_minute": 925,
    "entry_mode": "market",
    "require_gap_up": True,
    "slippage_bps": 0,
    # Current best config (update with R2 results if available)
    "target_pct": 1.0,
    "stop_pct": 0.5,
    "trailing_stop_pct": 0,
}

# ═══════════════════════════════════════════════════════════════════════════
# TEST 1: Individual year performance
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  TEST 1: Individual Year Performance")
print("=" * 70)

year_results = []
for year in [2022, 2023, 2024, 2025]:
    config = {**base, "start_date": f"{year}-01-01", "end_date": f"{year}-12-31"}
    output = run_pipeline(config)
    r = output["results"][0]
    r["year"] = year
    year_results.append(r)

print("\n  %-6s %7s %7s %7s %7s %6s %5s" % ("Year", "Return", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for r in year_results:
    print("  %-6d %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        r["year"], r["total_return"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

# ═══════════════════════════════════════════════════════════════════════════
# TEST 2: IS/OOS splits
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  TEST 2: In-Sample / Out-of-Sample Splits")
print("=" * 70)

splits = [
    {"name": "Full (2022-2025)", "start": "2022-01-01", "end": "2025-12-31", "type": "full"},
    {"name": "IS: 2023-2024", "start": "2023-01-01", "end": "2024-12-31", "type": "IS"},
    {"name": "OOS: 2022", "start": "2022-01-01", "end": "2022-12-31", "type": "OOS"},
    {"name": "OOS: 2025", "start": "2025-01-01", "end": "2025-12-31", "type": "OOS"},
    {"name": "IS: 2022-2024", "start": "2022-01-01", "end": "2024-12-31", "type": "IS"},
    {"name": "OOS: 2025 (fwd)", "start": "2025-01-01", "end": "2025-12-31", "type": "OOS"},
]

split_results = []
for sp in splits:
    config = {**base, "start_date": sp["start"], "end_date": sp["end"]}
    output = run_pipeline(config)
    r = output["results"][0]
    r["split_name"] = sp["name"]
    r["split_type"] = sp["type"]
    split_results.append(r)

print("\n  %-25s %4s %7s %7s %7s %6s %5s" % ("Split", "Type", "CAGR", "MDD", "Calmar", "Trades", "WR"))
print("  " + "-" * 70)
for r in split_results:
    print("  %-25s %4s %6.2f%% %6.2f%% %7.3f %6d %4.0f%%" % (
        r["split_name"], r["split_type"],
        r["cagr"], r["mdd"], r["calmar"], r["trades"], r["win_rate"]))

# ═══════════════════════════════════════════════════════════════════════════
# TEST 3: Walk-forward (train year N, test year N+1)
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  TEST 3: Walk-Forward (train N → test N+1)")
print("=" * 70)

# For each pair: does the strategy work on the next year?
# Train on 2022 → Test on 2023
# Train on 2023 → Test on 2024
# Train on 2024 → Test on 2025

# Since our strategy has fixed rules (no parameter fitting per year),
# this is really testing yearly stability. We also test parameter
# sensitivity by running the full target/stop grid on IS and checking
# if the best IS config also wins on OOS.

targets = [0.6, 0.8, 1.0, 1.2, 1.5]
stops = [0.25, 0.35, 0.5, 0.75]

wf_results = []
for train_year in [2022, 2023, 2024]:
    test_year = train_year + 1

    # Find best config on train year
    best_calmar = -999
    best_cfg = None
    for target in targets:
        for stop in stops:
            config = {**base, "start_date": f"{train_year}-01-01", "end_date": f"{train_year}-12-31",
                      "target_pct": target, "stop_pct": stop}
            output = run_pipeline(config)
            r = output["results"][0]
            if r["calmar"] > best_calmar and r["trades"] >= 10:
                best_calmar = r["calmar"]
                best_cfg = {"target_pct": target, "stop_pct": stop}

    if best_cfg is None:
        print(f"  Train {train_year}: no valid config found (too few trades)")
        continue

    # Test best IS config on OOS year
    config_oos = {**base, "start_date": f"{test_year}-01-01", "end_date": f"{test_year}-12-31",
                  **best_cfg}
    output_oos = run_pipeline(config_oos)
    r_oos = output_oos["results"][0]

    # Also test default config on OOS for comparison
    config_default = {**base, "start_date": f"{test_year}-01-01", "end_date": f"{test_year}-12-31",
                      "target_pct": 1.0, "stop_pct": 0.5}
    output_default = run_pipeline(config_default)
    r_default = output_default["results"][0]

    wf_results.append({
        "train": train_year, "test": test_year,
        "is_best_cfg": best_cfg, "is_calmar": best_calmar,
        "oos_cagr": r_oos["cagr"], "oos_calmar": r_oos["calmar"],
        "oos_wr": r_oos["win_rate"], "oos_trades": r_oos["trades"],
        "default_cagr": r_default["cagr"], "default_calmar": r_default["calmar"],
    })

print("\n  %-12s %-20s %8s %10s %10s %10s" % (
    "Period", "IS best config", "IS Calm", "OOS CAGR", "OOS Calm", "Default"))
print("  " + "-" * 75)
for w in wf_results:
    cfg_str = "t=%.1f s=%.2f" % (w["is_best_cfg"]["target_pct"], w["is_best_cfg"]["stop_pct"])
    print("  %d→%d    %-20s %8.3f %9.2f%% %10.3f %9.2f%%" % (
        w["train"], w["test"], cfg_str, w["is_calmar"],
        w["oos_cagr"], w["oos_calmar"], w["default_cagr"]))

# ═══════════════════════════════════════════════════════════════════════════
# TEST 4: Monthly return distribution
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  TEST 4: Monthly Return Distribution (full period, 0 slip)")
print("=" * 70)

# Re-run full period to get trade log
config_full = {**base, "start_date": "2022-01-01", "end_date": "2025-12-31"}
output_full = run_pipeline(config_full)
trades_full = output_full["results"][0]["trade_log"]

monthly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
for t in trades_full:
    month_key = t["trade_date"][:7]
    monthly[month_key]["pnl"] += t["pnl"]
    monthly[month_key]["trades"] += 1
    if t["pnl"] > 0:
        monthly[month_key]["wins"] += 1

# Stats
monthly_returns = [m["pnl"] / 1000000 * 100 for m in monthly.values()]
positive_months = sum(1 for r in monthly_returns if r > 0)
total_months = len(monthly_returns)

print("  Total months: %d" % total_months)
print("  Positive months: %d (%.0f%%)" % (positive_months, positive_months/total_months*100))
print("  Negative months: %d (%.0f%%)" % (total_months - positive_months,
                                            (total_months - positive_months)/total_months*100))
if monthly_returns:
    print("  Best month: %+.2f%%" % max(monthly_returns))
    print("  Worst month: %+.2f%%" % min(monthly_returns))
    print("  Median month: %+.2f%%" % sorted(monthly_returns)[len(monthly_returns)//2])

# Show all months
print("\n  %-8s %6s %5s %5s %8s" % ("Month", "Trades", "WR", "PnL%", "Cumul%"))
cumul = 0
for mk in sorted(monthly.keys()):
    m = monthly[mk]
    ret = m["pnl"] / 1000000 * 100
    cumul += ret
    wr = m["wins"] / m["trades"] * 100 if m["trades"] else 0
    print("  %-8s %6d %4.0f%% %+5.2f%% %+7.2f%%" % (mk, m["trades"], wr, ret, cumul))

print("\n  Done.")

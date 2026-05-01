"""R2: Fine grid sweep for gap-up variant.

Sweeps:
  - Target: 0.6, 0.8, 1.0, 1.2, 1.5%
  - Stop: 0.25, 0.35, 0.5, 0.65, 0.75%
  - Trailing stop: 0, 0.5, 0.75, 1.0%
  - Min gap size: 0, 5, 10, 20, 50 bps

Total: 5 * 5 * 4 * 5 = 500 configs (but we run in stages)
"""
import sys, json, time
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import run_pipeline

base = {
    "start_date": "2022-01-01",
    "end_date": "2025-12-31",
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
}

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 1: Target x Stop grid (no trailing, no min gap)
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  STAGE 1: Target × Stop Grid (25 configs)")
print("=" * 70)

targets = [0.6, 0.8, 1.0, 1.2, 1.5]
stops = [0.25, 0.35, 0.5, 0.65, 0.75]

stage1_results = []
for target in targets:
    for stop in stops:
        config = {**base, "target_pct": target, "stop_pct": stop, "trailing_stop_pct": 0}
        output = run_pipeline(config)
        r = output["results"][0]
        r["target"] = target
        r["stop"] = stop
        stage1_results.append(r)

# Sort by Calmar
stage1_results.sort(key=lambda x: x["calmar"], reverse=True)

print("\n" + "=" * 70)
print("  STAGE 1 RESULTS (sorted by Calmar)")
print("=" * 70)
print("  %-7s %-6s %7s %7s %7s %7s %6s %5s" % (
    "Target", "Stop", "CAGR", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 60)
for r in stage1_results[:15]:
    print("  %-7s %-6s %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        "%.1f%%" % r["target"], "%.2f%%" % r["stop"],
        r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

# Pick top 5 target/stop combos for next stages
top5 = stage1_results[:5]
print("\n  Top 5 configs for Stage 2:")
for i, r in enumerate(top5):
    print("    %d. target=%.1f%% stop=%.2f%% → CAGR=%.2f%% Calmar=%.3f" % (
        i+1, r["target"], r["stop"], r["cagr"], r["calmar"]))

best_target = top5[0]["target"]
best_stop = top5[0]["stop"]

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 2: Trailing stop (on best target/stop from Stage 1)
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  STAGE 2: Trailing Stop (best target=%.1f%%, stop=%.2f%%)" % (best_target, best_stop))
print("=" * 70)

trailing_stops = [0, 0.3, 0.5, 0.75, 1.0, 1.5]

stage2_results = []
for tsl in trailing_stops:
    config = {**base, "target_pct": best_target, "stop_pct": best_stop, "trailing_stop_pct": tsl}
    output = run_pipeline(config)
    r = output["results"][0]
    r["tsl"] = tsl
    stage2_results.append(r)

print("\n  %-8s %7s %7s %7s %7s %6s %5s" % ("TSL", "CAGR", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for r in stage2_results:
    print("  %-8s %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        "%.1f%%" % r["tsl"] if r["tsl"] > 0 else "off",
        r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

best_tsl = max(stage2_results, key=lambda x: x["calmar"])["tsl"]

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3: Minimum gap size
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  STAGE 3: Minimum Gap Size (target=%.1f%%, stop=%.2f%%, tsl=%.1f%%)" % (
    best_target, best_stop, best_tsl))
print("=" * 70)

min_gaps = [0, 5, 10, 15, 20, 30, 50, 100]

stage3_results = []
for mg in min_gaps:
    config = {**base, "target_pct": best_target, "stop_pct": best_stop,
              "trailing_stop_pct": best_tsl, "min_gap_bps": mg}
    output = run_pipeline(config)
    r = output["results"][0]
    r["min_gap"] = mg
    stage3_results.append(r)

print("\n  %-10s %7s %7s %7s %7s %6s %5s" % ("MinGap", "CAGR", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 60)
for r in stage3_results:
    print("  %-10s %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        "%d bps" % r["min_gap"],
        r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

best_min_gap = max(stage3_results, key=lambda x: x["calmar"])["min_gap"]

# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4: Volume at open filter
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  STAGE 4: Volume at Open Filter")
print("=" * 70)

volume_ratios = [0, 1.0, 1.5, 2.0, 3.0, 5.0]

stage4_results = []
for vr in volume_ratios:
    config = {**base, "target_pct": best_target, "stop_pct": best_stop,
              "trailing_stop_pct": best_tsl, "min_gap_bps": best_min_gap,
              "min_first_bar_volume_ratio": vr}
    output = run_pipeline(config)
    r = output["results"][0]
    r["vol_ratio"] = vr
    stage4_results.append(r)

print("\n  %-10s %7s %7s %7s %7s %6s %5s" % ("VolRatio", "CAGR", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 60)
for r in stage4_results:
    print("  %-10s %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        "%.1fx" % r["vol_ratio"] if r["vol_ratio"] > 0 else "off",
        r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

best_vol = max(stage4_results, key=lambda x: x["calmar"])["vol_ratio"]

# ═══════════════════════════════════════════════════════════════════════════
# FINAL: Slippage sensitivity with best config
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  FINAL: Best Config + Slippage Sweep")
print("  target=%.1f%% stop=%.2f%% tsl=%.1f%% min_gap=%d vol_ratio=%.1f" % (
    best_target, best_stop, best_tsl, best_min_gap, best_vol))
print("=" * 70)

final_results = []
for slip in [0, 1, 2, 3, 5]:
    config = {**base, "target_pct": best_target, "stop_pct": best_stop,
              "trailing_stop_pct": best_tsl, "min_gap_bps": best_min_gap,
              "min_first_bar_volume_ratio": best_vol, "slippage_bps": slip}
    output = run_pipeline(config)
    r = output["results"][0]
    r["slip"] = slip
    final_results.append(r)

print("\n  %-8s %7s %7s %7s %7s %6s %5s" % ("Slip", "CAGR", "MDD", "Sharpe", "Calmar", "Trades", "WR"))
print("  " + "-" * 55)
for r in final_results:
    print("  %-8s %6.2f%% %6.2f%% %7.3f %7.3f %6d %4.0f%%" % (
        "%d bps" % r["slip"],
        r["cagr"], r["mdd"], r["sharpe"], r["calmar"], r["trades"], r["win_rate"]))

# Yearly breakdown for 0-slip best config
print("\n  YEARLY BREAKDOWN (0 slip):")
from collections import defaultdict
trades = final_results[0]["trade_log"]
yearly = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
for t in trades:
    y = t["trade_date"][:4]
    yearly[y]["trades"] += 1
    yearly[y]["pnl"] += t["pnl"]
    if t["pnl"] > 0:
        yearly[y]["wins"] += 1

prev_eq = 1000000
for y in sorted(yearly.keys()):
    d = yearly[y]
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    ret = d["pnl"] / prev_eq * 100
    print("    %s: %4d trades (%d/wk), WR=%4.0f%%, Return=%+6.1f%%" % (
        y, d["trades"], d["trades"] // 52, wr, ret))
    prev_eq += d["pnl"]

# Save summary
summary = {
    "best_config": {
        "target_pct": best_target,
        "stop_pct": best_stop,
        "trailing_stop_pct": best_tsl,
        "min_gap_bps": best_min_gap,
        "min_first_bar_volume_ratio": best_vol,
    },
    "stage1_top5": [{k: v for k, v in r.items() if k not in ("trade_log", "equity_points")}
                     for r in top5],
    "final_slippage": [{k: v for k, v in r.items() if k not in ("trade_log", "equity_points")}
                        for r in final_results],
}
with open("/home/swas/backtester/results_r2_sweep.json", "w") as f:
    json.dump(summary, f, indent=2)
print("\n  Saved: results_r2_sweep.json")

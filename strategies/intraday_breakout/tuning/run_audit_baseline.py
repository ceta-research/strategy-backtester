"""Audit baseline: rerun pre-fix champion config with both bug fixes applied.

Compares to pre-fix numbers from STATUS.md to quantify the inflation.

Pre-fix numbers to beat (from STATUS.md, gap-up only, target=1.0/stop=0.5,
entry=15 bars, max_pos=5):
    0 bps: CAGR=24.14%, MDD=-0.81%, Calmar=29.84, WR=58%, Trades=1425
    3 bps: CAGR=19.81%, MDD=-0.86%, Calmar=22.93, WR=57%
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
    "target_pct": 1.0,
    "stop_pct": 0.5,
    "trailing_stop_pct": 0,
    "require_gap_up": True,
    "entry_mode": "market",
}

results = {}
for slip in [0, 3]:
    cfg = {**base, "slippage_bps": slip}
    print(f"\n{'='*60}")
    print(f"AUDIT BASELINE — slippage={slip} bps (gap-up, t=1.0/s=0.5)")
    print(f"{'='*60}")
    t0 = time.time()
    out = run_pipeline(cfg)
    elapsed = time.time() - t0
    r = out["results"][0]
    results[slip] = r
    print(f"  CAGR={r['cagr']:.2f}%  MDD={r['mdd']:.2f}%  "
          f"Sharpe={r['sharpe']:.3f}  Calmar={r['calmar']:.3f}  "
          f"Trades={r['trades']}  WR={r['win_rate']:.0f}%  ({elapsed:.0f}s)")

print(f"\n{'='*60}")
print("BEFORE / AFTER comparison")
print(f"{'='*60}")
print(f"{'Metric':<12} {'PRE-FIX':>12} {'POST-FIX':>12} {'Delta':>12}")
print("-" * 50)
pre = {0: dict(cagr=24.14, mdd=-0.81, calmar=29.84, wr=58, trades=1425),
       3: dict(cagr=19.81, mdd=-0.86, calmar=22.93, wr=57, trades=1425)}
for slip in [0, 3]:
    r = results[slip]
    p = pre[slip]
    print(f"\n--- slippage={slip} bps ---")
    print(f"{'CAGR':<12} {p['cagr']:>11.2f}% {r['cagr']:>11.2f}% {r['cagr']-p['cagr']:>+11.2f}pp")
    print(f"{'MDD':<12} {p['mdd']:>11.2f}% {r['mdd']:>11.2f}% {r['mdd']-p['mdd']:>+11.2f}pp")
    print(f"{'Calmar':<12} {p['calmar']:>12.3f} {r['calmar']:>12.3f} {r['calmar']-p['calmar']:>+12.3f}")
    print(f"{'WR':<12} {p['wr']:>11d}% {r['win_rate']:>11.0f}% {r['win_rate']-p['wr']:>+11.0f}pp")
    print(f"{'Trades':<12} {p['trades']:>12d} {r['trades']:>12d} {r['trades']-p['trades']:>+12d}")

with open("/home/swas/backtester/audit_baseline_result.json", "w") as f:
    json.dump({"post_fix": results, "pre_fix_reference": pre}, f, indent=2)

# Decision gate
cagr_0 = results[0]['cagr']
cagr_3 = results[3]['cagr']
print(f"\n{'='*60}")
print("AUDIT DECISION GATE")
print(f"{'='*60}")
if cagr_0 < 5:
    print(f"  RESULT: STRATEGY DEAD (CAGR={cagr_0:.2f}% < 5% at 0 slip)")
    print("  ACTION: Stop here. Numbers were entirely artifacts.")
elif cagr_3 < 0:
    print(f"  RESULT: STRATEGY DEAD with realistic costs ({cagr_3:.2f}% at 3 bps)")
    print("  ACTION: Stop here. No realistic edge.")
elif cagr_0 >= 10:
    print(f"  RESULT: SURVIVES ({cagr_0:.2f}% at 0 slip, {cagr_3:.2f}% at 3 bps)")
    print("  ACTION: Continue to R2 sweep rerun.")
else:
    print(f"  RESULT: MARGINAL ({cagr_0:.2f}% at 0 slip, {cagr_3:.2f}% at 3 bps)")
    print("  ACTION: Re-evaluate priorities.")

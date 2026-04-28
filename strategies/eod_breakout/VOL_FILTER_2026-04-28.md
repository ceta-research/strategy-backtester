# eod_breakout — Universe-level vol filter (R6, negative result)

**Date:** 2026-04-28 pt3
**Engine:** post-audit `fbcd36a+`
**Hypothesis:** Adding a per-stock trailing-volatility filter to the universe
should reduce MDD without proportional CAGR loss. Stocks with high realized
vol contribute disproportionately to drawdowns; excluding them should improve
Calmar. Signal-side approximation of true vol-scaled position sizing (which
would require simulator changes — protected file).

**Result:** **Hypothesis falsified.** No vol threshold delivers a Pareto
improvement. Every setting tested hurts both Sharpe (-0.30 to -0.40) and
Calmar (-0.10 to -0.24) vs the regime+holdout champion.

---

## Implementation

`engine/signals/eod_breakout.py` — added two entry-config params:

```yaml
entry:
  max_stock_vol_pct: [40, 50, ...]   # threshold; sentinel >= 500 = no filter
  vol_lookback_days: [60]            # rolling window
```

When active, the signal generator computes per-instrument rolling std of
simple daily returns over `vol_lookback_days`, annualized by `√252`, and
adds an `entry_filter` clause `trailing_vol_annual < max_stock_vol_pct / 100`.

**Backward compatibility:** sentinel `>= 500` skips vol computation entirely
and produces byte-identical output. Smoke-tested: champion config without
vol params returns identical CAGR/MDD/Sharpe/equity_curve/trades to the
existing `champion.json` (1795 trades, 5922 curve points, 0 differences).

## Sweep results

`config_round6_volfilter.yaml` — 6 thresholds × 1 lookback = 6 configs at
champion baseline (n_day_high=3, n_day_ma=10, ds={5,0.40}, regime+holdout,
tsl=8, min_hold=7).

| max_vol_pct | CAGR | MDD | Cal | Sharpe | Vol | Trades |
|---:|---:|---:|---:|---:|---:|---:|
| 30 | 10.81% | **-19.19%** | 0.563 | 0.881 | 9.99% | 1372 |
| 40 | 10.99% | -25.96% | 0.423 | 0.781 | 11.50% | 1589 |
| 50 | 13.11% | -29.86% | 0.439 | 0.874 | 12.71% | 1722 |
| 60 | 14.09% | -29.05% | 0.485 | 0.888 | 13.61% | 1826 |
| 70 | 13.78% | -27.15% | 0.508 | 0.877 | 13.43% | 1843 |
| **999 (champion)** | **17.68%** | -26.75% | **0.661** | **1.183** | 13.25% | 1795 |

### Pareto check vs champion (max_vol=999)

| max_vol | Δ CAGR | Δ MDD | Δ Cal | Δ Sharpe |
|---:|---:|---:|---:|---:|
| 30 | -6.87pp | +7.56pp | -0.098 | -0.302 |
| 40 | -6.69pp | +0.79pp | -0.238 | -0.402 |
| 50 | -4.57pp | -3.11pp | -0.222 | -0.309 |
| 60 | -3.59pp | -2.30pp | -0.176 | -0.295 |
| 70 | -3.90pp | -0.40pp | -0.153 | -0.306 |

**Every setting strictly worse on Sharpe AND Calmar.** Only vol=30 cuts MDD
meaningfully (+7.56pp better) but at -6.87pp CAGR — a steep trade.

## Why the hypothesis failed

1. **Breakout strategies need high-vol momentum names.** The high-CAGR alpha
   contributors in eod_breakout ARE the high-vol momentum stocks. Filtering
   them out doesn't selectively remove "losers"; it removes the breakout
   candidates that drive returns.

2. **MDD is regime-driven, not stock-vol-driven.** The champion's -26.75%
   MDD is post-NIFTYBEES-SMA(100) regime gate. The remaining drawdown is
   broad-market (2018 NBFC, 2020 COVID, 2022 ramp, 2025 turn) where ALL
   stocks drop together. Per-stock vol filtering doesn't help with
   systemic-risk drawdowns.

3. **Self-selection by ranking already favors lower-vol-among-breakouts.**
   The `top_gainer` ranking with `max_positions=15` already concentrates on
   the most liquid-and-lowest-noise breakouts. Adding a vol cap on top mostly
   removes the high-CAGR breakouts that the ranking would have picked.

4. **vol=30 IS the only setting that reduces MDD**, but at huge CAGR cost
   (-6.87pp). This says: *to actually shrink MDD via vol, you need to remove
   so much of the universe that you're effectively running a different,
   defensive strategy*. Not a free improvement.

## Conclusions for forward work

1. **Universe-level vol filtering is not the right lever for eod_breakout.**
   Don't pursue further variants (different lookback, dynamic thresholds, etc.)
   without a different theoretical motivation.

2. **True per-position vol-scaled sizing remains untested.** The sized-by-vol
   thesis (each position contributes equal vol) requires simulator changes.
   The universe-filter result does NOT falsify per-position sizing — those
   are mechanistically different. If we want to test per-position sizing,
   it requires modifying `engine/simulator.py` (protected; needs explicit
   approval).

3. **The MDD floor is regime-driven.** To go below -26% MDD on eod_breakout
   without giving up CAGR, the lever is probably better regime detection
   (multi-signal regime, breadth indicators, dispersion measures) — not
   per-stock filtering.

4. **eod_technical not tested.** Different mechanism (multi-window technical
   features). Universe vol filter could behave differently there. Not
   expected to be a free win given the eod_b result, but inexpensive to
   verify if needed (~20 min compute).

## Artifacts

| File | Purpose |
|---|---|
| `engine/signals/eod_breakout.py` | Added `max_stock_vol_pct` + `vol_lookback_days` params; gated by sentinel for backward compat |
| `strategies/eod_breakout/config_round6_volfilter.yaml` | 6-config sweep |
| `results/eod_breakout/round6_volfilter.json` | Sweep result |
| `strategies/eod_breakout/VOL_FILTER_2026-04-28.md` | This writeup |

The vol-filter code stays in the codebase (gated, no overhead when off)
for future experiments — both as an option for other strategies and for
re-running with different parameter regimes.

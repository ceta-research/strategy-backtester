# eod_technical — Regime+Holdout Experiment (NEGATIVE RESULT)

**Date:** 2026-04-28
**Engine:** post-audit `fbcd36a+`
**Hypothesis:** Apply the regime+holdout methodology that lifted eod_breakout
(Sharpe 0.804 → 1.334) to eod_technical, on the theory that eod_technical's
top-CAGR-mediocre-Sharpe profile reflects similar regime fragility.
**Result:** Methodology DOES NOT TRANSFER. Current champion stays unchanged.
**Result files:** `results/eod_technical/holdout_train_2010-2024.json`,
`results/eod_technical/holdout_champion_full.json`,
`results/eod_technical/regime_sweep.json`

---

## Bottom-line

Both halves of the methodology failed:

| Step | Outcome | Strict Pareto vs current? |
|---|---|---|
| Phase 1: Holdout retrain (648-config sweep on 2010-2024) | Best Calmar candidate `1_9_2_2` slightly worse on full 2010-2026 | **FAIL** |
| Phase 2: NIFTYBEES SMA regime gate (8-config sweep on current champion) | All 8 variants worse on CAGR/Calmar/Sharpe | **FAIL** |

| Variant | CAGR | MDD | Calmar | Sharpe | 2025 |
|---|---:|---:|---:|---:|---:|
| **Current champion (`1_3_6_2`, no regime)** | **19.63%** | **-25.95%** | **0.757** | **1.067** | **+2.69%** |
| Holdout retrain (`1_9_2_2`, no regime) | 19.36% | -27.31% | 0.709 | 1.040 | +0.29% |
| Best regime config (SMA=200, force_exit=False) | 16.76% | -34.52% | 0.485 | 0.732 | +16.36% |
| Worst regime config (SMA=100, force_exit=True) | 9.04% | -44.44% | 0.203 | 0.364 | +9.29% |

The regime gate trades MORE than 2.5pp of CAGR for marginal 2025 lift,
worsens both MDD and Sharpe substantially, and has the parameter direction
**inverted** from eod_breakout (looser SMA wins, force_exit=False wins).

---

## Phase 1: Holdout retrain (2010-2024 sweep)

### Setup
648 configs: ndma[3,5,7] × ndh[3,5,7] × ds[{3,0.54},{5,0.40}] × mh[0,3,7]
× tsl[8,10,12,15] × max_pos[10,15,20]. End-epoch = 2025-01-01 (holdout
boundary). Mirror of `eod_breakout/config_holdout_train.yaml`.

### Result
Top-5 by Calmar all share `ndh=5, tsl=10, max_pos=15`. Robust plateau.
Differences from current champion are minor (ndma 3↔5↔7, mh 0↔3 — all on
the same plateau).

| Rank | Config | CAGR (2010-2024) | MDD | Calmar | Sharpe |
|---:|---|---:|---:|---:|---:|
| 1 | `1_9_2_2` (ndma=5, mh=0) | 21.27% | -26.82% | 0.793 | 1.159 |
| 2 | `1_15_2_2` (ndma=7, mh=0) | 21.64% | -27.35% | 0.791 | 1.178 |
| 3 | `1_3_2_2` (ndma=3, mh=0) | 20.89% | -26.45% | 0.790 | 1.125 |
| 4 | `1_15_6_2` (ndma=7, mh=3) | 21.07% | -26.92% | 0.783 | 1.142 |
| 5 | `1_9_6_2` (ndma=5, mh=3) | 20.78% | -26.91% | 0.772 | 1.124 |
| **8** | **`1_3_6_2` CURRENT (ndma=3, mh=3)** | **20.24%** | **-26.63%** | **0.760** | **1.084** |

**Current champion ranks #8 of 648** in the holdout sweep — already in the
robust top cluster (top 1.2%). Contrast with eod_breakout, where the
pre-experiment champion ranked #457 of 1152 in its holdout sweep.

### Verification on full 2010-2026

| Metric | Current (`1_3_6_2`) | Holdout (`1_9_2_2`) | Δ |
|---|---:|---:|---:|
| CAGR | 19.63% | 19.36% | -0.27pp |
| MDD | -25.95% | -27.31% | -1.36pp |
| Calmar | 0.757 | 0.709 | -0.048 |
| Sharpe | 1.067 | 1.040 | -0.027 |
| Sharpe (arith) | 1.046 | 1.024 | -0.022 |
| Sortino | 1.510 | 1.468 | -0.042 |
| 2025 | +2.69% | +0.29% | -2.40pp |
| 2026 YTD | -2.20% | -7.59% | -5.38pp |
| Final value | ₹182.8M | ₹176.2M | -₹6.6M |

**Strict Pareto: holdout candidate fails on every dimension.** Don't promote.

### Year-by-year (where they differ)

Most years are identical because both configs share the same ndh/tsl/sort
and ndma 3↔5 doesn't materially shift entries. Differences:

| Year | Current | Holdout | Δ |
|---|---:|---:|---:|
| 2016 | -11.38% | -6.33% | +5.05 |
| 2017 | +57.56% | +55.33% | -2.23 |
| 2018 | -10.86% | -13.65% | -2.79 |
| 2019 | +2.54% | -2.25% | -4.80 |
| 2024 | +31.20% | +40.96% | +9.76 |
| 2025 | +2.69% | +0.29% | -2.40 |
| 2026 | -2.20% | -7.59% | -5.38 |

Holdout helps 2016 and 2024, hurts 2018/2019/2025/2026. Net effect: slightly
worse end-to-end, smoother in middle, fragile at the live edge. Not
something to ship.

---

## Phase 2: Regime sweep (current champion + NIFTYBEES gate)

### Setup
Since Phase 1 failed Pareto, the current champion is the regime baseline.
8 configs: `regime_sma_period × force_exit_on_regime_flip`. Required Phase 0
engine work: wired regime support into `engine/signals/eod_technical.py`'s
legacy scanner+order_generator path (slow-path: per-entry-config order
generation; fast path unchanged when regime disabled).

### Result (sorted by Calmar)

| Config | SMA | ForceExit | CAGR | MDD | Calmar | Sharpe | 2025 | Trades |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| 1_5_1_1 | 200 | False | 16.76% | -34.52% | 0.485 | 0.732 | +16.36% | 1211 |
| 1_7_1_1 | 250 | False | 16.23% | -37.41% | 0.434 | 0.711 | +3.01% | 1200 |
| 1_3_1_1 | 150 | False | 15.64% | -37.66% | 0.415 | 0.673 | +25.77% | 1188 |
| 1_8_1_1 | 250 | True | 14.15% | -40.73% | 0.347 | 0.618 | +1.50% | 1501 |
| 1_6_1_1 | 200 | True | 11.48% | -42.75% | 0.269 | 0.475 | +11.38% | 1702 |
| 1_4_1_1 | 150 | True | 11.53% | -44.30% | 0.260 | 0.474 | +28.58% | 1604 |
| 1_1_1_1 | 100 | False | 9.91% | -40.42% | 0.245 | 0.396 | +17.24% | 1214 |
| 1_2_1_1 | 100 | True | 9.04% | -44.44% | 0.203 | 0.364 | +9.29% | 1628 |
| **BASELINE (no regime)** | — | — | **19.63%** | **-25.95%** | **0.757** | **1.067** | **+2.69%** | **1303** |

### Two structural observations

1. **Direction inverted vs eod_breakout.** Looser SMA is better here (200 ≫ 100),
   and `force_exit=False > True` universally. eod_breakout's winner was the
   exact opposite (SMA=100, force_exit=True). This is not noise — it's
   consistent across all eight pairs.

2. **2025 actually lifts under regime, but everything else collapses.** Some
   variants take 2025 to +25-28%. But the gate's annualized cost (whipsaw
   in trending years) dwarfs the 2025 benefit. The strategy doesn't need
   the 2025 patch.

---

## Why the methodology doesn't transfer

### eod_breakout's regime+holdout fixed two specific problems:

1. **A 2025 collapse** (-16.57%) that parameter tuning alone could not escape.
   The holdout retrain saw the same -13% in 2025. The regime gate flipped
   it to +18.67%.

2. **A fragile parameter region.** Pre-experiment champion ranked #457 of
   1152 in the holdout sweep, indicating it was selected by full-period fit
   that included contaminating 2025 data. Holdout-clean params were
   genuinely different and Pareto-improving.

### eod_technical has neither problem:

1. **2025 is +2.69%, not a collapse.** The 2010-2019 weakness flagged in
   STATUS.md was the original "regime fragility" concern — but pre-2019
   weakness is structural to NSE mid-cap mechanism, not a parameter issue
   to be tuned away. Regime-gating modern data won't recover lost
   pre-2019 returns.

2. **Current champion is in the holdout top 1.2%.** Already robust by
   construction. There's nothing for holdout retraining to find.

### Mechanism-level reasons regime gate hurts:

- **Avg hold 60d** (vs eod_breakout 120d). Strategy already cycles positions
  twice as fast — entries adapt to changing breadth at the position level.
- **min_hold=3d** (vs eod_breakout's holdout-trained min_hold=7d). Regime
  flips can force-exit positions within days of entry, before the breakout
  thesis plays out.
- **Direction-score breadth filter** is already a regime gate at the
  ENTRY-DAY scale (3-day MA of % stocks above their MA, threshold 0.54).
  Stacking a 100-250d NIFTYBEES SMA gate on top is double-filtering at
  vastly different timescales — they fight rather than reinforce.

---

## Recommendation

**Don't apply this methodology to other COMPLETE strategies without first
checking the 2025 OOS test.** The regime+holdout approach is a 2025-collapse
remediation, not a universal Sharpe-lift technique.

Strategies on STATUS.md's deferred list to re-evaluate:

| Strategy | 2025 return (need to verify) | Apply regime+holdout? |
|---|---|---|
| `quality_dip_tiered` | TBD — check first | If 2025 < -10%, candidate. -47% solo MDD also flags structural issues. |
| `trending_value` | TBD — check first | Mechanism is value, not breakout — regime less relevant. Defer. |
| `low_pe` | TBD — check first | Already most defensive (Cal 1.016). Probably no upside. |

Add a 5-min "2025 OOS check" gate before authorizing any future regime+holdout
session: read `champion_verify.json`'s 2025 yearly return; only proceed if
materially negative (< -10%).

---

## What this experiment delivered

Even though the strategy outcome is negative, three durable artifacts:

1. **`engine/signals/eod_technical.py`** now supports the regime gate via
   the same `regime_instrument` / `regime_sma_period` /
   `force_exit_on_regime_flip` schema as eod_breakout. Slow path runs
   per-entry-config; fast path is byte-identical to the prior wrapper.
2. **`scripts/decode_config_id.py`** decodes a sweep's config_id back to
   parameter values without re-running. Useful for any future sweep
   inspection.
3. **A documented mechanism-level reason regime+holdout fails on
   eod_technical** — saves the next session from re-deriving it.

---

## Files produced

| File | Purpose |
|---|---|
| `engine/signals/eod_technical.py` (edit) | Wrapper-level regime support |
| `scripts/decode_config_id.py` (new) | Sweep config_id decoder |
| `strategies/eod_technical/config_holdout_train.yaml` | 648-config holdout sweep |
| `strategies/eod_technical/config_holdout_champion_full.yaml` | Holdout candidate verification |
| `strategies/eod_technical/config_regime_sweep.yaml` | 8-config regime sweep |
| `results/eod_technical/holdout_train_2010-2024.json` | Holdout sweep result |
| `results/eod_technical/holdout_champion_full.json` | Verified candidate result |
| `results/eod_technical/regime_sweep.json` | Regime sweep result |
| `REGIME_AND_HOLDOUT_2026-04-28.md` | This document |

## Predecessor

- `OPTIMIZATION.md` — Original optimization (R0-R4) of eod_technical

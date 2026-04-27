# N-leg ensemble experiment (2026-04-28)

**Goal:** test whether adding a third low-correlation leg (low_pe) improves the
2-leg `eod_b + eod_t invvol qtly` champion (Sharpe 1.281, Cal 0.789).

**Result:** **negative on Sharpe**, **positive on Calmar** — Pareto trade-off, no
clean win. Recommendation: keep the 2-leg as primary, optionally add a small
(20-30%) low_pe sleeve if MDD reduction is valued.

---

## Setup

Three legs evaluated on full 2010-2026:

| Leg | Solo CAGR | Solo MDD | Solo Sharpe | Solo Vol | Corr to eod_b | Corr to eod_t |
|---|---:|---:|---:|---:|---:|---:|
| `eod_breakout` | 17.68% | -26.75% | 1.183 | 13.25% | — | 0.590 |
| `eod_technical` | 19.63% | -25.95% | 1.067 | 16.53% | 0.590 | — |
| `low_pe` (champion params, full window) | **5.86%** | -12.08% | 0.521 | 7.41% | **0.391** | **0.450** |

low_pe has the lowest correlation of any candidate (vs trending_value, factor_composite,
quality_dip_tiered, ml_supertrend) but its full-period CAGR is severely depressed
because FMP NSE fundamentals are sparse pre-2018. The leg holds cash for most of
2010-2017 — that compresses both CAGR and vol, which messes with inverse-vol
weighting.

---

## Full-period results (2010-2026)

| Variant | Weights (b/t/lp) | CAGR | MDD | Cal | Sharpe |
|---|---|---:|---:|---:|---:|
| **2-leg invvol qtly (champion)** | 0.555 / 0.445 / — | **18.79%** | -23.81% | 0.789 | **1.281** |
| 2-leg 50/50 qtly | 0.500 / 0.500 / — | 18.90% | -23.65% | 0.799 | 1.272 |
| 3-leg +low_pe invvol qtly | 0.276 / 0.221 / 0.503 | 12.45% | **-15.02%** | **0.829** | 1.162 |
| 3-leg +low_pe equal qtly | 0.333 / 0.333 / 0.333 | 14.65% | -17.79% | 0.823 | 1.225 |
| 3-leg +qdt invvol qtly | 0.350 / 0.281 / 0.369 | 19.01% | -30.23% | 0.629 | 1.242 |
| 3-leg +qdt equal qtly | 0.333 / 0.333 / 0.333 | 19.10% | -32.09% | 0.595 | 1.196 |

Inverse-vol over-weights low_pe to ~50% because its full-period vol is artificially
low (cash-period compression). That's the main reason 3-leg invvol drops CAGR
from 18.79% to 12.45%.

QDT as a third leg makes things strictly worse: high CAGR contribution but its
-47% MDD bleeds into the ensemble, dropping Calmar from 0.789 to 0.629.

---

## Weight sensitivity (3-leg full-period)

Splitting `(1 - w_lowpe)` between eod_b/eod_t at their inverse-vol ratio (55/45):

| w_lowpe | w_eodb | w_eodt | CAGR | MDD | Cal | Sharpe |
|---:|---:|---:|---:|---:|---:|---:|
| 0.000 | 0.555 | 0.445 | 18.79% | -23.81% | 0.789 | **1.281** |
| 0.100 | 0.500 | 0.400 | 17.54% | -22.03% | 0.796 | 1.274 |
| 0.200 | 0.444 | 0.356 | 16.27% | -20.21% | 0.805 | 1.262 |
| **0.250** | **0.416** | **0.334** | **15.64%** | **-19.31%** | **0.810** | **1.253** |
| 0.300 | 0.389 | 0.311 | 15.00% | -18.45% | 0.813 | 1.242 |
| 0.333 | 0.370 | 0.297 | 14.58% | -17.87% | 0.816 | 1.232 |
| 0.500 | 0.278 | 0.222 | 12.43% | -14.99% | 0.829 | 1.161 |

**Pareto frontier:** Sharpe is monotonically decreasing in `w_lowpe`; Calmar is
monotonically increasing. There is **no weight that improves both** vs the 2-leg.

The "sweet spot" depends on which axis matters:
- **Sharpe-priority:** w_lowpe = 0 (2-leg). Best Sharpe 1.281.
- **Mild defensive tilt:** w_lowpe = 0.20-0.25. Sharpe -0.02 to -0.03, MDD -3.6 to -4.5pp,
  Cal +0.02. A reasonable compromise if users care more about quoteable drawdown
  numbers than Sharpe.

---

## Modern-window check (2018-2026)

For comparison only — 2018+ Sharpe is upward-biased because both breakout
strategies happened to outperform in that window.

| Variant | CAGR | MDD | Cal | Sharpe |
|---|---:|---:|---:|---:|
| Solo eod_b modern | 18.27% | -26.75% | 0.683 | 1.178 |
| Solo eod_t modern | 28.26% | -25.95% | 1.089 | 1.489 |
| Solo low_pe modern | 12.26% | -12.08% | 1.016 | 1.002 |
| 2-leg eod_b+eod_t invvol qtly modern | 22.91% | -23.83% | 0.961 | **1.519** |
| 2-leg eod_b+eod_t 50/50 qtly modern | 23.52% | -23.65% | 0.994 | **1.537** |
| 3-leg +low_pe invvol qtly modern | 18.62% | -16.40% | 1.136 | 1.483 |
| 3-leg +low_pe equal qtly modern | 19.88% | -17.79% | 1.118 | 1.518 |
| 2-leg eod_b+low_pe invvol qtly modern | 17.18% | -14.60% | 1.177 | 1.365 |

Apples-to-apples on the modern window: **2-leg eod_b+eod_t still wins on Sharpe
(1.537)**. low_pe only helps in the eod_b-only 2-leg case; once eod_t is already
diversifying eod_b, adding low_pe is redundant for Sharpe purposes.

---

## Key findings (durable)

1. **The 2-leg eod_b+eod_t invvol champion stays.** Adding any tested third leg
   reduces full-period Sharpe. low_pe is the only candidate that improves
   full-period Calmar; QDT/TV/FC actively hurt both metrics due to deeper MDDs.

2. **Inverse-vol misallocates when one leg has cash periods.** low_pe holds cash
   pre-2018 → vol compressed to 7.41% → invvol weight inflates to 50%. For mixed
   data-coverage legs, equal-weight or hand-tuned weights are safer than naive
   inverse-vol.

3. **Adding a defensive sleeve is a Sharpe/Calmar trade.** No weight beats the
   2-leg on both axes simultaneously; pick which axis matters.

4. **low_pe's diversification value is real (corr 0.39/0.45)** but the depressed
   full-period CAGR offsets the diversification gain. If FMP NSE fundamentals
   pre-2018 ever improve (or we synthesize with another data source), low_pe
   could become a viable third leg without the cash-drag.

5. **Modern-window Sharpe is upward-biased and not the right benchmark for live
   deployment.** All strategies in the suite happen to be tuned on 2010+ but
   benefit from the post-2018 bull. Use full-period numbers for go-live decisions.

---

## Recommendation

**Keep current champion**: 2-leg `eod_b + eod_t invvol qtly`, Sharpe 1.281.

**Optional defensive variant** (if MDD reduction is valued): w_lowpe = 0.25
(eod_b 41.6% / eod_t 33.4% / low_pe 25%). Locks in Cal 0.810 / MDD -19.31% at
the cost of -2.2% Sharpe. Document but don't promote.

**Future work** (out of scope for this experiment):
- Pre-2018 low_pe data backfill (alternative source) — unlocks low_pe as a real
  full-period leg, would warrant re-running this experiment.
- Per-rebalance adaptive weighting — could mitigate the inverse-vol misallocation
  by recomputing weights on trailing-window vol that excludes cash periods.

---

## Files produced

| File | Purpose |
|---|---|
| `strategies/low_pe/config_champion_full.yaml` | low_pe champion params on full 2010+ |
| `scripts/run_lowpe_full.py` | one-off runner for above |
| `results/low_pe/champion_full.json` | resulting equity curve |
| `strategies/ensembles/eod_eodt_lowpe_{invvol,equal}_quarterly_{full,modern}/` | 4 new ensemble configs |
| `results/ensembles/eod_eodt_lowpe_*.json` | 4 result files |
| `strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md` | this writeup |

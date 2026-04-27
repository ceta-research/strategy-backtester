# Session Handover — 2026-04-27 (pt2: Ensemble Runner)

**Session focus:** Build production-quality ensemble runner (Phase 1-6) and find the best 2010-current ensemble.
**Engine:** post-audit `fbcd36a+` (protected files unchanged, 0 diff verified).
**Continues from:** [`2026-04-27_handover.md`](2026-04-27_handover.md) (eod_breakout champion promotion).
**Result:** 9 commits shipped to `origin/main`. Ensemble runner complete; best 2010+ ensemble identified.

---

## TL;DR

- **Ensemble runner shipped** at `lib/ensemble_curve.py` + `scripts/run_ensemble.py`. Phase 1-6 complete: alignment, combine, rebalance, inverse-vol weighting, drawdown attribution, correlation matrix, Sharpe sensitivity, 32 unit tests, full guide doc.
- **Best 2010-current ensemble found**: eod_breakout + eod_technical, inverse-vol weighted (~56/44), quarterly rebalance. **CAGR 18.79% / MDD -23.81% / Calmar 0.789 / Sharpe 1.281**. Within 0.005 of Sharpe-sweep optimum.
- **Adding more legs degrades the ensemble**. Despite QDT's lowest correlation (0.457), its -47% solo MDD baggage drags ensemble drawdowns deeper (-23.81% → -30.23%). Drawdown depth dominates correlation for ensemble selection.
- **All 9 commits pushed to `origin/main`**. 32 tests passing.

---

## What was done

### 1. Ensemble runner — Phase 1 (set-and-forget)

`lib/ensemble_curve.py`: alignment (intersection mode), `combine_curves` treating each leg as a return stream `v[t]/v[0]`, `build_ensemble_curve` orchestrator. `scripts/run_ensemble.py`: CLI runner with leg-leaderboard stdout. Config schema in `strategies/ensembles/{name}/config.yaml`.

Acceptance test reproduced the prototype 50/50 numbers exactly (CAGR 19.44%, MDD -21.34%, Cal 0.911 on eod_breakout-modern + low_pe, 2018-2026).

Edge cases tested: weight sum != 1, negative weights, frequency mismatch, empty intersection, partial overlap, 70/30 analytic returns.

**Discrepancy noted:** Prototype writeup quoted Sharpe 1.538; that was `CAGR/vol` (no risk-free subtraction). The standard rf-adjusted geometric Sharpe is 1.380 — runner uses the standard.

Commit: `fc3d0f2`.

### 2. Phase 2 — Periodic rebalancing

Added `rebalance_combined_curve()` and `_period_key()`. Modes: `none | monthly | quarterly | annual`. Convention: rebalance fires at first day of new period (e.g. Feb 1 for monthly).

Real-world validation on eod+low_pe modern:

| Mode | CAGR | MDD | Cal | Sharpe |
|---|---:|---:|---:|---:|
| none | 19.44% | -21.34% | 0.911 | 1.380 |
| annual | 18.70% | -16.91% | 1.106 | 1.399 |
| quarterly | 18.65% | -17.19% | 1.085 | 1.399 |
| monthly | 18.50% | -17.35% | 1.066 | 1.389 |

Set-and-forget overstates Calmar by ~19% because eod's drift pushes effective weight to ~70%. Friction warning auto-emitted (~25bps/yr quarterly).

Commit: `fc3d0f2` (bundled with Phase 1).

### 3. Phase 3 — Inverse-vol weighting

`compute_inverse_vol_weights(curves, lookback_days)`: weights ∝ 1/vol_i. `resolve_weights()` dispatch on `fixed | inverse_vol | risk_parity`. risk_parity raises NotImplementedError (deferred to Phase 3.5; iterative ERC).

Computed weights on eod+low_pe: (0.381, 0.619) — **matches prototype prediction exactly**. With quarterly rebalance: CAGR 17.18% / MDD -14.60% / **Cal 1.177** / Sharpe 1.365.

**Honest finding:** inverse-vol Sharpe < 50/50 quarterly Sharpe (1.365 vs 1.399). Why? eod_breakout has higher solo Sharpe (1.349) than low_pe (1.002), so cutting its weight reduces ensemble Sharpe. Inverse-vol optimizes risk parity, not Sharpe. The real win is Calmar (1.177 — best of all variants).

In-sample bias warning auto-emitted when `weight_lookback_days: null`.

Commit: `e5dcd8a`.

### 4. Phase 4 — Drawdown attribution

`compute_leg_navs()`: walks per-leg NAV trajectories (rebalance-aware). `attribute_drawdown()`: finds worst peak-to-trough on combined curve and decomposes into per-leg contributions. **Math invariant**: sum(leg contributions) = ensemble_drawdown.

eod+low_pe attribution showed 2024-2025 drawdown driven by eod_breakout: -19.66% vs low_pe -1.68% (50/50 set-and-forget). Inverse-vol quarterly: -9.99% vs -4.61% — quarterly rebalance + smaller eod weight cut its DD share by ~50%.

Commit: `c06f844`.

### 5. Phase 5 — Correlation matrix + Sharpe sensitivity

`compute_correlation_matrix(curves)`: pairwise Pearson corr of period returns. `sharpe_sensitivity_2leg(curves, n_grid=21)`: sweeps w1 ∈ [0,1] in 21 steps, reports peak vs inverse-vol points.

eod+low_pe: corr 0.532 (matches prototype). Sharpe peak 1.408 at w=[0.60, 0.40]. Inverse-vol's [0.38, 0.62] sits at 1.365 — confirms inverse-vol is anti-Sharpe in this case. Sweep is flat near peak.

Commit: `99f392f`.

### 6. Phase 6 — Tests + ENSEMBLE_GUIDE.md

`tests/test_ensemble_curve.py`: 32 unittest cases covering combine, align, rebalance, inverse-vol, resolve_weights, leg NAVs, attribution, correlation, sensitivity, constants. All passing (`python -m unittest tests.test_ensemble_curve`).

`docs/ENSEMBLE_GUIDE.md`: full reference — when to use, config schema, output format, math contract, worked example, known limitations.

Linked from `docs/STATUS.md` doc map.

Commit: `26a8bff`.

### 7. 2010-current ensemble exploration

The eod+low_pe ensemble auto-truncates to 2018+ (low_pe has no pre-2018 FMP fundamentals). To get a true 2010-current ensemble, scouted full-period champion result files: eod_breakout, eod_technical, quality_dip_tiered, trending_value, factor_composite (all 2010-2026, 5922 daily points).

**Pre-built correlation matrix** to inform leg selection:

| | eod_b | eod_t | QDT | TV | FC |
|---|---:|---:|---:|---:|---:|
| eod_b | 1.000 | 0.590 | 0.457 | 0.501 | 0.499 |
| eod_t | | 1.000 | 0.576 | 0.641 | 0.637 |
| QDT | | | 1.000 | 0.633 | 0.650 |
| TV | | | | 1.000 | 0.695 |
| FC | | | | | 1.000 |

eod_breakout is least correlated (0.457-0.590) with everything else — prime ensemble anchor.

**Tested 5 ensemble configurations:**

| Variant | Weights | CAGR | MDD | Calmar | Sharpe |
|---|---|---:|---:|---:|---:|
| eod+eodt 50/50 qtly | 0.50/0.50 | 18.90% | -23.65% | 0.799 | 1.272 |
| **eod+eodt invvol qtly** | **0.56/0.44** | **18.79%** | **-23.81%** | **0.789** | **1.281** ⬆ |
| eod+eodt+QDT 33/33/33 | equal | 19.10% | -32.09% | 0.595 | 1.196 |
| eod+eodt+QDT invvol | 0.41/0.33/0.25 | 19.01% | -30.23% | 0.629 | 1.242 |
| eod+TV invvol qtly | 0.60/0.40 | 17.74% | -27.86% | 0.637 | 1.147 |

**Key lesson: drawdown depth dominates correlation for ensemble selection.** Despite QDT having the lowest correlation to eod_breakout (0.457), its -47% solo MDD baggage drags ensemble drawdowns deeper. The "low correlation = automatic diversification" rule of thumb breaks when leg MDDs differ significantly.

Sharpe sweep optimum on eod+eodt: 1.286 at w=[0.65, 0.35]. Inverse-vol's 0.56/0.44 sits at 1.281 — within 0.005 of optimum.

Commits: `41e9178`, `4b3cba8`.

---

## Final ensemble leaderboard

### Best 2010-current ensemble (this session)

```
Config:    strategies/ensembles/eod_eodt_invvol_quarterly_full/config.yaml
Window:    2010-01-01 -> 2026-03-19  (16.2 years, 5922 daily points)
Weights:   eod_breakout 56% / eod_technical 44% (inverse-vol)
Rebalance: quarterly
CAGR:      18.79%  (vs eod_t solo 19.63% / eod_b solo 17.68%)
MDD:       -23.81% (vs solo -25.95% / -26.75%)
Calmar:    0.789   (vs solo 0.757 / 0.661)
Sharpe:    1.281   (vs solo 1.067 / 1.183) ← Sharpe lifts above BOTH solos
Vol:       13.11%
WorstYear: -13.39%
```

### Best 2018+ ensemble (modern)

```
Config:    strategies/ensembles/eod_lowpe_invvol_quarterly/config.yaml
Window:    2018-01-01 -> 2026-03-19
Weights:   eod_breakout-modern 38% / low_pe-modern 62% (inverse-vol)
Rebalance: quarterly
CAGR:      17.18%
MDD:       -14.60%  ← best MDD of any variant
Calmar:    1.177    ← best Calmar of any variant
Sharpe:    1.365
```

---

## Pending / deferred work

### Pending — proposed next session (in priority order)

1. **C: Apply regime+holdout methodology to `eod_technical`** (~3-4 hrs)
   - Top by CAGR (19.63%) but Sharpe 1.067, MDD -25.95% — likely has the same regime fragility eod_breakout had.
   - Methodology proven: 1152-config R2 sweep on 2010-2024 only, then NIFTYBEES SMA(100) regime gate sweep.
   - Could lift eod_technical Sharpe to 1.2+, which would feed back into the ensemble (current ensemble Sharpe 1.281 → potentially 1.35+).
   - Best ROI of all pending items.

2. **B: Update `docs/LIVE_TRADING_INTEGRATION.md`** (~30-60 min)
   - Add ensemble-as-deployment option (eod_breakout + eod_technical, invvol qtly).
   - Document daily breadth check + quarterly rebalance ops.
   - Friction estimate ~25bps/yr quarterly.
   - Decide kill criterion (e.g. -30% MDD pause).

3. **D: Investigate eod_breakout solo Sharpe discrepancy** (~15 min)
   - `OPTIMIZATION.md` says 1.334; runner reads 1.183 from `champion.json`. Likely a recompute or file-version issue.
   - Verify by rerunning champion config and comparing.

4. **C continued: Apply regime+holdout to other strategies** (~3-4 hrs each)
   - `quality_dip_tiered`: -47% MDD baggage; ppi=1, sector caps, tsl=6% never tested
   - `trending_value`: lb=1 month vs O'Shaughnessy's 6-month never tested; modern-window champion re-pick
   - `low_pe`: already most defensive; regime exit may further improve
   - Each lift might enable new ensemble combinations.

### Deferred (carried forward from prior sessions)

- 49 stale results files (pre-`ba95a05` charges) for cross-exchange — not blocking
- Regression snapshot re-pinning (data drift)
- R4c (cross-data) + R4d (cross-exchange) for 6 newer COMPLETE strategies (`factor_composite`, `quality_dip_tiered`, `trending_value`, `eod_technical`, `low_pe`, `ml_supertrend`)
- 1 P1 + 2 P2 + 6 P3 audit hygiene items (see `archive/audit-2026-04/AUDIT_CHECKLIST.md`)
- Add deflated Sharpe to `OPTIMIZATION_RUNBOOK.md` as standard
- Add same-bar bias check to `OPTIMIZATION_PROMPT.md` (gap_fill lesson)
- Investigate `index_breakout` engine-vs-standalone discrepancy

### Engine-level fixes (deferred)

- **Slippage = ₹0** in engine pipeline (all signal generators). Real CAGR ~0.3pp lower than reported.
- **All exit_reason = "natural"** in eod_breakout signal gen.

### Ensemble runner — Phase 3.5 / 7+ (deferred)

- Per-rebalance adaptive weighting (use trailing window, recompute at each rebalance)
- Iterative ERC solver (`risk_parity` weighting mode)
- `config_path` rerun mode with cache invalidation on mtime
- Friction modeling per leg (~5-10bps per turn)
- `union_ffill` alignment for differently-windowed legs
- N-leg Sharpe sensitivity (currently 2-leg only)

---

## Key learnings

1. **Drawdown depth dominates correlation for ensemble selection.** A leg with -47% solo MDD will dominate an ensemble's drawdowns proportionally to its weight, regardless of how decorrelated it is. The "low correlation → diversification" rule of thumb breaks when MDDs differ significantly.

2. **Inverse-vol ≠ Sharpe-optimal.** Inverse-vol optimizes risk parity (each leg contributes equal vol to the ensemble). When legs have unequal solo Sharpes, the Sharpe-optimal weighting puts MORE weight on the higher-Sharpe leg, not less. Both observed in eod+low_pe (inverse-vol Sharpe 1.365 vs 50/50 1.399) and eod+eodt (Sharpe peak at w=[0.65, 0.35] vs invvol [0.56, 0.44]).

3. **eod_breakout is the best ensemble anchor in our suite.** Lowest correlation (0.46-0.59) to every other 2010+ champion. Pairs well with anything moderately decorrelated.

4. **Set-and-forget biases optimistically.** Without rebalancing, the winning leg's effective weight grows monotonically. Calmar overstated by ~19% on the eod+low_pe case. Quarterly is the honest baseline.

5. **Two technical strategies still diversify.** Even though eod_breakout and eod_technical share the breakout-family mechanism (corr 0.590), their 50/50 quarterly Sharpe (1.272) lifts above BOTH solo Sharpes (1.183 / 1.067). The 2018-2019 drawdown was a system-wide breakout failure (both legs lost ~24%) — limitation of mechanism similarity, but still a net improvement.

6. **Math/serializer invariants are the most testable property.** All 32 unit tests pass; the most valuable ones are the invariants: leg NAV sum equals combined curve at every timestep, attribution sums to ensemble drawdown, correlation diagonals = 1.0, sweep endpoints match solo Sharpes. These caught one bug (deterministic synthetic data → vol=0 → Sharpe blowup) early.

---

## Files modified this session

### Created (lib/ + scripts/ + tests/)

- `lib/ensemble_curve.py` — alignment, combine, rebalance, inverse-vol, leg NAVs, attribution, correlation, sensitivity (~600 lines)
- `scripts/run_ensemble.py` — CLI runner with stdout report and JSON output (~400 lines)
- `tests/test_ensemble_curve.py` — 32 unit tests
- `docs/ENSEMBLE_GUIDE.md` — full reference

### Created (strategies/ensembles/)

7 ensemble configs:
- `eod_lowpe_5050/` (Phase 1 baseline, set-and-forget)
- `eod_lowpe_5050_quarterly/` (Phase 2 quarterly)
- `eod_lowpe_invvol_quarterly/` (Phase 3 inverse-vol)
- `eod_eodt_5050_quarterly_full/` (first 2010+ ensemble)
- `eod_eodt_invvol_quarterly_full/` (**winner — best 2010+**)
- `eod_eodt_qdt_equal_quarterly/` (3-leg, fails)
- `eod_eodt_qdt_invvol_quarterly/` (3-leg invvol, still inferior)
- `eod_tv_invvol_quarterly_full/` (alternative pairing, fails)

### Modified

- `docs/STATUS.md` — added ENSEMBLE_GUIDE.md to doc map

### Results files (gitignored)

8 ensemble result JSONs in `results/ensembles/` (~200-400KB each).

---

## Commits this session (all pushed to `origin/main`)

```
4b3cba8 ensemble: 2010-current ensemble exploration (4 new configs)
41e9178 ensemble: eod_breakout + eod_technical 50/50 quarterly, full period 2010-2026
26a8bff ensemble runner: Phase 6 — tests + ENSEMBLE_GUIDE.md
99f392f ensemble runner: Phase 5 — correlation matrix + Sharpe sensitivity
c06f844 ensemble runner: Phase 4 — drawdown attribution
e5dcd8a ensemble runner: Phase 3 — inverse-vol weighting
fc3d0f2 ensemble runner: lib/ensemble_curve + scripts/run_ensemble (Phase 1+2)
ba1c208 eod_breakout: promote regime+holdout champion (2026-04-27)   [from prior session]
8943a5e docs cleanup: archive audit-era + pre-engine docs            [from prior session]
```

---

## Fast-start (next session)

```bash
cd /Users/swas/Desktop/Swas/Kite/ATO_SUITE/strategy-backtester

# Verify clean state
git status
git log --oneline -10

# Read current state
cat docs/STATUS.md
cat docs/sessions/2026-04-27_pt2_ensemble_handover.md  # this file
cat docs/ENSEMBLE_GUIDE.md  # if working on ensemble extensions

# Verify ensemble winner reproduces
source /Users/swas/Desktop/Swas/Kite/ATO_SUITE/.venv/bin/activate
python scripts/run_ensemble.py \
  --ensemble strategies/ensembles/eod_eodt_invvol_quarterly_full/config.yaml
# Expected: CAGR 18.79%, MDD -23.81%, Cal 0.789, Sharpe 1.281

# Run all 32 unit tests
python -m unittest tests.test_ensemble_curve
# Expected: OK in ~5ms

# Pick direction:
#   1. Apply regime+holdout to eod_technical (highest expected value, ~3-4hr)
#   2. Update LIVE_TRADING_INTEGRATION.md (~30-60min)
#   3. Investigate eod_breakout Sharpe discrepancy (~15min)
#   4. Apply regime+holdout to QDT / trending_value / low_pe
```

---

## Recommended opening for next session

**Highest expected value:** Apply regime+holdout methodology to `eod_technical` (option C above, ~3-4 hours).

Why:
- Top-CAGR strategy (19.63%) with mediocre Sharpe (1.067) — strongly suggests regime fragility, same as eod_breakout pre-promotion.
- Methodology fully proven on eod_breakout: 1152-config R2 sweep on 2010-2024, NIFTYBEES SMA(100) regime gate sweep, holdout-validated promotion.
- A Sharpe lift on eod_technical feeds back into the ensemble: current 1.281 → potentially 1.35-1.40.
- Could also unlock new ensemble combinations if eod_technical's MDD shrinks meaningfully.

**Alternative low-risk option:** Update `docs/LIVE_TRADING_INTEGRATION.md` for the new ensemble winner (~30-60 min). Critical if any chance of going live.

**Quick win:** Investigate eod_breakout Sharpe discrepancy (1.334 in OPTIMIZATION.md vs 1.183 from `champion.json`). Could surface a recompute or file-version inconsistency.

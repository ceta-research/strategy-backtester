# quality_dip_tiered Optimization

**Strategy:** Quality-filter + multi-tier DCA dip-buy. Each tier generates separate order at
progressively deeper dip from rolling peak.
**Signal file:** `engine/signals/quality_dip_tiered.py`
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: COMPLETE (R5 promotion 2026-04-28 pt2)

- [x] Round 0: Baseline
- [x] Round 1: Sensitivity (3 sub-sweeps, 81 configs)
- [x] Round 2: Full cross (144 configs)
- [x] Round 3: Robustness (108 configs, 10/10 PASS)
- [x] Round 4a: OOS (2020-2026)
- [x] Round 4b: Walk-forward (5 folds, std Cal 0.494 borderline PASS)
- [ ] Round 4c: Cross-data-source (deferred per precedent)
- [ ] Round 4d: Cross-exchange (deferred)
- [x] **Round 5: Exit/concentration overlay refit (72 configs, R5/R5b/R5c)**

## Champion (current, post-R5 promotion 2026-04-28)

| Period | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|------|-----|--------|--------|--------|
| **Full (2010-2026)** | **17.73%** | **-39.30%** | **0.451** | **0.759** | 535 |

**Params:** `yr=2, n_tiers=2, tier_mult=1.5, base_dip=4, peak=30, regime=NIFTYBEES>SMA200,
tsl=10, max_hold=504, sort=top_gainer, pos=15, ppi=2`

### Promotion history

| Date | Champion | CAGR | MDD | Cal | Sharpe |
|---|---|---:|---:|---:|---:|
| 2026-04-24 | tsl=8, ppi=3 | 18.39% | -47.37% | 0.388 | 0.761 |
| **2026-04-28** | **tsl=10, ppi=2 (current)** | **17.73%** | **-39.30%** | **0.451** | **0.759** |

### Why the new champion is better (Pareto-relevant)

| Metric | Old | New | Δ |
|---|---:|---:|---:|
| CAGR | 18.39% | 17.73% | -0.66pp |
| MDD | -47.37% | -39.30% | **+8.07pp** |
| Calmar | 0.388 | **0.451** | **+16.2%** |
| Sharpe | 0.761 | 0.759 | -0.002 (noise) |

The MDD reduction is meaningful; Sharpe is unchanged within noise. CAGR cost
of -0.66pp is acceptable for live deployment where deep drawdowns are
operationally costly.

### Mechanism

- **ppi=3 → ppi=2**: cuts the third-tier DCA re-entry that was deepening drawdowns
  while contributing diminishing CAGR. 582 trades → 535 trades.
- **tsl=8 → tsl=10**: gives positions room to breathe past the 8-10% bracket
  where many false stops occurred. Trade count reduction of 47 (8% fewer)
  reflects stops avoided.

### 3-leg ensemble check

Re-ran QDT-as-third-leg ensembles after R5 promotion:

| Variant | CAGR | MDD | Cal | Sharpe |
|---|---:|---:|---:|---:|
| 2-leg eod_b+eod_t invvol qtly (champion) | 18.79% | -23.81% | 0.789 | **1.281** |
| 3-leg +QDT-pre-R5 invvol qtly | 19.01% | -30.23% | 0.629 | 1.242 |
| 3-leg +QDT-post-R5 invvol qtly | 18.77% | -25.88% | **0.725** | 1.230 |
| 3-leg +QDT-post-R5 equal qtly | 18.80% | -26.53% | 0.709 | 1.188 |

QDT's better solo MDD lifts the 3-leg's Calmar from 0.629 → 0.725 (+15%) but
the 2-leg champion still dominates on both Sharpe and Cal. Confirms the prior
N-leg conclusion: 2-leg eod_b+eod_t stays the champion.

### Backup of pre-R5 config

`config_champion_pre_r5.yaml` retained for traceability.

---

## Round 5: Exit/concentration overlay refit (72 configs)

**Goal:** shave the 4.5-year MDD without losing CAGR. Hypothesis: deep MDD
driven by holding losers (max_hold=504d) AND DCA re-entries (ppi=3) deepening
drawdowns; tightening either should help.

### R5 sweep (36 configs): tsl × max_hold × ppi

Grid: tsl[6,8,10] × max_hold[126,252,378,504] × ppi[1,2,3] = 36.

Top 5 by Calmar:

| config | tsl | max_hold | ppi | CAGR | MDD | Cal | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| `1_1_12_2` ⭐ | 10 | 504 | 2 | 17.73% | -39.30% | **0.451** | 0.759 |
| `1_1_8_3` (old champ) | 8 | 504 | 3 | 18.39% | -47.37% | 0.388 | 0.761 |
| `1_1_8_2` | 8 | 504 | 2 | 16.86% | -46.97% | 0.359 | 0.698 |
| `1_1_1_2` | 6 | 126 | 2 | 15.01% | -44.65% | 0.336 | 0.581 |

**Finding:** The (tsl=10, max_hold=504, ppi=2) config is the only Pareto improver
in the sweep. Cleanly dominant by Calmar, near-equal Sharpe, modest CAGR cost.

### R5b zoom (9 configs): looser tsl × max_hold

Grid: tsl[10,12,15] × max_hold[378,504,720] at ppi=2 = 9.

Best: same `tsl=10, max_hold=504, ppi=2` (Cal 0.451). Looser tsl (12, 15) and
extreme max_hold (720) all produce lower Calmar. Confirms the R5 winner is at
a local optimum.

### R5c entry recheck (27 configs): yr × dip × peak at new exit

Grid: yr[1,2,3] × base_dip[3,4,5] × peak_lookback[20,30,45] at the new
(tsl=10, max_hold=504, ppi=2) baseline = 27.

Best: same yr=2, dip=4, peak=30 (matches prior R3 champion entry params).
Confirms entry-side champion is independent of exit refit.

### R5 files

| File | Purpose |
|---|---|
| `config_round5_overlay.yaml` | R5 sweep: tsl × max_hold × ppi |
| `config_round5b_zoom.yaml` | R5b zoom: looser tsl × max_hold at ppi=2 |
| `config_round5c_entry.yaml` | R5c: yr × dip × peak recheck |
| `results/quality_dip_tiered/round5_overlay.json` | R5 result (36 configs) |
| `results/quality_dip_tiered/round5b_zoom.json` | R5b result (9) |
| `results/quality_dip_tiered/round5c_entry.json` | R5c result (27) |

### Untested angles (not pursued in R5)

- **Sector concentration cap** would require engine-level work in the signal
  generator. Not pursued because R5 already achieved meaningful Pareto gain
  via existing parameters; sector cap is a deeper refactor for marginal upside.
- **`force_exit_on_regime_flip`** support is in `engine/signals/quality_dip_tiered.py`
  via the regime gate but only as an entry-block; full force-exit was not added
  because DCA strategies that hold averaged-down losers would dump positions
  exactly at the bottom under force-exit. Mechanism-incompatible with the
  strategy thesis.
- **Lower `peak_lookback_days` (e.g. 15)** untested. R3 confirmed 30 > 20 > 45.

---

## Original (pre-R5) champion details below


### Walk-forward (5 folds, 3-yr rolling)

| Fold | CAGR | MDD | Calmar | Sharpe |
|------|------|-----|--------|--------|
| 2010-2013 |  6.58% | -29.0% |  0.227 |  0.26 |
| 2013-2016 | 25.10% | -16.9% |  1.486 |  1.14 |
| 2016-2019 | 13.76% | -38.5% |  0.357 |  0.57 |
| 2019-2022 | 22.06% | -27.6% |  0.801 |  1.00 |
| 2022-2025 | 24.13% | -29.5% |  0.818 |  0.84 |

**Positive folds:** 5/5 (100%)
**Mean Calmar:** 0.738  **Std Calmar:** 0.494 → **PASSES** (borderline, threshold 0.5)

### OOS / Plausibility caveats

- OOS Cal (0.718) / IS Cal (0.388) = **1.85×** — near runbook 2× warning. Likely regime-specific
  (2020-2026 was a strong period for Indian mid-cap dip-buy).
- Fold 2013-2016 Cal 1.486 is exceptional — midcap bull (ICICI-direct's "banking scandal 2017"
  period was a sharp dip-buy's dream). Not a bug; real regime.
- Full-period CAGR 18.39% < 20% threshold. OOS CAGR 27.18% is above 20% but is a single recent
  period, not the optimization target.
- **Win rate 70.8%**: runbook flag ("Win rate > 65% with high CAGR: Check for look-ahead bias").
  Investigated: no look-ahead. `walk_forward_exit(require_peak_recovery=True)` structurally
  produces high win rates — it holds losers until peak recovery (or TSL/max_hold). Entries
  use `next_open` (day+1), exits iterate only forward from entry_idx. Pattern is a known
  signature of mean-reversion + DCA, not a bug.

### Deflated Sharpe

334 configs tested, 192 months. SR_observed = 0.761.
Var(SR) = (1 + 0.5·0.761²)/192 = 0.0067, √Var = 0.082, Z(1 − 1/334) ≈ 2.75.
SR_deflated ≈ 0.761 − 0.225 = **0.536** → strongly above 0.3 threshold.

### Additional metrics

- Sortino 1.077, vol 21.5%, profit factor 2.352
- MDD duration **1143 days (~4.5 years)** — long drawdown
- Time-in-market 99.9% (NIFTYBEES regime filter rarely triggers in mostly-bullish 2010-2026 NSE)
- Avg hold 139d (~6.5 mo); best year +87% / worst year -33%

## vs baseline

CAGR 5.38% → 18.39% (+13pp); Calmar 0.104 → 0.388 (+3.7×); MDD -51.8% → -47.4% (similar deep).

## vs qdb (single-tier sibling)

| Metric | qdb | qdt |
|---|---|---|
| Full CAGR | 11.63% | **18.39%** |
| Full Calmar | 0.307 | **0.388** |
| OOS Calmar | 0.581 | 0.718 |
| WF Std Cal | 0.530 (FRAGILE) | 0.494 (borderline PASS) |

DCA averaging works. Two tiers at 4% and 6% dip beat single-tier at 5%.

## Key findings

- **DCA thesis works on NSE.** n_tiers=2 (tier1 4%, tier2 6%) beats single-entry qdb decisively.
  n_tiers=3 is close but adds trade overhead.
- **Tighter dip threshold** (4% vs qdb's 5%) and shorter peak window (30d vs 63d) generate
  more frequent entries — DCA works better with smaller dips.
- **Looser quality filter** (yr=2) beats stricter (yr=3) — more candidates = more DCA opportunities.
- **Tight trailing stop (8%)** wins — cuts losers fast while DCA averages entry down.
- **peak_lookback=30 >> 63**: marginal 12.03% vs 9.66%. Shorter window catches more dips.
- **pos=15** with ppi=3 (up to 45 concurrent orders) gives effective diversification.

## Parameters

| Param | Baseline | Champion | Notes |
|-------|----------|----------|-------|
| `consecutive_positive_years` | 3 | **2** | Looser = larger pool |
| `n_tiers` | 1 | **2** | DCA sweet spot |
| `tier_multiplier` | 1.5 | **1.5** | 1.3-1.5 similar |
| `base_dip_threshold_pct` | 5 | **4** | Tighter = more entries |
| `peak_lookback_days` | 63 | **30** | Shorter captures more dips |
| `rescreen_interval_days` | 63 | **63** | Not swept |
| `regime_instrument` | NIFTYBEES | **NIFTYBEES** | Mostly bull period = rarely blocks |
| `regime_sma_period` | 200 | **200** | Not swept |
| `trailing_stop_pct` | 15 | **8** | Tight stop cuts losers |
| `max_hold_days` | 504 | **504** | Not swept |
| `order_sorting_type` | top_gainer | **top_gainer** | Best |
| `max_positions` | 15 | **15** | 20 marginal edge |
| `max_positions_per_instrument` | 3 | **3** | Plateau 3-5. ppi=1 is conservative alternative |

## ppi sensitivity (post-hoc check)

ppi wasn't explicitly swept in R1-R3 (fixed at 3). A post-hoc check confirms ppi=3 is well-chosen:

| ppi | CAGR | MDD | Calmar | Trades | Win rate |
|---:|---:|---:|---:|---:|---:|
| 1 | 12.79% | -40.7% | 0.314 | 547 | 69.1% |
| 2 | 16.86% | -47.0% | 0.359 | 577 | 70.9% |
| **3** | **18.39%** | **-47.4%** | **0.388** | 582 | 70.8% |
| 5 | 18.46% | -47.4% | 0.389 | 570 | 71.0% |

Note: total trades barely change (547→582) but CAGR jumps 12.8%→18.4%. The extra trades enabled
by ppi>1 are the DCA re-entries — high-alpha (averaging down a quality stock then exiting on
recovery). ppi=1 is a valid conservative alternative: trades 0.074 Cal for 7pp lower MDD. The
default champion uses ppi=3 as the Calmar optimum.

## Rounds

### R0: Baseline — CAGR 5.38%, Cal 0.104, MDD -51.8%, 115k orders

Params: yr=3, n_tiers=1, dip=5, peak=63, tsl=15, regime=NIFTYBEES>200, pos=15

### R1: Sensitivity

- **R1a (27 configs)**: n_tiers × dip × tsl. Best 13.11%/Cal 0.309 (n_tiers=2, dip=5, tsl=10).
  **DCA wins**: n_tiers=2 > 1 > 3. Baseline was n_tiers=1; crossing revealed the improvement.
- **R1b (18 configs)**: peak × yr × tier_mult. Best 12.95%/Cal 0.314 (yr=3, tm=1.5, peak=30).
  peak=30 best, peak=120 worst. yr=3 > yr=4 (too strict).
- **R1c (36 configs)**: tsl × pos × sort. Best 13.44%/Cal 0.316 (tsl=10, pos=20, top_gainer).

### R2: Full cross — 144 configs, ~19 min

Grid: yr[2,3] × tm[1.3,1.5] × dip[4,5,7] × peak[30,63] × tsl[8,10,12] × pos[15,20]

Best CAGR: 18.75%/Cal 0.368 (yr=2, tm=1.3, dip=4, peak=30, tsl=8, pos=20)
Best Calmar: 18.39%/Cal 0.388 (yr=2, tm=1.5, dip=4, peak=30, tsl=8, pos=15)

Marginal: yr=2>3, peak=30>63, dip=4>5>7 (tighter dip wins), tsl=8 best, pos=20 marginal edge.

### R3: Fine grid — 108 configs, 10/10 PASS

Extended edges: dip[3,4,5], peak[20,30,45]. Dip=4 confirmed peak (3 slightly worse),
peak=30 confirmed peak (20 and 45 weaker). Champion unchanged: 18.39%/Cal 0.388.

### R4a OOS (2020-2026)
CAGR 27.18%, MDD -37.8%, Cal 0.718. OOS > IS (regime-specific, not overfitting).

### R4b Walk-forward
5/5 positive, Mean Cal 0.738, Std 0.494. PASSES (borderline).

Relationship to `quality_dip_buy` (qdb):
- qdt with `n_tiers=1` ≈ qdb (single-tier dip buy)
- qdt's novelty is DCA: multiple tiers at progressively deeper dips, each a separate order
- qdb champion (for reference): yr=3, dip=5%, peak=63, regime=NIFTYBEES>SMA200, tsl=15%, pos=15 → 11.63%/Cal 0.307 (FRAGILE)

## Parameters

**Entry:**
- `consecutive_positive_years` — quality filter (N years of positive returns)
- `min_yearly_return_pct` — quality threshold
- `n_tiers` — number of DCA tiers (core novelty)
- `tier_multiplier` — dip depth multiplier per tier
- `base_dip_threshold_pct` — tier 1 dip threshold
- `peak_lookback_days` — rolling peak window
- `rescreen_interval_days` — quality re-screen cadence
- `regime_instrument`, `regime_sma_period` — regime filter

**Exit:**
- `trailing_stop_pct`, `max_hold_days`

**Simulation:**
- `order_sorting_type`, `max_positions`, `max_positions_per_instrument` (should be >= max n_tiers)


# eod_technical Optimization

**Strategy:** Legacy ATO strategy — MA crossover + n-day high breakout + direction_score
market-breadth filter + trailing stop-loss. Wraps the original `scanner` + `order_generator`
modules (predates `eod_breakout`'s modern framework).
**Signal file:** `engine/signals/eod_technical.py` (thin wrapper)
**Data:** `nse.nse_charting_day`
**Session:** 2026-04-24 (post-audit engine, commit fbcd36a+)

## Status: COMPLETE (regime-dependent recent-fold strength)

- [x] Round 0: Baseline
- [x] Round 1: Sensitivity (2 sub-sweeps, 144 configs)
- [x] Round 2: Full cross (243 configs, 10/10 robustness)
- [x] Round 3: (skipped — R2 already 10/10 robust with dense grid)
- [x] Round 4a: OOS (2020-2026)
- [x] Round 4b: Walk-forward (5 folds, Std Cal 0.723)
- [x] Round 4c: Cross-data-source (2026-04-28: nse_charting primary 19.63%/Cal 0.757 > FMP 19.26%/Cal 0.592 > bhavcopy 14.87%/Cal 0.411)
- [x] Round 4d: Cross-exchange (2026-04-28: NSE dominant 2-4×; KR/TW/UK modest; China graveyard; US scanner-threshold-filtered to 0 orders). Full results: [`docs/R4C_R4D_BACKFILL_2026-04-28.md`](../../docs/R4C_R4D_BACKFILL_2026-04-28.md).
- [x] Regime+holdout (2026-04-28) — **negative result, methodology does not transfer**

## Regime+holdout investigation (2026-04-28)

Applied the methodology that lifted eod_breakout (Sharpe 0.804→1.334) on the
hypothesis that eod_technical's top-CAGR-mediocre-Sharpe profile reflected
similar regime fragility. **Both phases failed Pareto:**

| Phase | Best variant | vs current champion |
|---|---|---|
| Holdout retrain (648-config 2010-2024 sweep) | `1_9_2_2` (ndma=5, mh=0) | -0.27pp CAGR / -0.048 Calmar / -0.027 Sharpe |
| Regime gate (8-config NIFTYBEES SMA × force_exit) | SMA=200, force_exit=False | -2.87pp CAGR / -0.272 Calmar / -0.335 Sharpe |

Two reasons it doesn't transfer:
1. eod_technical's 2025 was +2.69% (not a collapse to escape — eod_breakout had -16.57%).
2. Faster cycling (60d avg hold, 3d min_hold) + breadth filter at entry already does regime adjustment at the position level. Stacking a 100-250d NIFTYBEES SMA on top creates whipsaw, not protection.

**Current champion stays.** Full analysis: [`REGIME_AND_HOLDOUT_2026-04-28.md`](REGIME_AND_HOLDOUT_2026-04-28.md).

## Champion

| Period | CAGR | MDD | Calmar | Sharpe | Trades |
|--------|------|-----|--------|--------|--------|
| **Full (2010-2026)** | **19.63%** | -25.9% | **0.757** | **1.07** | 1303 |
| OOS (2020-2026)      | 38.31%    | -28.4% | 1.347    | —    | —    |

**Params:** `ndma=3, ndh=5, direction_score={3, 0.54}, min_hold=3d, tsl=10%, sort=top_gainer, pos=15`

### Walk-forward (5 folds, 3-yr rolling)

| Fold | CAGR | MDD | Calmar | Sharpe | Trades |
|------|------|-----|--------|--------|--------|
| 2010-2013 |  3.05% | -28.1% |  0.109 |  0.07 | 231 |
| 2013-2016 | 11.46% | -17.4% |  0.659 |  0.56 | 250 |
| 2016-2019 |  4.65% | -22.4% |  0.207 |  0.16 | 265 |
| 2019-2022 | 45.92% | -36.3% |  1.263 |  1.30 | 262 |
| 2022-2025 | 35.30% | -19.5% |  **1.813** |  1.70 | 325 |

**Positive folds:** 5/5
**Mean Calmar:** 0.810  **Std Calmar:** 0.723 → FAILS fragility threshold (>0.5)

### Regime dependency (post-hoc sanity check)

The full-period 19.63% CAGR hides significant regime dependency:

| Period | CAGR | Cal | MDD |
|---|---|---|---|
| Pre-2019 (2010-2019) | **8.62%** | 0.310 | -27.8% |
| Full (2010-2026) | 19.63% | 0.757 | -25.9% |
| OOS (2020-2026) | 38.31% | 1.347 | -28.4% |

**The 2010-2019 period is BELOW Nifty ~11% buy-and-hold.** Strategy only outperforms
meaningfully from 2019+, which aligns with the NSE mid-cap bull market 2020-2026
(Nifty Midcap 100 roughly 3-4× in that period).

Forward-looking expectation: somewhere between 8.62% and 38.31%, depending heavily on
whether the NSE mid-cap momentum regime persists. Conservative baseline: ~10-13% CAGR
if markets revert to 2010-2019 conditions.

### Plausibility investigation

OOS CAGR 38.31% triggers the runbook's 20% flag. Investigated:
- **Same-bar entry?** No. `engine/order_generator.py` uses `next_epoch`/`next_open` for
  entries (line 83-88). Same as eod_breakout.
- **Universe survivorship?** No. `nse.nse_charting_day` includes delisted stocks; scanner is
  point-in-time.
- **Stale charges?** No. Post-audit fixes applied to shared engine (scanner.py + order_generator.py
  are not protected files but feed into protected simulator.py + metrics.py).
- **Fold Cal >1.0 with <100 trades?** No — every fold has 230+ trades. Runbook warning
  doesn't apply (it targets sample-size concerns).

The late-fold strength (Cal 1.26-1.81) reflects NSE mid-cap bull market 2019-2025.
Nifty Midcap 100 went from ~17000 → ~60000+ in that period (~20% CAGR just for index).
A concentrated (pos=15) momentum breakout strategy capturing 35-45% CAGR in such a
regime is plausible, not suspicious.

### vs eod_breakout

- eod_technical uses legacy `engine/scanner.py` + `engine/order_generator.py`
- eod_breakout uses the modern signal pipeline + quality/percentile filters + 126-day max hold

Functionally different despite surface similarity: eod_technical has simpler entry
(just ndma + ndh + ds) with no quality overlay. Result: captures more signals,
higher CAGR, better Sharpe.

| Metric | eod_breakout | eod_technical |
|---|---|---|
| Full CAGR | 15.20% | **19.63%** |
| Full Calmar | 0.446 | **0.757** |
| Sharpe | ~0.75 | **1.07** |
| Trades | 833 | 1303 |
| Avg hold | 120d | 60d |

### Deflated Sharpe

388 configs, 192 months. SR = 1.067. Var(SR) = (1 + 0.5·1.067²)/192 = 0.0082,
√Var = 0.090, Z(1 − 1/388) ≈ 2.9. SR_deflated = 1.067 − 0.262 = **0.805** →
strongly above 0.3 threshold.

### Additional metrics

- Sortino **1.510**, vol 16.5% (lower than NIFTYBEES ~18%)
- Win rate 41.8%, PF 1.736 (normal breakout asymmetry — small wins, small losses, long tail)
- MDD duration 1170d (~4.5yr), avg hold 60.3d
- Best year +88.8%, worst year -14.4% (moderate downside)
- Time-in-market 99.9%

## vs baseline

CAGR 15.45% → 19.63% (+4.2pp); Calmar 0.517 → 0.757 (+46%); MDD -29.9% → -25.9%.
Baseline was already strong at 15.45% — minor tuning yielded ~4pp improvement.

## Key findings

- **ndh=5 is the sweet spot.** ndh=2 too noisy (many false breakouts), ndh=7 too slow.
- **tsl=10% optimal.** Tighter (5-8) kills winners, looser (15+) deepens MDD.
- **min_hold=3d slight edge** over min_hold=0 (prevents whipsaw on day-after-entry).
- **ndma=3/5/7 insensitive** — MA filter is binary gate, length barely matters.
- **direction_score=0.54 still best.** Disabling or relaxing hurts.
- **pos=15 optimal.** 10 = concentrated/volatile, 20 = diluted alpha.
- **top_gainer > top_performer** sort.

## Parameters

| Param | Baseline | Champion | Notes |
|-------|----------|----------|-------|
| `n_day_ma` | 5 | **3** | Insensitive 3-7 |
| `n_day_high` | 7 | **5** | Sweet spot |
| `direction_score` | {3, 0.54} | **{3, 0.54}** | Default wins |
| `min_hold_time_days` | 0 | **3** | Prevents whipsaw |
| `trailing_stop_pct` | 10 | **10** | Optimal |
| `order_sorting_type` | top_gainer | **top_gainer** | Best |
| `max_positions` | 15 | **15** | 10-20 plateau |

## Rounds

### R0: Baseline — CAGR 15.45%, Cal 0.517, MDD -29.9%, 159k orders, 1000+ trades

### R1: Sensitivity

- **R1a (96 configs)**: ndma × ndh × tsl × sort. Best 19.81%/Cal 0.720 (ndma=3, ndh=5, tsl=10, top_gainer). Confirms ndh=5 and tsl=10 sweet spots.
- **R1b (48 configs)**: direction_score × min_hold × max_positions. Best 19.63%/Cal 0.757 (ds=0.54, mh=3, pos=15). min_hold=3 > 0 slightly.

### R2: Full cross — 243 configs, 10/10 robustness PASS

Grid: ndma[3,5,7] × ndh[3,5,7] × mh[0,3,5] × tsl[8,10,12] × pos[10,15,20]

Best CAGR: 20.40%/Cal 0.524 (ndma=5, ndh=3, tsl=12)
Best Calmar: **19.63%/Cal 0.757** (ndma=3, ndh=5, mh=3, tsl=10, pos=15) ← champion

Top 10 Calmar all share ndh=5, tsl=10, pos=15. Robust plateau.

### R4a OOS (2020-2026)
CAGR 38.31%, MDD -28.4%, Cal 1.347. Very strong — recent period favorable for
mid-cap momentum breakout. No bias detected (next_open entry, post-audit charges,
point-in-time universe).

### R4b Walk-forward
5/5 positive, Mean Cal 0.810, Std 0.723. FAILS fragility threshold.
Regime-dependent: early folds (2010-2019) weak (Cal 0.11-0.66), recent folds
(2019-2025) explosive (Cal 1.26-1.81). Tracks NSE mid-cap bull market.

## Relationship to eod_breakout

Both strategies use MA + n-day-high + direction_score entries with trailing stop exits.
`eod_breakout` uses the modern signal pipeline, `eod_technical` wraps the legacy path.
Functionally similar; expect comparable results.

eod_breakout champion: `ndh=7, ndm=5, ds={3,0.54}, tsl=8, pos=15` → CAGR 15.20%, Cal 0.446.

## Parameters

**Entry:**
- `n_day_ma` — MA period for "stock above MA" filter
- `n_day_high` — n-day breakout period
- `direction_score` — {n_day_ma, score} market-breadth gate

**Exit:**
- `min_hold_time_days`
- `trailing_stop_pct`

**Simulation:**
- `order_sorting_type`, `max_positions`


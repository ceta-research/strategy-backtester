# Session handover — 2026-04-28 pt3 (B/C/D/E systematic execution)

**Predecessors:**
- [`2026-04-28_handover.md`](2026-04-28_handover.md) — eod_t regime+holdout, Sharpe-doc realign, LIVE_TRADING ensemble rewrite
- [`2026-04-28_pt2_handover.md`](2026-04-28_pt2_handover.md) — N-leg ensemble experiment (negative)

## TL;DR

Executed all four queued forward-work items B/C/D/E in order:

| Item | Result | Commit |
|---|---|---|
| **B — QDT R5 refit** | Pareto-promoted: tsl 8→10, ppi 3→2. Cal 0.388→0.451, MDD -47.4%→-39.3%, Sharpe unchanged | `1f94f53` |
| **C — R4c/R4d cross-validation** | R4c done for 6 strategies; R4d for 2 price-only. FMP fragility for fundamentals strategies confirmed | `c2ade9c` |
| **D — low_pe pre-2018 data investigation** | **Surprise:** data NOT sparse (1547-2126 symbols/year). Cash gap is filter+intersection issue. No backfill needed | `4961505` |
| **E — Per-rebalance adaptive weighting** | Shipped `inverse_vol_adaptive` mode + 5 new tests (37 total, all pass). Empirically doesn't beat static equal-weight | `ad40e04` |

**No new champion overall.** 2-leg `eod_eodt_invvol_quarterly_full` stays at Sharpe 1.281. QDT solo got better (Cal +16%) but 3-leg with QDT still doesn't beat 2-leg.

## What was done (this session)

### B — QDT quality/sector overlay refit (~75 min)

Three-round optimization on QDT's exit/concentration parameters at the existing entry baseline:
- **R5 (36 configs):** tsl[6,8,10] × max_hold[126,252,378,504] × ppi[1,2,3]
- **R5b (9 configs):** zoom around winner with looser tsl[10,12,15] × max_hold[378,504,720] at ppi=2
- **R5c (27 configs):** entry-side recheck (yr × dip × peak) at new exit baseline

Winner `1_1_12_2`: tsl=10, max_hold=504, ppi=2.
- CAGR 17.73% (vs 18.39% old; -0.66pp)
- MDD -39.30% (vs -47.37%; +8.07pp)
- Cal 0.451 (vs 0.388; +16.2%)
- Sharpe 0.759 (vs 0.761; noise)

Mechanism: ppi 3→2 cuts third-tier DCA re-entries deepening drawdowns; tsl 8→10 gives positions room past the 8-10% bracket where false stops occurred.

3-leg-with-QDT ensembles re-run: Cal lifts 0.629→0.725 invvol, but still doesn't beat 2-leg.

Sector cap and force-exit-on-regime-flip not pursued (engine work + mechanism-incompatible with DCA thesis).

### C — R4c/R4d cross-validation backfill (~60 min)

Built two generic runners:
- `scripts/run_r4c_generic.py` — cross-data-source (cr-as-fmp, bhavcopy)
- `scripts/run_r4d_generic.py` — cross-exchange via fmp.stock_eod (11 markets)

R4c run for all 6 backlog strategies. Patterns:
- bhavcopy uniformly worse (-3 to -8pp CAGR; unadjusted price artifacts)
- FMP fragile for fundamentals strategies: factor_composite -12pp, low_pe -8.7pp, trending_value -5.6pp
- Technical strategies robust to data source (±1pp)

R4d run for 2 price-only candidates (eod_technical, ml_supertrend):
- NSE dominant 2-4× cross-exchange
- KR/TW/UK modest positive; China graveyard; US scanner-threshold-filtered

R4d skipped for 4 fundamental strategies (factor_composite, low_pe, trending_value, quality_dip_tiered) — out-of-scope; documented why.

Full writeup: [`docs/R4C_R4D_BACKFILL_2026-04-28.md`](../R4C_R4D_BACKFILL_2026-04-28.md). All 6 OPTIMIZATION.md files updated with R4c/R4d entries.

### D — low_pe pre-2018 data investigation (~30 min)

**Discovered the prior memo claim ("FMP NSE fundamentals sparse pre-2018") is misleading.**

Quantified raw FMP `key_metrics` NSE FY coverage 2010-2025: 1547-2126 symbols/year throughout (74-100% of 2025). Filter pass rate (PE<8, ROE>8%, DE<1, mktcap>10B) is 14-35 stocks/year pre-2018 — small but workable.

low_pe shows 0 trades 2011-2013 because the strategy's intersection of (filter-passing FY candidates) ∩ (scanner-eligible by liquidity avg_txn>70M) yields fewer than `min_stocks=10` candidates per quarter, triggering cash fallback. **This is microstructure (NSE was mid-cap dominant pre-2018, current threshold is 2018+-calibrated), not data sparsity.**

**Decision:** No data backfill project needed. If low_pe full-period viability is desired, run a R5 on the strategy thresholds (lower mktcap_min, lower scanner threshold, lower min_stocks fallback) — separate task.

Memory updated to correct the misleading sparsity claim. Full writeup: [`strategies/low_pe/PRE_2018_INVESTIGATION_2026-04-28.md`](../../strategies/low_pe/PRE_2018_INVESTIGATION_2026-04-28.md).

### E — Per-rebalance adaptive weighting (~75 min)

Implemented `inverse_vol_adaptive` weighting mode in `lib/ensemble_curve.py`:
- `rebalance_combined_curve_adaptive()` — recomputes weights at each rebalance boundary from trailing N days
- `_adaptive_invvol_weights()` — zero-vol-leg drop logic with floor (1e-6)
- `build_ensemble_curve()` — `adaptive=False/True` flag
- 5 new unit tests (37 total, all pass)

`scripts/run_ensemble.py` dispatches to adaptive path when `weighting=inverse_vol_adaptive`; validates `rebalance != 'none'`.

Empirical test on 3-leg eod_b+eod_t+low_pe full 2010+:

| Variant | CAGR | MDD | Cal | Sharpe |
|---|---:|---:|---:|---:|
| 2-leg invvol qtly (champion) | 18.79% | -23.81% | 0.789 | **1.281** |
| 3-leg static invvol | 12.45% | -15.02% | 0.829 | 1.162 |
| 3-leg static equal | 14.65% | -17.79% | 0.823 | **1.225** |
| 3-leg adaptive invvol (252d) | 13.97% | -17.23% | 0.811 | 1.154 |

Adaptive fixes the over-weighting trap (CAGR 12.45 → 13.97%) but doesn't beat static equal-weight. 252-day window is noisy enough to oscillate weights more than truth warrants. Lookback tuning is deferred to Phase 3.5c.

## Decisions

- **Champion unchanged:** 2-leg `eod_eodt_invvol_quarterly_full` stays at Sharpe 1.281.
- **QDT promoted** to new R5 champion (Cal-priority Pareto improvement).
- **No data backfill project** for low_pe — premise was wrong. Strategy R5 retune is the future path if pursued.
- **`inverse_vol_adaptive` shipped** but not promoted as default — static equal-weight is empirically better for the cases tested.

## Files produced (this session, summary)

| Category | Files |
|---|---|
| QDT R5 | `config_round5{,b,c}*.yaml`, `config_champion_pre_r5.yaml` (backup), updated `config_champion.yaml`, OPTIMIZATION.md additions |
| R4c/R4d | `scripts/run_r4c_generic.py`, `scripts/run_r4d_generic.py`, `docs/R4C_R4D_BACKFILL_2026-04-28.md`, updates to 6 strategies' OPTIMIZATION.md |
| low_pe investigation | `strategies/low_pe/PRE_2018_INVESTIGATION_2026-04-28.md` |
| Adaptive weighting | `lib/ensemble_curve.py` (additions), `scripts/run_ensemble.py` (additions), `tests/test_ensemble_curve.py` (5 new tests), 2 new ensemble configs, ENSEMBLE_GUIDE.md additions |
| Memory | 2 entries: project_lowpe_pre2018_data_check + updated feedback_ensemble_invvol_trap |

## Commits

| Commit | Title |
|---|---|
| `1f94f53` | QDT R5 refit: Pareto-promote tsl=10/ppi=2 (Cal 0.388 → 0.451) |
| `c2ade9c` | R4c/R4d backfill: 6 strategies cross-validated |
| `4961505` | low_pe pre-2018 investigation: data is NOT sparse (closes D) |
| `ad40e04` | Phase 3.5: per-rebalance adaptive inverse-vol weighting in ensemble runner |
| (this) | Session handover 2026-04-28 pt3 |

## Next session candidates

The major queue (B/C/D/E) is exhausted. Forward options:

1. **low_pe R5 strategy-threshold retune.** Investigation D pivoted the work from data-engineering to strategy-tuning. A focused 32-64 config sweep on (mktcap_min, avg_txn threshold, min_stocks fallback, pe_max) targeting continuous trading 2010+. Expected upshot: low_pe full-period CAGR rises from 5.86% → likely 8-12%, which could shift the N-leg ensemble conclusion. Estimate: ~3-4 hrs.

2. **Adaptive weighting Phase 3.5c — lookback tuning.** Empirically test adaptive with shorter (60d, 120d) and longer (504d) lookbacks. Maybe one window does beat static equal-weight. Estimate: ~1 hr.

3. **R4d for technical strategies on different scanner thresholds** (per-market avg_txn calibration). The current R4d for eod_technical/ml_supertrend is hampered by INR-calibrated liquidity threshold. A clean cross-exchange test would re-tune scanner per market. Estimate: ~3 hrs.

4. **Engine-level deferred items:**
   - Slippage = ₹0 in all 21 signal generators (real CAGR ~0.3pp lower).
   - All `exit_reason = "natural"` in eod_breakout signal gen.
   - 49 stale results files (pre-`ba95a05` charges).
   - 1 P1 + 2 P2 + 6 P3 audit hygiene items.

5. **Brand-new direction.** None of the above is a forced next step. Reasonable to pause and direct.

Recommend talking through priorities at the start of next session before opening any of these.

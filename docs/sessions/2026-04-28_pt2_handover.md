# Session handover — 2026-04-28 pt2 (N-leg ensemble experiment)

**Predecessor:** [`2026-04-28_handover.md`](2026-04-28_handover.md) (eod_t regime+holdout investigation, Sharpe-doc realign, LIVE_TRADING ensemble rewrite)

## TL;DR

- Tested whether adding a third low-correlation leg (low_pe, then QDT) to the 2-leg `eod_b+eod_t invvol qtly` champion improves Sharpe or Calmar on the full 2010-2026 window.
- **Negative on Sharpe, positive only on Calmar.** No 3-leg variant tested beats the 2-leg champion (Sharpe 1.281) on the primary axis.
- Mechanism: low_pe IS the lowest-correlation candidate (corr 0.39/0.45) but its full-period CAGR is depressed to 5.86% by FMP NSE pre-2018 fundamentals sparsity (leg holds cash 2010-2017 → diversification value lost to cash drag).
- **2-leg champion stays.** Optional defensive variant (w_lowpe=0.25) buys Cal 0.789→0.810 / MDD -23.81%→-19.31% at -0.03 Sharpe — Pareto trade, documented but not promoted.
- Documentation updated: `ENSEMBLE_GUIDE.md` worked-3-leg example + lessons; `STATUS.md` session log + ensembles section; full writeup at `strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md`.

## What was done (this session)

1. **Ran low_pe with champion params on full 2010-2026** (`scripts/run_lowpe_full.py` → `results/low_pe/champion_full.json`). Solo: CAGR 5.86%, MDD -12.08%, Cal 0.485, Sharpe 0.521. Vol 7.41% (cash-period compression).

2. **Built 4 new 3-leg ensemble configs**:
   - `eod_eodt_lowpe_invvol_quarterly_full` (full window)
   - `eod_eodt_lowpe_equal_quarterly_full`
   - `eod_eodt_lowpe_invvol_quarterly_modern` (2018+ for upper-bound check)
   - `eod_eodt_lowpe_equal_quarterly_modern`

3. **Surveyed alternative 3rd-leg candidates** (QDT, TV, FC, MLS) for correlation against eod_b/eod_t. low_pe wins: 0.39/0.45 vs 0.46/0.58 (QDT) and worse for the rest.

4. **Apples-to-apples 2-leg modern baseline computed** (eod_b+eod_t clipped to 2018+ window via slicing): Sharpe 1.519/1.537. Confirmed modern-window 3-leg `eod_eodt_lowpe_equal_quarterly_modern` (1.518) does NOT beat 2-leg modern (1.537) — the 2-leg already extracts the diversification benefit.

5. **Weight sensitivity scan on full period** (w_lowpe ∈ [0, 0.6], split rest at eod_b/eod_t inverse-vol ratio). Sharpe monotonically decreases; Calmar monotonically increases. No Pareto-dominating weight.

6. **Aggregated final leaderboard** of all ensemble variants in suite (10 configs total).

7. **Documentation:**
   - Created `strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md` (full writeup, ~5 pages).
   - Updated `docs/ENSEMBLE_GUIDE.md`: added 3-leg worked example, weight-sensitivity table, lessons-learned section, full file inventory.
   - Updated `docs/STATUS.md`: session-log entry, expanded "Best 2010-current" section with N-leg result.
   - This handover.

## Commits

| Commit | Title |
|---|---|
| `38d3cef` | N-leg ensemble experiment: 2-leg champion stays |
| (this session) | docs: ENSEMBLE_GUIDE 3-leg worked example + handover |

## Key findings (durable)

1. **Adding more legs ≠ better Sharpe.** Diversification helps Sharpe only when the new leg has comparable per-unit-risk return. low_pe's full-period CAGR/vol ratio (5.86%/7.41% = 0.79) is below eod_b's (17.68%/13.25% = 1.33). Adding it lowers ensemble Sharpe-per-vol.

2. **Inverse-vol is unsafe with mixed-coverage legs.** A leg that holds cash for years has compressed vol → gets over-weighted by invvol. low_pe's full-period vol (7.41%) is artificially low; invvol allocates 50% to it, dragging CAGR from 18.79% to 12.45%. Use equal-weight or hand-tune for such legs.

3. **MDD reduction has a CAGR price.** Pareto trade-offs along w_lowpe are real and quantifiable.

4. **Modern-window Sharpe is upward-biased and not the right benchmark for live deployment.** 2018+ happens to be a great window for the suite. Use full-period numbers.

5. **Mechanism observation:** in a 2-leg eod_b+low_pe (modern), low_pe diversifies eod_b. In a 3-leg eod_b+eod_t+low_pe (modern), eod_t already diversifies eod_b enough (different parameters of breakout, but corr 0.59 still useful), so low_pe is redundant for Sharpe purposes. Only adds Calmar value via lower MDD.

## Decisions

- **Champion unchanged**: 2-leg `eod_eodt_invvol_quarterly_full` (Sharpe 1.281, Cal 0.789, MDD -23.81%).
- **Defensive variant documented but not promoted**: w_lowpe=0.25 (eod_b 41.6% / eod_t 33.4% / low_pe 25%). Available as decision input if user prioritizes MDD over Sharpe.
- **N-leg work paused** until either (a) low_pe pre-2018 data backfilled from alternate source, or (b) per-rebalance adaptive weighting (Phase 3.5) implemented to mitigate inverse-vol misallocation.

## Files produced

| File | Status |
|---|---|
| `strategies/low_pe/config_champion_full.yaml` | new |
| `scripts/run_lowpe_full.py` | new (one-off runner) |
| `results/low_pe/champion_full.json` | new (gitignored) |
| `strategies/ensembles/eod_eodt_lowpe_invvol_quarterly_full/config.yaml` | new |
| `strategies/ensembles/eod_eodt_lowpe_equal_quarterly_full/config.yaml` | new |
| `strategies/ensembles/eod_eodt_lowpe_invvol_quarterly_modern/config.yaml` | new |
| `strategies/ensembles/eod_eodt_lowpe_equal_quarterly_modern/config.yaml` | new |
| `results/ensembles/eod_eodt_lowpe_*.json` (4 files) | new (gitignored) |
| `strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md` | new |
| `docs/ENSEMBLE_GUIDE.md` | edited (3-leg worked example + lessons) |
| `docs/STATUS.md` | edited (session log + ensembles section) |
| `docs/sessions/2026-04-28_pt2_handover.md` | this file |

## Next session candidates

The 2-leg champion is locked. Forward work options remain the same as the predecessor handover, with one item closed:

- ~~A. N-leg ensemble experiment~~ — **DONE this session, negative result.**
- **B. QDT quality/sector overlay refit** (~4-6 hrs). Untested angles: `consecutive_positive_years` filter, sector concentration caps, tighter TSL grid. Goal: shave QDT's -47% solo MDD without losing CAGR. Useful regardless of ensemble work because QDT is rank-2 by CAGR (18.39%).
- **C. R4c/R4d cross-validation backfill** (~3 hrs). Run cross-data-source + cross-exchange validation for 6 strategies that completed R0-R4b but skipped R4c/R4d: factor_composite, quality_dip_tiered, trending_value, eod_technical, low_pe, ml_supertrend.
- **D. low_pe pre-2018 data backfill** (~unknown, requires data engineering). If we can synthesize fundamentals from an alternate source (Refinitiv, manual scraping, BSE filings), low_pe full-period CAGR could rise meaningfully → re-run N-leg with that.
- **E. Per-rebalance adaptive weighting (Phase 3.5)**. Unlocks honest invvol with trailing-window vol that excludes cash periods. Would change the N-leg conclusion if it materially changes the low_pe weight. Engine-level work; non-trivial.

Recommended order if you're back: **B (QDT refit)** — highest expected leverage on the leaderboard, isolated scope, no dependency on data or engine work.

# Strategy Backtester — Current Status

**Last updated:** 2026-04-28 (pt2)
**Engine baseline:** commit `fbcd36a` (post-audit, all P0/P1 fixes landed)
**Latest session handover:** [`sessions/2026-04-28_pt2_handover.md`](sessions/2026-04-28_pt2_handover.md)

> **Maintenance convention:** This is the living source-of-truth doc. Every session must update STATUS.md before closing — at minimum: header date, latest handover link, session log entry, leaderboards if changed, deferred work. Per-session narratives go to `sessions/`.

---

## Session log

| Date | Focus | Commits | Key outcome |
|---|---|---|---|
| 2026-04-28 pt2 | N-leg ensemble + QDT R5 + R4c/R4d backfill + low_pe data check | (this session) | (1) Tested 3-leg eod_b+eod_t+low_pe. 2-leg champion stays (Sharpe 1.281). [`strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md`](../strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md). (2) QDT R5 refit (72 configs): new champion `tsl=10, ppi=2` gives Cal 0.388→0.451. (3) R4c run for all 6 backlog strategies + R4d for 2 price-only. [`R4C_R4D_BACKFILL_2026-04-28.md`](R4C_R4D_BACKFILL_2026-04-28.md). (4) low_pe pre-2018 data investigation: FMP coverage 1547-2126 symbols/year — **NOT sparse**. Pre-2018 cash gap is filter+scanner intersection issue. No data backfill needed. [`strategies/low_pe/PRE_2018_INVESTIGATION_2026-04-28.md`](../strategies/low_pe/PRE_2018_INVESTIGATION_2026-04-28.md). |
| 2026-04-28 | eod_t regime+holdout (negative); Sharpe-doc realign; LIVE_TRADING ensemble rewrite; 2025 gate run | `14bbe35`..`0c046e2` | eod_t regime+holdout investigation — both phases failed Pareto, methodology mechanism-specific. Sharpe references realigned: doc 1.334 was CAGR/vol, engine canonical is 1.183 — all canonical docs aligned. LIVE_TRADING_INTEGRATION rewritten for ensemble winner. 2025 OOS gate ran on QDT/TV/low_pe — all 3 fail (no 2025 collapse), regime+holdout deprioritized indefinitely. |
| 2026-04-27 pt2 | Ensemble runner Phase 1-6 + 2010+ best | `fc3d0f2`..`af35ac5` (8 commits) | Ensemble runner shipped. Best 2010+ ensemble: eod_b+eod_t invvol qtly, Sharpe 1.281 |
| 2026-04-27 | eod_breakout regime+holdout champion | `ba1c208` | Strict Pareto improvement: 15.20%→17.68% CAGR, 2025 -16.57%→+18.67% |
| 2026-04-26 | Docs cleanup, audit-era archival | `8943a5e` | 38 commits flushed; STATUS.md created; LIVE_TRADING rewritten |
| 2026-04-25 | Queue exhaustion review | `896323b` | 0 PENDING; brainstorm framework drafted |
| 2026-04-24 (pt1-3) | Bulk audit retirements | various | 8 strategies AUDIT_RETIRED |

Older entries: see `sessions/` directory.

---

## Queue snapshot

```
0 PENDING / 13 COMPLETE / 17 AUDIT_RETIRED   (30 strategies total)
```

The strategy-optimization queue is exhausted. Forward work is now:
**(1)** apply regime+holdout methodology to other COMPLETE strategies (proven on eod_breakout — see recommended next session below), **(2)** extend ensemble exploration, **(3)** clear deferred maintenance backlog.

---

## Top by CAGR (2010-2026 unless noted)

| Rank | Strategy | CAGR | Calmar | Sharpe | Caveat |
|---:|---|---:|---:|---:|---|
| 1 | `eod_technical` | **19.63%** | **0.757** | 1.067 | Pre-2019 only 8.62%, but 2025 was +2.69% — no 2025 collapse. Regime+holdout TESTED 2026-04-28: methodology does not transfer (see deferred work / [`strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md`](../strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md)). |
| 2 | `quality_dip_tiered` | **17.73%** | **0.451** | 0.759 | Re-promoted 2026-04-28 (R5: tsl 8→10, ppi 3→2). MDD -47.4% → -39.3%. |
| 3 | `eod_breakout` | **17.68%** | **0.661** | **1.183** | Re-promoted 2026-04-27 (regime+holdout, 2025 +18.67%). |
| 4 | `trending_value` | 16.89% | 0.481 | 0.753 | WF Std Cal 0.745 (FMP sparsity) |
| 5 | `enhanced_breakout` | 16.40% | 0.656 | — | Best Cal among breakouts |
| 6 | `factor_composite` | 14.78% | 0.319 | 0.549 | Deep MDD -46% |

> **Sharpe convention:** all Sharpe values in STATUS.md are engine-canonical
> `(geom_mean(daily) - rf_daily) / std(daily)` annualized — what the runner
> and `lib/metrics.py` produce. The `regime+ensemble` doc and earlier drafts
> of OPTIMIZATION.md cited a `CAGR / vol` prototype number ~0.10-0.20 higher
> (1.334 vs canonical 1.183 for eod_breakout). All canonical docs were
> realigned 2026-04-28.

## Top by Calmar

| Rank | Strategy | CAGR | Calmar | Caveat |
|---:|---|---:|---:|---|
| 1 | `low_pe` | 12.30% | **1.016** | Modern 2018+ only (FMP sparsity) |
| 2 | `eod_technical` | 19.63% | 0.757 | Most CAGR-efficient |
| 3 | `earnings_dip` | 13.80% | 0.680 | Modern 2020+ only |
| 4 | `eod_breakout` | 17.68% | 0.661 | Regime-gated |
| 5 | `quality_dip_tiered` | 17.73% | **0.451** | R5-promoted 2026-04-28 |

## Top by Sharpe

| Rank | Strategy | Sharpe | Caveat |
|---:|---|---:|---|
| 1 | `eod_breakout` | **1.183** | Regime+holdout, post-promotion |
| 2 | `eod_technical` | 1.067 | Pre-2019 fragile |
| 3 | `low_pe` | 1.002 | Modern only |

---

## Best ensembles (NEW — 2026-04-27 pt2)

Built via `scripts/run_ensemble.py` (see [`ENSEMBLE_GUIDE.md`](ENSEMBLE_GUIDE.md)).

### Best 2010-current

```yaml
config:    strategies/ensembles/eod_eodt_invvol_quarterly_full/config.yaml
window:    2010-01-01 -> 2026-03-19  (16.2 years)
weights:   eod_breakout 56% / eod_technical 44% (inverse-vol)
rebalance: quarterly
CAGR:      18.79%   # vs eod_t solo 19.63% / eod_b solo 17.68%
MDD:      -23.81%   # vs solo -25.95% / -26.75%
Calmar:    0.789    # vs solo 0.757 / 0.661
Sharpe:    1.281    # lifts above BOTH solos (1.067 / 1.183)
Vol:       13.11%
```

**N-leg attempts (2026-04-28 pt2):** 3-leg with low_pe / QDT tested. None beat
the 2-leg on Sharpe. low_pe IS the lowest-correlation candidate (corr 0.39/0.45)
but full-period CAGR 5.86% (cash drag pre-2018) overwhelms diversification.
Optional defensive variant: add 25% low_pe → Cal 0.810 / MDD -19.31% / Sharpe
1.253 (Pareto trade, not strict improvement). Writeup:
[`strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md`](../strategies/ensembles/N_LEG_EXPERIMENT_2026-04-28.md).

### Best 2018+ (modern)

```yaml
config:    strategies/ensembles/eod_lowpe_invvol_quarterly/config.yaml
weights:   eod_breakout-modern 38% / low_pe-modern 62% (inverse-vol)
rebalance: quarterly
Calmar:    1.177    # best of any variant in the suite
MDD:      -14.60%   # smallest of any variant
Sharpe:    1.365
```

### Ensemble lessons

- Drawdown depth dominates correlation for leg selection. QDT (-47% solo MDD) drags ensemble drawdowns deeper despite lowest correlation (0.457).
- Inverse-vol ≠ Sharpe-optimal. Optimizes risk parity. Use `sharpe_sensitivity_2leg` to see the curve.
- eod_breakout is the best ensemble anchor (corr 0.46-0.59 to all candidates).
- Set-and-forget biases optimistically; quarterly is the honest baseline.

---

## Benchmark

NIFTYBEES buy-and-hold 2010-2026: **10.45% CAGR / Cal ~0.27 / Sharpe ~0.45**

Best 2010+ ensemble (above) vs benchmark: +8.34pp CAGR, 3× Calmar, 2.8× Sharpe.

---

## Protected files (DO NOT MODIFY without re-audit)

```
engine/pipeline.py        engine/utils.py        engine/simulator.py
engine/ranking.py         engine/charges.py      engine/exits.py
engine/order_key.py       lib/metrics.py         lib/backtest_result.py
lib/equity_curve.py
```

`lib/ensemble_curve.py` is a NEW sibling module (not on the protected list) added 2026-04-27 pt2.

Verify before any optimization session:
```bash
git diff fbcd36a HEAD -- engine/pipeline.py engine/utils.py engine/simulator.py \
  engine/ranking.py engine/charges.py engine/exits.py engine/order_key.py \
  lib/metrics.py lib/backtest_result.py lib/equity_curve.py | wc -l
# Must be 0
```

---

## Deferred work

### Pending (prioritized, top of queue first)

1. **Update `LIVE_TRADING_INTEGRATION.md`** (~30-60 min). Add ensemble-as-deployment option (eod_b + eod_t invvol qtly). Document daily breadth check + quarterly rebalance ops. Friction ~25bps/yr.
2. **Apply regime+holdout to other strategies — GATE RAN 2026-04-28, ALL FAILED** (~3-4 hrs each, NOT scheduled). The eod_technical investigation (2026-04-28) showed the methodology is mechanism-specific: it fixes a 2025 collapse, not a generic Sharpe gap. The 5-min 2025 OOS gate (champion 2025 < -10% → proceed) ran on all three candidates:
   - `quality_dip_tiered`: 2025 = **+33.64%** → FAIL (huge year, nothing to correct). Other angles (ppi=1, sector caps, tsl=6%) remain untested but unrelated to regime.
   - `trending_value`: 2025 = -4.85% → FAIL (mild, not catastrophic).
   - `low_pe`: 2025 = -1.32% → FAIL (basically flat — already most defensive).

   None of the three has a 2025 problem. Regime+holdout work on this list is deprioritized indefinitely. Other angles (alternative entry mechanisms, walk-forward refits, ensemble experiments with non-breakout legs) may still apply but should not be framed as "regime+holdout".

### Closed (negative result)

- **eod_technical regime+holdout (2026-04-28)** — both phases failed Pareto. Holdout retrain produced -0.27pp CAGR / -0.048 Calmar; all 8 regime-gate variants worse than baseline (best variant: -2.87pp CAGR). Mechanism reasons: no 2025 collapse to fix (+2.69%), faster cycling + breadth-filter entries already do regime adjustment at the position level. Full writeup: [`strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md`](../strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md). Engine artifacts retained: regime support in `engine/signals/eod_technical.py` wrapper + `scripts/decode_config_id.py` helper.

### Carried-forward backlog

- 49 stale results files (pre-`ba95a05` charges) — see [`archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md`](archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md). Not blocking; affects only R4d cross-exchange validation.
- Regression snapshots in `tests/regression/snapshots/` show drift vs fresh runs (data growth). Re-pinning decision pending.
- ~~R4c (cross-data) + R4d (cross-exchange) for 6 newer COMPLETE strategies~~ **DONE 2026-04-28 pt2.** R4c run for all 6; R4d run for 2 price-only strategies (eod_technical, ml_supertrend); R4d skipped for 4 fundamental strategies as out-of-scope. Full results: [`R4C_R4D_BACKFILL_2026-04-28.md`](R4C_R4D_BACKFILL_2026-04-28.md).
- 1 P1 + 2 P2 + 6 P3 audit hygiene items (see [`archive/audit-2026-04/AUDIT_CHECKLIST.md`](archive/audit-2026-04/AUDIT_CHECKLIST.md)).
- Add deflated Sharpe to `OPTIMIZATION_RUNBOOK.md` as standard requirement.
- Add same-bar bias check to `OPTIMIZATION_PROMPT.md` (gap_fill lesson).
- Investigate `index_breakout` engine-vs-standalone discrepancy (memory: 13.3% vs engine -0.70% on identical params).
- Consider revisiting 5 retired NSE mean-reversion strategies on index-ETF universe (BANKBEES, sector ETFs).

### Engine-level (deferred — protected files)

- Slippage = ₹0 in all 21 signal generators. Real CAGR ~0.3pp lower than reported.
- All `exit_reason = "natural"` in eod_breakout signal gen (TSL/anomalous_drop reasons not propagated).

### Ensemble runner — Phase 3.5 / 7+

- ~~Per-rebalance adaptive weighting (trailing-window inverse-vol)~~ — **shipped 2026-04-28 pt2** as `inverse_vol_adaptive`. Empirically does NOT beat static equal-weight on the 3-leg test case.
- Iterative ERC solver for `risk_parity` mode
- `config_path` rerun mode with cache invalidation on mtime
- Friction modeling per leg (rebalance trades cost something)
- `union_ffill` alignment for differently-windowed legs
- N-leg Sharpe sensitivity (currently 2-leg only)

---

## Recommended opening for next session

**Low-risk (~30-60 min):** Update `LIVE_TRADING_INTEGRATION.md` (item 1) for the ensemble winner. The deployment story is settled (eod_b + eod_t invvol qtly, Sharpe 1.281). Live trading docs should reflect that.

**Open question:** with regime+holdout gated out for the entire deferred list, the highest-leverage forward work is unclear. Three candidate directions:

1. **Refit a non-breakout strategy with the post-audit engine** — e.g., `quality_dip_tiered` has -47% MDD that the regime gate can't fix; a quality-overlay or sector-cap refit might shave that. Estimate 4-6 hrs.
2. **Build an N-leg ensemble experiment** — current best ensemble is 2-leg (eod_b + eod_t). Adding a low-correlated value leg (low_pe) was tried in the 2018+ window (Cal 1.177). A full 2010+ 3-leg with `low_pe` extended back via FMP fundamentals when available is untried.
3. **Sharpen R4c/R4d (cross-data + cross-exchange) on the 6 deferred strategies** — pure validation work, low surprise factor.

Recommend talking through priorities at the start of next session before opening any of these.

---

## Documentation map

**Live (canonical):**
- [`STATUS.md`](STATUS.md) — this file. Living tracker; updated every session.
- [`OPTIMIZATION_PROMPT.md`](OPTIMIZATION_PROMPT.md) — session-start prompt
- [`OPTIMIZATION_RUNBOOK.md`](OPTIMIZATION_RUNBOOK.md) — repeatable methodology
- [`ENGINE_STRATEGY_GUIDE.md`](ENGINE_STRATEGY_GUIDE.md) — pipeline architecture
- [`BACKTEST_GUIDE.md`](BACKTEST_GUIDE.md) — standalone scripts
- [`ENSEMBLE_GUIDE.md`](ENSEMBLE_GUIDE.md) — combine N strategies (`scripts/run_ensemble.py`)
- [`LIVE_TRADING_INTEGRATION.md`](LIVE_TRADING_INTEGRATION.md) — going-live plan

**Sessions:** [`sessions/`](sessions/) — chronological session handovers + code reviews

**Archive:**
- [`archive/audit-2026-04/`](archive/audit-2026-04/) — completed audit (P0-P3) + bias measurements
- [`archive/pre-engine-2026-03/`](archive/pre-engine-2026-03/) — pre-engine standalone-script era

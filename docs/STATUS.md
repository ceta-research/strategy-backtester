# Strategy Backtester — Current Status

**Last updated:** 2026-04-27 (post-session pt2)
**Engine baseline:** commit `fbcd36a` (post-audit, all P0/P1 fixes landed)
**Latest session handover:** [`sessions/2026-04-27_pt2_ensemble_handover.md`](sessions/2026-04-27_pt2_ensemble_handover.md)

> **Maintenance convention:** This is the living source-of-truth doc. Every session must update STATUS.md before closing — at minimum: header date, latest handover link, session log entry, leaderboards if changed, deferred work. Per-session narratives go to `sessions/`.

---

## Session log

| Date | Focus | Commits | Key outcome |
|---|---|---|---|
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
| 1 | `eod_technical` | **19.63%** | **0.757** | 1.067 | Pre-2019 only 8.62% (regime-dep). Likely benefits from regime+holdout. |
| 2 | `quality_dip_tiered` | 18.39% | 0.388 | 0.761 | Deep MDD -47% |
| 3 | `eod_breakout` | **17.68%** | **0.661** | **1.334** | Re-promoted 2026-04-27 (regime+holdout, 2025 +18.67%). See Sharpe note below. |
| 4 | `trending_value` | 16.89% | 0.481 | 0.753 | WF Std Cal 0.745 (FMP sparsity) |
| 5 | `enhanced_breakout` | 16.40% | 0.656 | — | Best Cal among breakouts |
| 6 | `factor_composite` | 14.78% | 0.319 | 0.549 | Deep MDD -46% |

> **Note on eod_breakout Sharpe:** OPTIMIZATION.md documents 1.334 (computed from regime+holdout champion result). Today's ensemble runner reads 1.183 from `results/eod_breakout/champion.json`. Likely a file-version discrepancy — `champion.json` may pre-date the regime+holdout promotion. Pending investigation (see deferred work).

## Top by Calmar

| Rank | Strategy | CAGR | Calmar | Caveat |
|---:|---|---:|---:|---|
| 1 | `low_pe` | 12.30% | **1.016** | Modern 2018+ only (FMP sparsity) |
| 2 | `eod_technical` | 19.63% | 0.757 | Most CAGR-efficient |
| 3 | `earnings_dip` | 13.80% | 0.680 | Modern 2020+ only |
| 4 | `eod_breakout` | 17.68% | 0.661 | Regime-gated |

## Top by Sharpe

| Rank | Strategy | Sharpe | Caveat |
|---:|---|---:|---|
| 1 | `eod_breakout` | **1.334** | Regime+holdout, post-promotion |
| 2 | `low_pe` | 1.198 | Modern only |
| 3 | `eod_technical` | 1.067 | Pre-2019 fragile |

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

1. **Apply regime+holdout to `eod_technical`** (~3-4 hrs). Highest ROI: top-CAGR strategy, mediocre Sharpe (1.067), pre-2019 fragility documented. Methodology proven on eod_breakout. Could lift Sharpe to 1.2+, then ensemble Sharpe 1.281 → 1.35+.
2. **Update `LIVE_TRADING_INTEGRATION.md`** (~30-60 min). Add ensemble-as-deployment option (eod_b + eod_t invvol qtly). Document daily breadth check + quarterly rebalance ops. Friction ~25bps/yr.
3. **Investigate eod_breakout Sharpe discrepancy** (~15 min). 1.334 (OPTIMIZATION.md) vs 1.183 (champion.json read by runner). Likely file-version issue.
4. **Apply regime+holdout to other strategies** (~3-4 hrs each):
   - `quality_dip_tiered`: -47% MDD; ppi=1, sector caps, tsl=6% never tested
   - `trending_value`: lb=1mo champion vs O'Shaughnessy's 6mo never tested
   - `low_pe`: already most defensive; regime exit may further improve

### Carried-forward backlog

- 49 stale results files (pre-`ba95a05` charges) — see [`archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md`](archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md). Not blocking; affects only R4d cross-exchange validation.
- Regression snapshots in `tests/regression/snapshots/` show drift vs fresh runs (data growth). Re-pinning decision pending.
- R4c (cross-data) + R4d (cross-exchange) for 6 newer COMPLETE strategies (`factor_composite`, `quality_dip_tiered`, `trending_value`, `eod_technical`, `low_pe`, `ml_supertrend`).
- 1 P1 + 2 P2 + 6 P3 audit hygiene items (see [`archive/audit-2026-04/AUDIT_CHECKLIST.md`](archive/audit-2026-04/AUDIT_CHECKLIST.md)).
- Add deflated Sharpe to `OPTIMIZATION_RUNBOOK.md` as standard requirement.
- Add same-bar bias check to `OPTIMIZATION_PROMPT.md` (gap_fill lesson).
- Investigate `index_breakout` engine-vs-standalone discrepancy (memory: 13.3% vs engine -0.70% on identical params).
- Consider revisiting 5 retired NSE mean-reversion strategies on index-ETF universe (BANKBEES, sector ETFs).

### Engine-level (deferred — protected files)

- Slippage = ₹0 in all 21 signal generators. Real CAGR ~0.3pp lower than reported.
- All `exit_reason = "natural"` in eod_breakout signal gen (TSL/anomalous_drop reasons not propagated).

### Ensemble runner — Phase 3.5 / 7+

- Per-rebalance adaptive weighting (trailing-window inverse-vol)
- Iterative ERC solver for `risk_parity` mode
- `config_path` rerun mode with cache invalidation on mtime
- Friction modeling per leg (rebalance trades cost something)
- `union_ffill` alignment for differently-windowed legs
- N-leg Sharpe sensitivity (currently 2-leg only)

---

## Recommended opening for next session

**Highest expected value:** Apply regime+holdout methodology to `eod_technical` (item 1 above, ~3-4 hours).

Why:
- Top-CAGR strategy with mediocre Sharpe (1.067) — strongly suggests regime fragility, same as eod_breakout pre-promotion.
- Methodology fully proven on eod_breakout: 1152-config R2 sweep on 2010-2024, NIFTYBEES SMA(100) regime gate sweep.
- Sharpe lift on eod_technical feeds straight into the best 2010+ ensemble (1.281 → potentially 1.35+).

**Alternative low-risk option:** Update `LIVE_TRADING_INTEGRATION.md` for the new champion + ensemble (~30-60 min).

**Quick win:** Pin down the eod_breakout Sharpe discrepancy (~15 min). Surfaces a recompute or file-version inconsistency.

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

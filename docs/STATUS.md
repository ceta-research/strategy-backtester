# Strategy Backtester — Current Status

**Last updated:** 2026-04-26
**Engine baseline:** commit `fbcd36a` (post-audit, all P0/P1 fixes landed)
**Latest session handover:** [`sessions/2026-04-25_handover.md`](sessions/2026-04-25_handover.md)

---

## Queue snapshot

```
0 PENDING / 13 COMPLETE / 17 AUDIT_RETIRED   (30 strategies total)
```

The auto-prioritized queue is exhausted. Next session needs strategic
direction (see brainstorm in latest handover).

## Top 5 by CAGR

| Rank | Strategy | CAGR | Calmar | Sharpe | Caveat |
|---:|---|---:|---:|---:|---|
| 1 | `eod_technical` | **19.63%** | **0.757** | **1.07** | Pre-2019 only 8.62% (regime-dep) |
| 2 | `quality_dip_tiered` | 18.39% | 0.388 | 0.76 | Deep MDD -47% |
| 3 | `eod_breakout` | **17.68%** | **0.661** | **1.334** | Re-promoted 2026-04-27 (regime+holdout, 2025 +18.67%) |
| 4 | `trending_value` | 16.89% | 0.481 | 0.75 | WF Std Cal 0.745 (FMP sparsity) |
| 5 | `enhanced_breakout` | 16.40% | 0.656 | — | Best Cal among breakouts |

## Top by Calmar

| Rank | Strategy | CAGR | Calmar | Caveat |
|---:|---|---:|---:|---|
| 1 | `low_pe` | 12.30% | **1.016** | Modern 2018+ only (FMP sparsity) |
| 2 | `eod_technical` | 19.63% | 0.757 | — |
| 3 | `earnings_dip` | 13.80% | 0.680 | Modern 2020+ only |

## Benchmark

NIFTYBEES buy-and-hold 2010-2026: **10.45% CAGR / Cal ~0.27 / Sharpe ~0.45**

---

## Protected files (DO NOT MODIFY without re-audit)

```
engine/pipeline.py        engine/utils.py        engine/simulator.py
engine/ranking.py         engine/charges.py      engine/exits.py
engine/order_key.py       lib/metrics.py         lib/backtest_result.py
lib/equity_curve.py
```

Verify before any optimization session:
```bash
git diff fbcd36a HEAD -- engine/pipeline.py engine/utils.py engine/simulator.py \
  engine/ranking.py engine/charges.py engine/exits.py engine/order_key.py \
  lib/metrics.py lib/backtest_result.py lib/equity_curve.py | wc -l
# Must be 0
```

---

## Deferred work

- 49 stale results files (pre-`ba95a05` charges) — see
  [`archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md`](archive/pre-engine-2026-03/CROSS_EXCHANGE_STALE_RATES.md).
  Not blocking; only affects R4d cross-exchange validation.
- Regression snapshots in `tests/regression/snapshots/` show drift vs
  fresh runs (data growth). Re-pinning decision pending.
- R4c (cross-data) + R4d (cross-exchange) for 6 newer COMPLETE strategies
  (`factor_composite`, `quality_dip_tiered`, `trending_value`,
  `eod_technical`, `low_pe`, `ml_supertrend`).
- 1 P1 + 2 P2 + 6 P3 audit hygiene items (see
  [`archive/audit-2026-04/AUDIT_CHECKLIST.md`](archive/audit-2026-04/AUDIT_CHECKLIST.md)).
- Add deflated Sharpe to `OPTIMIZATION_RUNBOOK.md` as standard requirement.
- Add same-bar bias check to `OPTIMIZATION_PROMPT.md` (gap_fill lesson).
- Investigate `index_breakout` engine-vs-standalone discrepancy
  (memory 13.3% vs engine -0.70% on identical params).
- Consider revisiting 5 retired NSE mean-reversion strategies on an
  index-ETF universe (BANKBEES, sector ETFs).

---

## Brainstorm framework (next session)

To beat current best `eod_technical` (19.63%) by +5pp → need **24.6%+ CAGR**.

Three buckets:

**A) Deferred maintenance** (defensive)
- Walk-forward re-runs, cross-exchange, deflated Sharpe, bias-check protocol.

**B) Bias-fix retrievals** (>1 week infra; may revive 5-7 retired strategies)
- Minute-bar NSE data + realistic execution sim.

**C) New strategy generation** (highest +CAGR ROI — RECOMMENDED)
1. Ensemble of top-5 (highest probability, ~17-18% CAGR with Cal 0.7+)
2. Quality + momentum overlay (proven NSE pattern, 20-23%)
3. Sector rotation on `eod_technical` (highest payoff, 22-25%)
4. 2× leverage on `low_pe` (lowest engineering cost, 24% CAGR)
5. Microstructure infra (long-term highest payoff)

**Recommended opening:** C1 + C4 together — ensemble allocator +
2× leverage on `low_pe` as volatility-stabilizer. Estimated 20-24%
CAGR with Cal > 0.7. ~1-2 sessions.

Full detail in [`sessions/2026-04-25_handover.md`](sessions/2026-04-25_handover.md).

---

## Documentation map

**Live (canonical):**
- [`OPTIMIZATION_PROMPT.md`](OPTIMIZATION_PROMPT.md) — session-start prompt
- [`OPTIMIZATION_RUNBOOK.md`](OPTIMIZATION_RUNBOOK.md) — repeatable methodology
- [`ENGINE_STRATEGY_GUIDE.md`](ENGINE_STRATEGY_GUIDE.md) — pipeline architecture
- [`BACKTEST_GUIDE.md`](BACKTEST_GUIDE.md) — standalone scripts
- [`LIVE_TRADING_INTEGRATION.md`](LIVE_TRADING_INTEGRATION.md) — going-live plan

**Sessions:** [`sessions/`](sessions/) — chronological session handovers + code reviews

**Archive:**
- [`archive/audit-2026-04/`](archive/audit-2026-04/) — completed audit (P0-P3) + bias measurements
- [`archive/pre-engine-2026-03/`](archive/pre-engine-2026-03/) — pre-engine standalone-script era

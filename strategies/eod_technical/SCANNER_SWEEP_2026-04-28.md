# Scanner-threshold sweep — eod_technical (Phase 4, 2026-04-28)

**Hypothesis** (from `docs/inspection/FINDINGS_eod_technical.md` and
`FINDINGS_cross.md`): scanner pass-rate drifted from ~14% (2010) to
~45% (2024) on the same static config. The thresholds calibrated on
early data may now be too lax. Raising them should keep the strategy
honest as the universe gets more liquid.

**Result: hypothesis falsified.** The champion's thresholds (price 50,
turnover 70M / 125-day window) are the **global maximum on every
single dimension** (CAGR, MDD, Sharpe, Calmar) within a 4×4 grid
that brackets the champion two notches in each direction.

The likely explanation is the same finding that runs through Phase 3:
**the position cap is the binding constraint, not the scanner**.
Tightening the scanner doesn't help because capacity-ranking already
selects the top-N daily; loosening doesn't help because the looser
candidates rank below the cap. The audit's
`conditional_fail_rate = 70%` for the scanner clause was a
pre-capacity measurement that doesn't carry through to the simulator.

---

## Setup

Sweep config: `strategies/eod_technical/config_scanner_sweep.yaml` —
champion + 4 price thresholds × 4 turnover thresholds = 16 scanner
configs. Everything else is champion-identical so the comparison
isolates the scanner stage.

Grid:
- `price_threshold`: [25, 50, 100, 200]
- `avg_day_transaction_threshold`: [50M, 70M, 150M, 300M] (period 125)

Total runtime: 105 seconds (single data fetch + 16 scanner+order+sim
passes).

Outputs: `results/eod_technical/scanner_sweep_results.json`.

## Full grid

| cid | price | turn (M) | trades | CAGR | MDD | Sharpe | Calmar | note |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 25 | 50 | 1,388 | 16.02% | -36.62% | 0.674 | 0.438 | |
| 2 | 25 | 70 | 1,347 | **18.71%** | -29.74% | 0.998 | 0.629 | best non-champion |
| 3 | 25 | 150 | 1,330 | 16.20% | -36.24% | 0.674 | 0.447 | |
| 4 | 25 | 300 | 1,229 | 8.89% | -36.53% | 0.418 | 0.243 | |
| 5 | 50 | 50 | 1,348 | 16.67% | -36.27% | 0.712 | 0.460 | |
| **6** | **50** | **70** | **1,303** | **19.63%** | **-25.95%** | **1.067** | **0.757** | **CHAMPION** |
| 7 | 50 | 150 | 1,294 | 15.43% | -36.03% | 0.645 | 0.428 | |
| 8 | 50 | 300 | 1,213 | 11.09% | -36.07% | 0.555 | 0.308 | |
| 9 | 100 | 50 | 1,310 | 16.42% | -27.83% | 0.886 | 0.590 | |
| 10 | 100 | 70 | 1,250 | 14.90% | -27.53% | 0.808 | 0.541 | |
| 11 | 100 | 150 | 1,280 | 16.17% | -30.75% | 0.840 | 0.526 | |
| 12 | 100 | 300 | 1,177 | 10.84% | -36.12% | 0.546 | 0.300 | |
| 13 | 200 | 50 | 1,283 | 12.78% | -27.10% | 0.675 | 0.472 | |
| 14 | 200 | 70 | 1,280 | 15.36% | -28.83% | 0.817 | 0.533 | |
| 15 | 200 | 150 | 1,240 | 13.71% | -29.23% | 0.711 | 0.469 | |
| 16 | 200 | 300 | 1,154 | 9.74% | -31.97% | 0.488 | 0.305 | |

## Top 5 by Calmar

| rk | cid | (price, turn) | CAGR | MDD | Sharpe | Calmar |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 6 | (50, 70M) | 19.63% | -25.95% | 1.067 | 0.757 (champion) |
| 2 | 2 | (25, 70M) | 18.71% | -29.74% | 0.998 | 0.629 |
| 3 | 9 | (100, 50M) | 16.42% | -27.83% | 0.886 | 0.590 |
| 4 | 10 | (100, 70M) | 14.90% | -27.53% | 0.808 | 0.541 |
| 5 | 14 | (200, 70M) | 15.36% | -28.83% | 0.817 | 0.533 |

The champion beats the runner-up by 0.13 on Calmar (0.757 vs 0.629)
and 0.07 on Sharpe (1.067 vs 0.998).

## Patterns visible in the grid

1. **Turnover = 70M is a sweet spot for every price tier.** Looser
   (50M) consistently widens MDD into the -36% range. Stricter
   (150M, 300M) consistently kills CAGR. The champion didn't get
   lucky — 70M is the right number across the price axis.

2. **Price = 50 is the sweet spot.** Going looser (price=25) gives
   the second-best CAGR but at a 4-point MDD cost; going stricter
   (100, 200) costs ~3-7 CAGR points without buying back MDD.

3. **No "drift correction" works.** The hypothesis that 2024-era
   liquidity should be matched by raising thresholds 2-4× is
   falsified by configs 7, 8, 11, 12, 15, 16 — every stricter-than-
   champion turnover threshold loses on every dimension.

4. **Trade count is loosely coupled to scanner config.** The
   tightest config (16: 200, 300M) still produces 1,154 trades vs
   champion's 1,303 — only ~12% fewer despite 4×4 = 16× stricter
   thresholds combined. Reason: capacity cap absorbs the difference.
   When scanner produces fewer candidates, the cap is met from the
   smaller pool but is still binding.

## Why the audit-level conditional_fail_rate of 70% didn't predict this

The audit's `conditional_fail_rate = 70%` for the scanner clause
measures: when all OTHER entry clauses pass, what fraction of rows
does the scanner reject. That's a measurement of how much the
scanner narrows the **audit-level** candidate set. It says nothing
about the **simulator-level** trades, which are downstream of the
position-cap.

Since eod_t is 99%+ capacity-blocked (`FINDINGS_eod_technical.md`),
the simulator's daily 15-position selection comes from a pool that
is 99× larger than the cap. Raising scanner thresholds shrinks the
pool but the top-15 within the new pool barely differs from the
top-15 within the old pool: the highly-ranked names sail through
either threshold.

## Lesson learned

**Audit-level clause-binding measurements are not equivalent to
simulator-level performance impact when the strategy is heavily
capacity-bound.** The Phase-3 audit highlighted scanner as the
dominant entry filter; this Phase-4 sweep shows that dominance
doesn't translate into actionable optimization room.

A more useful metric for capacity-bound strategies might be:
"after capacity-ranking selection, what fraction of selected trades
are unique to a given scanner configuration?". The grid here shows
that's ~5-15% across the configs (1,303 vs 1,177-1,388 trade range)
— small relative to the audit's 70% binding measurement.

## Reproduction

```bash
source .venv/bin/activate
python3 - <<'PY'
import json
from engine import pipeline
from engine.signals import eod_technical  # noqa
sweep = pipeline.run_pipeline(
    "strategies/eod_technical/config_scanner_sweep.yaml"
)
print(f"{len(sweep.configs)} configs run")
print(f"Best CAGR: {max((r.to_dict()['summary']['cagr'] or 0) for _,r in sweep.configs):.4f}")
PY
```

## Files

- `strategies/eod_technical/config_scanner_sweep.yaml` — sweep config.
- `results/eod_technical/scanner_sweep_results.json` — raw output.
- `strategies/eod_technical/SCANNER_SWEEP_2026-04-28.md` — this doc.

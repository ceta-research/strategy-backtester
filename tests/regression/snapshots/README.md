# Regression Snapshots

Each `*.json` file in this directory pins the expected metrics for a
specific result.json. The snapshot harness (`tests/regression/snapshot.py`)
captures and diffs against these pins with configurable tolerance.

## Contract

Snapshots represent the **authoritative** output after all known audit fixes.
A diff against the snapshot means the result has drifted — investigate before
re-capturing. Never capture a snapshot from a result you haven't reviewed.

## Capture

```bash
python -m tests.regression.snapshot capture <result.json> [name]
```

## Compare

```bash
python -m tests.regression.snapshot compare <name> <result.json>
```

Exit 0 = match within tolerance. Exit 2 = drift detected (pinned fields
exceed `abs: 1e-6, rel: 1e-4` thresholds).

## What is NOT pinned here

- Pre-fix (pre-2026-04-21) numbers. Those are archived in
  `docs/AUDIT_FINDINGS.md` as a delta table, along with the exact metric
  changes produced by each layer fix. Don't pin pre-fix baselines here
  — the harness would flag every correct post-fix run as a regression.
- Per-run volatile fields (timestamps, paths, intermediate equity
  curve length). Only the fields in `snapshot.PINNED_FIELDS` are captured.

## Current pins (baseline: tag `pre-strategy-rework`, commit `ca0b5a5`)

Captured 2026-04-22 from `results_v2/` post all P0+P1 audit fixes. The
three strategies with `status: COMPLETE` in `strategies/OPTIMIZATION_QUEUE.yaml`
are pinned. The two `AUDIT_BLOCKED` and one `AUDIT_RETIRED` strategies are
deliberately NOT pinned — their numbers are known to be biased and will
change materially when re-optimised.

| Snapshot | Source file | CAGR | Calmar | Sharpe | Trades |
|---|---|---:|---:|---:|---:|
| `eod_breakout_champion` | `results_v2/eod_breakout/champion.json` | 14.51% | 0.536 | 0.816 | 833 |
| `enhanced_breakout_round2` | `results_v2/enhanced_breakout/round2.json` | 15.09% | 0.594 | — | 396 |
| `momentum_cascade_champion` | `results_v2/momentum_cascade/champion_2005_2026.json` | 12.22% | 0.442 | 0.577 | 671 |

## Use during strategy rework

When reworking a strategy, produce a new `result.json` and run:

```bash
python -m tests.regression.snapshot compare <snapshot_name> <new_result.json>
```

Exit 0 = match within tolerance (`abs: 1e-6, rel: 1e-4`) — the rework left
metrics unchanged. Exit 2 = drift — expected when the rework itself changes
behavior; investigate the diff and either accept (re-capture) or fix.

**Do not re-capture casually.** A re-capture is a commit saying "this new
result is the authoritative baseline now." Treat it with the care of an
intentional snapshot update.

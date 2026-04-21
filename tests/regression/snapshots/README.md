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

## Next action

Post-fix snapshots should be captured from the migrated `results_v2/`
outputs once Layers 2 and 4 land (order_id + exit policy). Capturing now
would require re-capture after those layers move the numbers.

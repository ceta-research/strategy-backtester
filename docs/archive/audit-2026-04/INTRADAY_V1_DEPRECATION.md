# Intraday Simulator v1 Deprecation

**Deprecated:** 2026-04-21 (P2 audit sprint, D2).
**Removal target:** next minor release.

## Why

`engine/intraday_simulator.py` (v1) has a known stop-loss bug in the
SQL builder path:

```sql
-- engine/intraday_sql_builder.py:131 (v1 only)
b.close <= LEAST(e.entry_price * stop_factor, e.or_low)
```

For a long position, `LEAST` picks the LOWER of `entry * stop_factor`
and `or_low`. If the user set `stop_pct=0.02` (entry\*stop_factor=98
for entry=100) but `or_low=95` from the opening range, the effective
stop becomes `95` — further from entry than the user specified.
Stops fire ~3-5% later than requested across affected strategies.

`engine/intraday_simulator_v2.py` supersedes this. Per its own docstring:
> "OR low no longer used as floor".

v2 explicitly drops `or_low` from the stop expression.

## Migration

Set `pipeline_version: v2` in the strategy YAML. Example:

```yaml
static_config:
  pipeline_version: v2   # was "v1"
  # ... rest of config unchanged ...
```

Call sites:
- `engine/intraday_pipeline.py` dispatches on `pipeline_version` (default
  v2 since 2026-Q1).
- Standalone scripts calling `simulate_intraday` directly should import
  `engine.intraday_simulator_v2.simulate_intraday_v2` instead.

## Current impact scan

Grep as of 2026-04-21 — no production YAML uses `pipeline_version: v1`.
Remaining callers:
- `tests/test_intraday_simulator.py` — v1-specific regression tests.
- `tests/test_intraday_pipeline.py` — v1 dispatch path.
- `tests/verification/verify_orb.py` — legacy cross-check script.
- `scripts/archive/debug_pipeline.py` — archived; non-production.

All confined to tests and archived scripts; no live optimization runs
depend on v1.

## Deprecation mechanism

A one-time `DeprecationWarning` fires on the first call to
`simulate_intraday()` per process. The warning is intentionally
one-time (not per-call) so test suites using v1 are not flooded.
The warning message points at this doc.

## Removal checklist (when deletion lands)

1. Delete `engine/intraday_simulator.py`.
2. Delete `engine/intraday_sql_builder.py` v1 paths (lines guarded by
   version check in the builder).
3. Remove `pipeline_version == "v1"` dispatch in
   `engine/intraday_pipeline.py`.
4. Delete v1-specific tests: `tests/test_intraday_simulator.py`,
   v1-tagged cases in `tests/test_intraday_pipeline.py`,
   `tests/test_intraday_sql_builder.py` v1 cases.
5. Remove archived scripts that reference v1:
   `scripts/archive/debug_pipeline.py`.
6. Update BACKTEST_GUIDE.md to drop v1 references.

# NO MA FILTER experiment — eod_technical (2026-04-28)

**Hypothesis** (from Phase 3 audit, `docs/inspection/FINDINGS_eod_technical.md`):
the `close > n_day_ma` clause has `conditional_fail_rate = 0.13%` —
when all other entry clauses pass, the MA clause additionally rejects
only 0.13% of rows. Removing it should leave performance unchanged.

**Result: confirmed.** The no-MA variant produces a **byte-identical**
trade set to the champion: same 1,303 trades, same equity curve, same
metrics to full float precision.

---

## Setup

- Engine baseline: this commit's `engine/order_generator.py` adds an
  optional `disable_close_gt_ma` entry-config flag (default `False`,
  preserves byte-identical behavior on existing configs).
- New config: `strategies/eod_technical/config_no_ma_filter.yaml`
  (champion + `disable_close_gt_ma: [true]`).

## Results

| Metric | Champion | No-MA variant | Δ |
|---|---:|---:|---:|
| CAGR | 0.1963276938896985 | 0.1963276938896985 | 0.0 |
| MaxDD | -0.2594960154843559 | -0.2594960154843559 | 0.0 |
| Sharpe | 1.0666989228712191 | 1.0666989228712191 | 0.0 |
| Calmar | 0.7565730576758486 | 0.7565730576758486 | 0.0 |
| Total trades | 1,303 | 1,303 | 0 |

Trade-set diff:

- Intersection: 1,303 trades
- Only-in-champion: 0
- Only-in-no_ma: 0
- Jaccard: 1.0000

## Why is the audit-level 0.13% a complete no-op?

The audit measures clause-binding power **before** the position cap.
At audit level, removing the MA clause adds ~250 audit-level entries
(193,691 → 193,941, est.). After capacity-ranking those candidates,
the top-15 / top-N selected daily are identical because the
MA-rejected candidates rank below the cap. They never make it into
`simulator_trade_log`.

This pattern (audit-level binding power → zero capacity-effect) is
common when:
1. The strategy is heavily capacity-constrained (eod_t is 99%+
   blocked — `FINDINGS_eod_technical.md`).
2. The dropped clause is nearly subsumed by a stricter clause that
   keeps firing first (`close ≥ n_day_high` with a 5-day window
   essentially implies `close > 3-day MA` for a typical breakout).

## Recommendation

Either keep the clause as defensive belt-and-suspenders or drop it
to simplify the parameter surface. Mathematically it has zero
binding power on this champion. **No performance reason to make a
decision either way** — the choice is about config readability and
parameter-search efficiency only.

## Reproduction

```bash
source .venv/bin/activate
python3 - <<'PY'
import json
from engine import pipeline
from engine.signals import eod_technical  # noqa

champ = pipeline.run_pipeline("strategies/eod_technical/config_champion.yaml")
no_ma = pipeline.run_pipeline("strategies/eod_technical/config_no_ma_filter.yaml")
_, rc = champ.configs[0]; _, rn = no_ma.configs[0]
dc, dn = rc.to_dict(), rn.to_dict()
for k in ["cagr", "max_drawdown", "sharpe_ratio", "calmar_ratio"]:
    print(f"{k}: champ={dc['summary'][k]} no_ma={dn['summary'][k]}")
PY
```

## Files modified

- `engine/config_loader.py` — `_build_entry_config_default` accepts
  the new `disable_close_gt_ma` key (default `[False]`).
- `engine/order_generator.py` — `add_entry_signal_inplace` reads the
  flag and replaces `close > n_day_ma` with `pl.lit(True)` when set.
  The audit clause-mirror column also reflects the flag for
  consistency.
- `strategies/eod_technical/config_no_ma_filter.yaml` — new test
  config.

## Tests

- 435/435 pass after the changes.
- Champion regression confirmed byte-identical to
  `champion_pre_audit_baseline.json` with the flag omitted (default
  False).

# momentum_dip_quality -- audit-retired 2026-04-22

## Decision

**Retired.** Honest CAGR 5.08% < NIFTYBEES buy-and-hold ~12-13% over the same 2010-2026 window.

## Evidence

Full A/B report: `docs/audit_phase_8a/momentum_dip_quality.md`

| Variant | CAGR | Calmar | Sharpe | MDD | Trades |
|---------|-----:|-------:|-------:|----:|-------:|
| Legacy (`full_period` universe) | +22.71% | 0.55 | 1.20 | -41.2% | 297 |
| Honest (`point_in_time` universe) | +5.08% | 0.14 | 0.19 | -35.6% | 225 |
| Delta | -17.63pp | -0.41 | -1.00 | +5.6pp | -72 |

## Root cause

`period_universe_set` computes a static full-period average turnover filter
across 2010-2026 (look-ahead + survivorship bias). The scanner already
computes correct per-day rolling filters, but the strategy discards the
scanner output and uses `period_universe_set` as the hard universe gate.

## Paths considered

- **Path A (chosen) -- Retire.** Remove from active optimization queue.
  Honest performance does not justify compute or code investment.
- **Path B -- Re-optimize as-is** with `universe_mode: "point_in_time"`.
  Likely still underperforms index. 10-30h compute for marginal benefit.
- **Path C -- Architecture cleanup + re-optimize.** Delete
  `period_universe_set`, use `scanner_config_ids.is_not_null()`. Sound
  engineering but the honest base is too weak to justify.

## Actions taken

1. `OPTIMIZATION_QUEUE.yaml`: status set to `AUDIT_RETIRED`
2. Publishing gate: do NOT cite momentum_dip_quality CAGR/Calmar in any
   blog, video, or strategy card. The legacy numbers are invalid.
3. Code is preserved as-is (no deletion) for reference. The `universe_mode`
   flag remains available if a future re-optimization is attempted.

## Related

- `momentum_top_gainers` uses the same architecture. Status: `AUDIT_BLOCKED`
  pending full-NSE A/B via CR API.
- `momentum_rebalance` has separate bias (same-bar entry). Status:
  `AUDIT_BLOCKED` pending full-NSE A/B.
- Scanner architecture cleanup (Critical 3 in NEXT_SESSION_HANDOVER.md)
  is deferred unless one of these strategies is revived.

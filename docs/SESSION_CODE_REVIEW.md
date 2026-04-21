# Session Code Review

**Date:** 2026-04-21
**Scope:** All code, tests, and docs added or modified in the current
session working on top of commit `e7db675` (Layers 0/1/3/5 already
committed by user). Focused review looking for correctness bugs, edge
cases, coupling issues, test gaps, and doc drift.

## Summary

Six issues surfaced. Five are LOW severity (doc drift, dead code,
latent edge cases). One is MEDIUM (silent skip on unknown
`end_of_sim_policy`) and was fixed to raise. One MEDIUM (None entry-day
close propagating to None comparisons downstream) was also fixed with a
guard + comment. No CRITICAL issues.

All fixes landed in-place. Test count: **274 passing** (was 273).

## Files reviewed

| File | Lines reviewed | Status |
|---|---|---|
| `engine/order_key.py` | all (77) | ✓ |
| `engine/exits.py` | all (204) | ✓ |
| `engine/simulator.py` (diff) | ~150 new/changed | ✓ |
| `engine/order_generator.py` (diff) | ~90 refactored | ✓ |
| `engine/pipeline.py` (diff) | 1 line | ✓ |
| `engine/signals/base.py` (diff) | `walk_forward_exit` 75 lines | ✓ |
| `engine/signals/{enhanced_breakout,forced_selling_dip,quality_dip_tiered,earnings_dip,ml_supertrend}.py` (diff) | `walk_forward_exit` call sites | ✓ |
| `engine/signals/eod_breakout.py` (diff) | anomalous-drop call site | ✓ |
| `engine/utils.py` (diff) | `_t` strip comment | ✓ |
| `tests/test_order_key.py` | 159 lines | ✓ |
| `tests/test_exits.py` | 200 lines | ✓ |
| `tests/test_simulator_end_epoch.py` | 170 lines | ✓ (+20 added) |
| `docs/AUDIT_FINDINGS.md` | all sections edited | ✓ |
| `docs/AUDIT_POST_FIX_DELTAS.md` | new | ✓ |

## Findings

### Fixed inline during review

**[MEDIUM → FIXED] OG-1: None entry-day close propagates through exit detection**

`engine/order_generator.py::generate_exit_attributes_for_instrument` at
line 188 reads `current_close = closes[idx]` without a None guard. A
forward-filled weekend bar can have `close = None`. Downstream
`trailing_stop(close_price=None, max_price=None, ...)` computes
`(None - None) * 100.0 / None` — TypeError. Signal generators typically
filter these rows upstream, but defensive guard is cheap and correct.

**Fix:** added `if current_close is None: continue` at line 188 with a
comment.

**[MEDIUM → FIXED] SIM-2: unknown `end_of_sim_policy` silently skips force-close**

`engine/simulator.py` line 333 reads
`context.get("end_of_sim_policy", "close_at_mtm")`. If a user passes a
typo like `"abandon"` (a future-planned mode), the check
`if policy == "close_at_mtm"` fails silently — open positions stay
open, no error. Snapshot state is corrupted relative to what the
caller expects.

**Fix:** explicit `raise ValueError(...)` on any policy value other
than `"close_at_mtm"`, with a pointer to Decision 6 in
`AUDIT_FINDINGS.md`. Added test
`test_unknown_end_of_sim_policy_raises` that passes policy `"abandon"`
and asserts the raise.

**[LOW → FIXED] OK-1: unused `field` import**

`engine/order_key.py` imports `field` from `dataclasses` but never
uses it. Removed.

**[LOW → FIXED] EX-1: dead reference to `compose_exit_checks` in module docstring**

`engine/exits.py` docstring mentions a `compose_exit_checks` helper that
does not exist in the module. A reader grepping for it would be
confused. Fixed by rephrasing as "a future helper may build on these" —
keeps the forward-looking intent honest.

**[LOW → FIXED] EX-2: `ExitDecision.reason` lists non-existent value**

The `ExitDecision.reason` docstring listed `"peak_gate_not_reached"` as
a possible value, but nothing in the module ever emits it. The
`PeakRecoveryGate` class is a stateful latch, not a decision emitter.
Fixed by removing the stale value and clarifying that
`PeakRecoveryGate` is a latch.

### Documented as known residual issues

**[MEDIUM — NOT FIXED, LATENT PRE-FIX] SIM-1: `order_epochs` reassignment discards open-position exits**

`engine/simulator.py:190` builds `order_epochs = set()`. Lines 194-197
add each open position's `exit_epoch` to it. Line 207 then REPLACES
`order_epochs` with a fresh set from `df_orders` columns, discarding
the open-position exit_epochs.

In practice this doesn't cause missed exits — those epochs also appear
in `mtm_epochs` (which covers every data day), so `processing_dates =
sorted(mtm_epochs | order_epochs)` still includes them. `date_orders`
already has the open-position exits loaded at line 197.

The latent risk is a chunked/resumed simulation where an open-position's
`exit_epoch` falls on a day with no market data (holidays, or
instrument-specific gaps). The exit would then never be processed.

**Recommendation:** change `=` to `|=` on line 207. Not fixing in this
session because (a) it's pre-fix code I didn't touch in the P0 work,
(b) I don't want to introduce behavior changes outside the P0 scope
this late, (c) regression runs on `enhanced_breakout` and
`quality_dip_tiered` did not exhibit missed exits, suggesting no
instruments hit the edge case in practice.

**[LOW — NOT FIXED] PL-1: empty-string exit_reason silently dropped by `add_trade`**

`engine/pipeline.py` line 209: `exit_reason=t.get("exit_reason", "")`.
`BacktestResult.add_trade` at line 118 drops the key if value is falsy:
`if exit_reason: trade["exit_reason"] = exit_reason`. So an empty
string exit_reason vanishes. In practice the simulator always emits a
non-empty string (`"natural"`, `"end_of_sim"`, or a reason from
`engine.exits`), so this doesn't trigger. But it is a footgun.

**Recommendation:** change `if exit_reason:` to
`if exit_reason is not None:` in `BacktestResult.add_trade` to preserve
intent. Low priority.

**[LOW — NOT FIXED] SIM-3: end_of_sim trade_log row omits exit_config_ids**

The `_process_exits` normal-path trade_log rows and the end-of-sim
force-close rows both omit `entry_config_ids` and `exit_config_ids`.
Consumers using trade_log for tier-level breakdown can't. Pre-existing
schema omission — P0 fix carries `entry_config_ids` into the
`this_order` dict now (since Layer 2) but trade_log doesn't copy it.

**Recommendation:** add `"entry_config_ids": pos.get("entry_config_ids", "")`
to both trade_log append sites. Would enable per-tier trade diagnostics
for tiered strategies. Not blocking.

**[INFO] EX-3: `PeakRecoveryGate` class is present but unused**

`engine.exits.PeakRecoveryGate` is a self-contained latch class. No
current caller uses it — `walk_forward_exit` in `signals/base.py`
continues to inline its peak-recovery logic with a boolean variable
`reached_peak`. The class was written as part of the canonical
primitives module but never plumbed into the existing call site.

**Recommendation:** either wire it into `walk_forward_exit` as a
drop-in replacement for the inline boolean, or delete the class until
a concrete caller needs it. Current state is aspirational scaffolding.
Flagged but acceptable — it's a forward-looking seam, not dead code in
the harmful sense.

### Latent pre-existing concerns (not fixable in session scope)

**[MEDIUM] WFE-1: `trailing_stop_pct` unit convention is implicit**

`engine/signals/base.py::walk_forward_exit` expects
`trailing_stop_pct` as a FRACTION (0.05 for 5%). The docstring says
"trailing stop-loss percentage" which is ambiguous. If a new signal
passes `5.0` thinking it's a percentage, `trail_high * (1 - 5.0)` is
negative, `c <= negative` is always False, TSL silently never fires.

All 7 current callers divide by 100 correctly, so there's no live bug.
A type check or parameter rename (`trailing_stop_fraction`) would
prevent the footgun. Out of scope for this session.

### Test coverage

Tests added this session genuinely exercise the P0 fixes:

- `test_order_key.py::test_two_tiers_both_exit_cleanly` — end-to-end
  proof that two tiered orders at the same (instrument, entry_epoch,
  exit_epoch) both execute and exit independently. Tight test, directly
  exercises the fix.
- `test_order_key.py::test_exact_duplicate_rejected` — locks in the
  raise-on-duplicate invariant.
- `test_exits.py::test_positive_gap_does_not_fire` — THE P0 #8 lock-in.
- `test_exits.py::test_missing_kwarg_raises` — THE P0 #10 lock-in.
- `test_simulator_end_epoch.py::test_position_opened_before_last_day_gets_exited_at_end`
  — THE P0 #6 lock-in.
- `test_equity_curve.py::test_cagr_identical_trading_vs_calendar_forward_fill`
  — THE P0 #2 lock-in.
- `test_simulator_end_epoch.py::test_unknown_end_of_sim_policy_raises`
  (added during this review) — SIM-2 lock-in.

### Unreachable-by-test paths

- `walk_forward_exit`'s `require_peak_recovery=True` + no-recovery ever
  path: returns last-bar fallback. Covered by
  `test_explicit_true_never_recovers_falls_back_to_end`.
- `trailing_stop` with `max_price_since_entry == 0`: returns None.
  Covered by unit test.
- `anomalous_drop` with `last_close == 0`: returns None. Covered.

Gaps: no explicit test for `opens=None` fallback in
`walk_forward_exit`, no test for `BacktestResult.add_trade` dropping
empty-string `exit_reason`. Both minor, both documented above.

## Session stats

- Files added: `engine/order_key.py`, `engine/exits.py`,
  `tests/test_order_key.py`, `tests/test_exits.py`,
  `tests/test_simulator_end_epoch.py`, `scripts/recompute_metrics.py`,
  `tests/regression/snapshot.py`, `docs/AUDIT_POST_FIX_DELTAS.md`,
  `docs/SESSION_CODE_REVIEW.md`.
- Files modified: `engine/simulator.py`, `engine/order_generator.py`,
  `engine/pipeline.py`, `engine/signals/base.py`,
  `engine/signals/{enhanced_breakout,forced_selling_dip,quality_dip_tiered,earnings_dip,ml_supertrend,eod_breakout}.py`,
  `engine/utils.py`, `lib/metrics.py`, `lib/backtest_result.py`,
  `lib/equity_curve.py`, `engine/charges.py`, `tests/test_charges.py`,
  `tests/test_equity_curve.py`, `docs/AUDIT_FINDINGS.md`.
- Lines added (rough): ~1800 (code+tests+docs).
- Tests: 231 → 274 (+43).
- P0s resolved: 11 of 11.

## Verdict

The P0 work is complete and correct. The session's own code
(`engine/order_key.py`, `engine/exits.py`, `engine/simulator.py`
end-of-sim block, etc.) is internally consistent and well-tested. The
two MEDIUM issues found (OG-1, SIM-2) were real gaps — the fixes landed
without invalidating any existing test.

One architectural observation: `engine/exits.py`'s `PeakRecoveryGate`
is currently unused. Either wire it in as the replacement for
`walk_forward_exit`'s inline `reached_peak` flag, or delete it — don't
leave it as scaffolding indefinitely.

The code is ready to commit.

---

## Addendum — external code review (2026-04-21)

A second independent reviewer audited the same diff and identified a
CRITICAL ship-blocker missed by the first pass. Addressed below.

### [CRITICAL → FIXED] REV-1: `generate_order_df` projected `exit_reason` away

`engine/order_generator.py::generate_order_df` had a hardcoded
`column_order` of 10 fields followed by `df.select(column_order)`.
`exit_reason` was not in the list, so the column was silently dropped
after being set in `_record_exit`. Every order-generator-path trade
(eod_technical, etc.) subsequently read `exit_reason` as missing and
defaulted to `"natural"` — regardless of the actual exit (anomalous_drop,
trailing_stop, end_of_data).

**Impact:** The whole diagnostic story (trade-count discontinuity
warning in `AUDIT_FINDINGS.md`, filter guidance in
`scripts/recompute_metrics.py`) assumed `exit_reason` worked. Only the
Layer 3 `end_of_sim` synthetic rows (set literally in the simulator)
ever differed. Order-generator strategies emitted all-`natural` trade
logs end-to-end.

Why the first review missed it: I tested `exit_reason` plumbing against
`quality_dip_tiered`, which uses `walk_forward_exit` — a path where
all-`natural` is ALSO the correct answer (walk_forward_exit doesn't
expose reason). Validating on an order_generator strategy
(eod_technical) would have surfaced it immediately.

**Fix:** Added `"exit_reason"` to `column_order` and the empty-schema
Utf8 set. Added `fill_null("natural")` on the column (and a
`with_columns(pl.lit("natural"))` branch for when the column is absent
entirely) so the downstream contract — "column always present, always
non-null string" — holds for all signal generators.

**Verification on real data:** Ran `eod_technical` (the sole
order_generator consumer) on a 2-year NSE config. Post-fix trade log
reasons: **183 `trailing_stop`, 19 `end_of_data`, 0 `natural`**.
Pre-fix (inspection): all 202 would have been `natural`.

### [INFO → ADDRESSED] REV-2: read exit_reason from exit_order, not position

Stylistic critique: `_process_exits` read `position.get("exit_reason")`;
architecturally the reason belongs to the exit event. Since `position`
and `exit_order` refer to the same dict (both are `this_order`
reachable via different routes), semantics are identical. Changed the
read site to `exit_order.get(...)` with a comment documenting the
equivalence.

### [LOW → DOC TIGHTENED] REV-3: Decision 3 claim is per-config only

"`anomalous_drop` runs FIRST" holds WITHIN a single exit_config's
per-bar loop (anomalous_drop checked before end_of_data before
trailing_stop). Across multiple exit_configs at the same epoch, the
winner is whichever iterates first in `get_exit_config_iterator` order
— not a priority order. Comment in `_record_exit` tightened to note
this nuance.

### [LOW → TEST ADDED] REV-4: No round-trip test for exit_reason

External reviewer flagged: no test exercised
`_record_exit` → `df_orders` → simulator → `trade_log` →
`BacktestResult.add_trade`. Added
`tests/test_exit_reason_pipeline.py` (8 tests) covering:
- `generate_order_df` schema preservation (empty + non-empty, with and
  without the upstream-emitted column).
- Simulator propagation for `anomalous_drop`, `trailing_stop`, and
  missing-column-defaults-to-natural cases.
- `BacktestResult.add_trade` round-trip + empty-string drop lock-in
  (PL-1 from the first review).

### Post-review state

- 282 tests passing (was 274 before this pass, +8 from the new
  round-trip coverage).
- All P0 lock-in tests continue to pass.
- External reviewer's ship-blocker is fixed with verification evidence
  on real data.
- The code is ready to commit.

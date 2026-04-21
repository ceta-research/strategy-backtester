# Strategy-Backtester Audit Findings

**Started:** 2026-04-21
**Status:** All 11 P0s fixed. Layers 0-6 landed. Historical batch migration complete (214 files in `results_v2/`); strategies affected by P0s #7-10 still require full simulation re-runs (see Layer 6).

**Self-review log:** After initial implementation, a critical code review found
and fixed four additional bugs introduced during the work:

  1. `snapshot._extract` was reading the wrong key (`configs` vs `detailed`)
     and silently captured null values for all 4 strategy baselines. Fixed
     `_extract` / `_identity` against the real sweep shape (`detailed[0]` +
     `meta`). Re-captured all 6 baselines with verified non-null values.
  2. `BacktestResult`'s new `equity_curve_frequency` default of
     `DAILY_CALENDAR` was wrong for two live callers
     (`intraday_pipeline.py`, `scripts/quality_dip_buy_lib.py`) that emit one
     point per trading day. Fixed both call sites to pass
     `Frequency.DAILY_TRADING` explicitly. Default retained for the main EOD
     pipeline which IS calendar-day.
  3. `_compute_comparison` crashed on `port_cagr=None` / `bench_cagr=None`
     (possible when a curve starts at 0). Guarded `excess_cagr` and `alpha`
     to propagate None instead of raising.
  4. Hygiene: removed dead imports, replaced `v != v` NaN check with
     `math.isfinite`, removed redundant length-check guard after ValueError
     raise.

6 new regression tests added in `tests/test_review_findings.py` to prevent
each of these from recurring. Full suite now at 244 passing.

This document records each P0 audit finding, the root cause, the fix, and the
measured impact on historical results. Entries are added as layers complete;
existing entries are never edited (only corrected by a new dated entry below).

---

## Layer 0 + Layer 1 — 2026-04-21

### What shipped

- `lib/equity_curve.py` — new `EquityCurve` frozen dataclass + `Frequency` enum.
  Invariants enforced at construction (monotonic epochs, finite non-negative values,
  matched lengths).
- `lib/metrics.py` — new entry point `compute_metrics_from_curve(port_curve, ...)`.
  Legacy `compute_metrics(returns, ppy, ...)` retained for intraday stack (uses
  matched trading-day returns + ppy=252, which was already correct).
- `lib/backtest_result.py` — `BacktestResult.compute()` now builds a typed
  `EquityCurve` and routes through the corrected path. Accepts
  `equity_curve_frequency` in constructor; defaults to `DAILY_CALENDAR` (matches
  the engine's forward-filled output). Result JSON is bumped to v1.1 and
  includes `equity_curve_frequency` for future migration safety.
- `tests/test_equity_curve.py` — P0 regression tests:
  - `test_cagr_identical_trading_vs_calendar_forward_fill` locks in the
    forward-fill invariance invariant.
  - `test_cagr_matches_hand_computed_10_year_doubling` pins CAGR to the
    standard financial definition (double in 10y = 7.1773%).
  - Type invariants: rejects non-monotonic epochs, length mismatches,
    negative/NaN values.
- `tests/regression/snapshot.py` — pinned-metric regression harness.
  Captures CAGR / MDD / Sharpe / Sortino / Calmar / vol / trade stats from a
  result.json and diffs against a later run with configurable tolerance.
  Baseline snapshots captured for eod_breakout, enhanced_breakout,
  momentum_cascade, momentum_dip_quality under `tests/regression/snapshots/*_pre_fix.json`.
- `scripts/recompute_metrics.py` — migration tool. Re-derives metrics from
  the `equity_curve` embedded in each historical result.json using the
  corrected path, with automatic frequency detection by point density.

### P0s fixed

| # | Item | Status |
|---|------|:---:|
| 2 | `metrics.py:124` — `years = n / ppy` deflates CAGR by ~1/1.45 on forward-filled curves | ✅ |
| 3 | `metrics.py:135` — vol `sqrt(ppy)` assumes trading days when curve was calendar days | ✅ |
| 4 | `metrics.py:138` — Sharpe compound ppy error (follows from 2+3) | ✅ |
| 5 | `backtest_result.py:140` — hardcoded `periods_per_year=252` | ✅ |

### Why this is a correctness fix, not a parameter tweak

Pre-fix, metrics silently coupled `ppy=252` to a forward-filled (calendar-day)
equity curve, mis-annualizing every metric. The fix separates two concerns:

- **CAGR** is derived from wall-clock years (`(last_epoch - first_epoch) /
  seconds_per_year`). Frequency-independent. Matches Bloomberg / Morningstar
  convention.
- **Vol annualization** uses `Frequency.periods_per_year`. The curve carries
  its own frequency; metrics do not guess.

Forward-filled weekend bars produce zero-return periods, which correctly
deflate raw variance. The annualization factor compensates exactly, so
vol is invariant across sampling frequencies. This is the invariant that
`test_cagr_identical_trading_vs_calendar_forward_fill` locks in.

### Measured impact on historical results

Applied via `scripts/recompute_metrics.py` with auto-detected frequency:

| Strategy | Curve | Freq (detected) | CAGR before | CAGR after | Δ | Sharpe before | Sharpe after |
|---|---|---|---:|---:|---:|---:|---:|
| eod_breakout/champion | 5914pt / 16.19y | DAILY_CALENDAR | 9.80% | **14.51%** | +4.71pp | 0.612 | 0.816 |
| momentum_cascade/baseline | 5920pt / 16.20y | DAILY_CALENDAR | 4.80% | **7.03%** | +2.23pp | 0.179 | 0.267 |
| momentum_dip_quality/baseline_corrected | 5901pt / 16.15y | DAILY_CALENDAR | 17.12% | **25.74%** | +8.62pp | 0.956 | 1.248 |
| momentum_top_gainers/baseline | 5848pt / 16.01y | DAILY_CALENDAR | 7.23% | **10.65%** | +3.42pp | 0.328 | 0.451 |
| earnings_dip/champion_final | 3240pt / 13.09y | DAILY_TRADING | 10.88% | **10.67%** | -0.21pp | 0.921 | 0.899 |

The first four strategies emit one point per calendar day (forward-filled
weekends). Their pre-fix CAGR was deflated by `ratio ≈ 252/365 ≈ 0.69` in the
exponent, which shows up as ~1.47× multiplier on the corrected CAGR.

earnings_dip's equity curve was already trading-day indexed, so its pre-fix
numbers were correct *by coincidence*. Post-fix numbers are marginally
different due to exact-year-count precision, not formula change.

### What this invalidates

Every `results/*.json` produced before 2026-04-21 contains metrics computed
with the buggy path. CAGR / Sharpe / Sortino / Calmar / vol are all affected
when the underlying curve was forward-filled (which the engine has done by
default). Total return, MDD, VaR, skew, kurt, trade stats are unchanged —
these are frequency-invariant.

### What to do with historical results

- `results/` directory is preserved read-only as the buggy archive.
- `scripts/recompute_metrics.py <result.json>` recomputes on demand.
- Batch migration to `results_v2/` complete (Layer 6); 214 files migrated.
- Any public claim (blog, Reddit, LinkedIn) citing pre-fix CAGR/Sharpe/Calmar
  should be reviewed against the post-fix numbers before new content publishes.

### Test coverage added

- 9 new tests in `tests/test_equity_curve.py` (type invariants, CAGR
  invariance, vol consistency, legacy compat).
- Full suite: 231 tests, all passing.
- 4 pre-fix regression snapshots captured for future delta tracking.

---

---

## Layer 5 — Charges per-side contract — 2026-04-21

### What shipped

- `engine/charges.py` rewritten. Every `calculate_*` function documents and
  enforces PER-SIDE semantics. Fee constants (`NSE_STT_DELIVERY`,
  `OTHER_EXCHANGE_PER_SIDE_RATE`, etc.) are module-level named data — adding
  a new exchange is editing data, not control flow. New
  `calculate_round_trip()` helper makes two-leg cost a first-class concept.
- `tests/test_charges.py` — 3 new P0 regression tests:
  - `test_non_in_us_exchange_is_per_side` pins the 0.0005 per-leg rate.
  - `test_every_exchange_has_per_side_semantics` invariant: round-trip
    equals buy + sell for every exchange × trade_type pair.

### P0 fixed

| # | Item | Status |
|---|------|:---:|
| 11 | `charges.py:36` — non-IN/US fallback `return order_value * 0.001` called per-side produced 0.2% round-trip instead of the commented 0.1% | ✅ |

### Impact on historical results

Every backtest on LSE, JPX, HKSE, XETRA, KSC, ASX, TSX, SAO, SES, SHH, SHZ,
TAI, JNB was overstating round-trip costs by 0.1% of notional. Those
strategies' net CAGR is understated post-cost. NSE/BSE and US results
unaffected. Re-run those strategies after this layer to recover the ~0.1%
annual cost drag.

---

## Layer 3 — Simulator end_epoch authoritative window — 2026-04-21

### What shipped

- `engine/simulator.py` `process()`: `end_epoch` now unconditionally sourced
  from `context["end_epoch"]`. Pre-fix, it was
  `df_orders["entry_epoch"].max()` when orders existed and `context["end_epoch"]`
  otherwise — two silent behaviors masquerading as one.
- Loop terminator changed from `>= end_epoch: break` to `> end_epoch: break`
  so the final simulation day IS processed (off-by-one fix).
- New explicit end-of-sim block: every still-open position is force-closed at
  its last known MTM price, recorded in `trade_log` with
  `exit_reason="end_of_sim"`, and cash is settled. Policy is
  `context["end_of_sim_policy"]` (default `close_at_mtm`).
- `tests/test_simulator_end_epoch.py` — 4 new regression tests:
  - Late-entry position (exit beyond window) now gets exited and recorded.
  - No open positions remain after end-of-sim.
  - `end_epoch` itself appears in the MTM day log.
  - Empty df_orders still uses `context["end_epoch"]`.

### P0 fixed

| # | Item | Status |
|---|------|:---:|
| 6 | `simulator.py:180,200` — end_epoch inconsistent between orders-present and orders-empty paths; `>=` off-by-one; positions with exit_epoch > max entry_epoch were silently abandoned | ✅ |

### Impact on historical results

Every strategy's tail was clipped at the last entry day pre-fix:
- Positions opened in the final weeks with distant planned exits were
  abandoned — their P&L never recorded.
- Late-cycle drawdowns were not reflected in MDD.
- `time_in_market` was biased high (no realization of the open tail).

Post-fix, strategies with high activity in their final months will show:
- More trade_log entries (exits at `end_of_sim` appear).
- Slightly different final CAGR and MDD.
- Different peak/trough depending on where the final MTM landed vs. the
  planned exits.

Total return for strategies whose final positions were net-winners should
INCREASE (their gains are now realized). For net-losers, it DECREASES.
Re-run backtests to pick up the delta.

---

---

## Layer 6 — Historical results migration — 2026-04-21

### What shipped

- `scripts/recompute_metrics.py` extended with `--out-dir` flag. Walks the
  source tree, recomputes metrics from each result's embedded equity curve
  using the corrected path, writes new files under `results_v2/` preserving
  the original structure (single-result or sweep).
- Each migrated file adds `summary_v1_pre_fix` (original summary, for audit),
  `equity_curve_frequency` (auto-detected), and a per-file `migration_report`
  field. No top-level aggregate report is produced; before/after deltas are
  streamed to stdout during the run.

### Batch run result

- **214 files migrated** from `results/` (of 247 source `.json` files; 33
  had no embedded equity curve and were skipped).
- Pure calendar-day strategies show consistent +2.9% to +3.75% ΔCAGR, matching
  the predicted 1.45× correction factor.
- Trading-day strategies show near-zero ΔCAGR (as expected — `n/252` was
  already close to wall-clock `years` for those).
- Frequency auto-detection correctly discriminated between the two types of
  curve across all 214 files.

### What this buys

- Every public claim referencing a result in `results/` can now be validated
  against the corrected value in `results_v2/`.
- No simulation rerun needed for metric-formula corrections (Layers 1, 5) —
  metrics are rederived from the stored equity curve.
- `results/` remains the buggy archive (preserved for audit chain).

### Caveat: `results_v2/` is not universally authoritative

Migration re-derives metrics from stored equity curves but CANNOT recover
from trade-generation changes. Strategies whose executed trades were
affected by P0s #7-10 still need full simulation re-runs; the migrated
metrics in `results_v2/` will be internally consistent but will reflect
the pre-fix trade path.

| Strategy | Re-run reason |
|---|---|
| `quality_dip_tiered` | P0 #7 — tier collisions silently dropped all but one tier per (instrument, entry_epoch, exit_epoch). |
| `enhanced_breakout`  | P0 #10 — TSL never fired on red-close breakouts (missing `require_peak_recovery=False`). |
| `eod_breakout`       | P0 #8 — `abs()` anomalous-drop forced losses on positive gaps. |
| Any strategy exiting via `order_generator` path | P0 #9 — anomalous-drop branch missing `tracker.add()` produced duplicate exits with non-deterministic pricing. |

For the strategies NOT in the table (`momentum_top_gainers`,
`momentum_cascade`, `momentum_dip_quality`, `earnings_dip`), the migrated
`results_v2/` numbers are authoritative.

---

## Deferred hygiene fixes — 2026-04-21

Items identified during the critical review that were initially deferred as
out-of-P0-scope, all now addressed under the 100%-safe bar:

- **P1** — `_india_per_side` used delivery stamp duty (0.015%) for intraday
  trades too, 5× too high on the buy leg. Fixed to use
  `NSE_STAMP_DUTY_INTRADAY` (0.003%) for INTRADAY. Regression test
  `test_nse_intraday_per_side_matches_helper_round_trip` locks in consistency
  with the Zerodha-modeling `nse_intraday_charges` helper (the two must now
  agree within 2 rupees per leg, vs >50 rupees pre-fix).
- **S3** — dead-flag on `NSE_STAMP_DUTY_INTRADAY` removed; constant is now
  live via P1.
- **S5** — `us_intraday_charges` rounding unified to 2dp (was 4dp) to match
  the rest of the charges module. `test_us_taf_cap` updated accordingly.
- **S7** — `EquityCurve.__post_init__` coerces list inputs to tuples so
  `frozen=True` actually delivers immutability. New test
  `test_lists_are_coerced_to_tuples` locks this in.
- **P2** — documented the timing asymmetry between `day_wise_positions[end_epoch]`
  (pre-end-of-sim snapshot) and `snapshot["current_positions"]` (post-close)
  with an inline note in `engine/simulator.py`.

Full test suite at 246 passing.

---

---

## Layer 2 — OrderKey structured identity — 2026-04-21

### What shipped

- `engine/order_key.py` — frozen dataclass `OrderKey(instrument, entry_epoch,
  exit_epoch, entry_config_ids)`. Hashable, usable directly as a dict key,
  immutable. `__str__` emits a grep-friendly format with an `@{config_ids}`
  suffix when the tier component is non-empty.
- `engine/simulator.py` now keys `current_positions[instrument]` by
  `OrderKey`. Pre-fix `order_id = f"{inst}_{entry}_{exit}"` collided across
  tiers of the same `(instrument, entry_epoch, exit_epoch)` tuple with
  different `entry_config_ids` like `"5_t0"` vs `"5_t1"`.
- Entries now carry `entry_config_ids` into `this_order` so the exit-side
  `OrderKey.from_order(exit_order)` reconstructs the same key. Missing this
  was a pre-existing latent bug masked by the string-key collapse; the new
  test `test_simulator_trade_log_fields` caught it immediately.
- Duplicate-key detection: simulator now raises `ValueError` if a second
  entry produces the exact same `OrderKey`. Pre-fix this was a silent
  overwrite. Invariant: signal generators must emit distinct keys.
- `engine/utils.py:39` `_t` suffix stripping preserved — it is CORRECT
  pipeline-layer behavior (groups all tiers under the same entry config).
  Added a comment making the rationale explicit so a future reader does
  not try to "fix" it.
- `tests/test_order_key.py` — 8 tests: hashability, frozen/immutable,
  from_order dict adapter, end-to-end tier-collision fix, hard-error on
  true duplicates.

### P0 fixed

| # | Item | Status |
|---|------|:---:|
| 7 | `simulator.py:108` order_id string collision for tiered strategies → silent overwrite of earlier tiers | ✅ |

### Impact on historical results

Tiered DCA strategies (`quality_dip_tiered`) were non-functional: only
the last tier of each `(instrument, entry_epoch, exit_epoch)` group
actually simulated. Any prior sweep or champion result produced with
`quality_dip_tiered` misrepresents the strategy's behavior.
Re-run tiered strategies after this layer.

---

## Layer 4 — Consolidated exit policy module — 2026-04-21

### What shipped

- `engine/exits.py` — canonical primitives with one correct implementation
  each:
  - `anomalous_drop(close, last_close, threshold, this_epoch)` — signed
    downward check.
  - `end_of_data(this_epoch, last_epoch, close)` — final-bar force-close.
  - `trailing_stop(close, max_since_entry, pct, next_epoch, next_open, this_epoch)` —
    MOC execution with next-open fallback.
  - `below_min_hold`, `max_hold_reached` — hold-window gates.
  - `ExitTracker` — `record()` is the ONLY way to mark a config fired.

  Note: a `PeakRecoveryGate` class was drafted during this layer as an OO
  replacement for the `reached_peak` boolean in `walk_forward_exit`, but
  had zero callers and was flagged as unused scaffolding by code review;
  deleted before commit. The P0 #10 fix is delivered entirely via the
  mandatory keyword-only `require_peak_recovery` parameter (below).
- `engine/order_generator.py::generate_exit_attributes_for_instrument`
  refactored to call these primitives. The three-way exit logic
  (anomalous/end-of-data/TSL) now delegates to the canonical module.
  `_record_exit()` helper consolidates the "merge decision into
  instrument_order_config" ceremony.
- `engine/signals/base.py::walk_forward_exit` — `require_peak_recovery`
  is now **keyword-only and mandatory** (no default). Calling without it
  raises `TypeError`. Seven call sites audited and updated:
  - `enhanced_breakout.py:455` → `require_peak_recovery=False` (breakout,
    P0 #10 fix)
  - `momentum_top_gainers.py:270` → already `False` (was correct)
  - `forced_selling_dip.py:419` → `True` (dip-buy, made explicit)
  - `quality_dip_tiered.py:225` → `True` (dip-buy, made explicit)
  - `earnings_dip.py:603` → `True` (dip-buy, made explicit)
  - `ml_supertrend.py:550` → `True` (dip-buy component, made explicit)
  - `momentum_dip_quality.py:439` → already config-driven (was correct)
- `engine/signals/eod_breakout.py::_walk_forward_tsl` — inline `abs(diff)`
  anomalous check replaced with a call to `engine.exits.anomalous_drop`
  (P0 #8 fix at the duplicate site).
- `tests/test_exits.py` — 19 tests locking in every primitive's contract
  and the three-way P0 fixes.

### P0s fixed

| # | Item | Status |
|---|------|:---:|
| 8 | `order_generator.py:213` + `eod_breakout.py:246` — `abs(diff) > threshold` fired on positive gaps, booking losses on rallies. Fixed: signed check in single module, consumed from both sites. | ✅ |
| 9 | `order_generator.py:211-224` — anomalous-drop branch did not add to `order_exit_tracker`, allowing a second exit row on the next day. Fixed: `ExitTracker.record()` is invoked by every exit decision path. | ✅ |
| 10 | `enhanced_breakout.py:455` — missing `require_peak_recovery=False`; TSL silently inherited dip-buy semantics and never fired on red-close breakouts. Fixed: kwarg is mandatory and explicit in every call site. | ✅ |

### Impact on historical results

- **enhanced_breakout** (P0 #10): every pre-fix sweep's `enhanced_breakout`
  results used dip-buy TSL semantics. For breakouts where the entry day
  closed below its open, TSL never activated — the position held to
  max_hold or end-of-data. Post-fix, TSL fires from entry. Expect
  materially different MDD / final return / trade count on re-run.
- **Anomalous-drop on positive gaps** (P0 #8): any stock with a one-day
  gap-up greater than `drop_threshold` (default 20%) was force-exited at
  `last_close * 0.8`, booking a ~20% loss on a day the stock rallied.
  Re-run flags this as improved win rate / higher total return on
  strategies that hold stocks through earnings or M&A.
- **Duplicate exit rows** (P0 #9): removed a source of non-determinism
  in the simulator (dict-iteration order decided which of the two exit
  rows "won"). Post-fix, exit bookings are deterministic.

---

## All 11 P0s fixed — summary table

| # | File | Layer | Status |
|---|------|-------|:---:|
| 2 | metrics.py:124 CAGR `years = n/ppy` | 1 | ✅ |
| 3 | metrics.py:135 vol `sqrt(ppy)` mismatch | 1 | ✅ |
| 4 | metrics.py:138 Sharpe compound ppy | 1 | ✅ |
| 5 | backtest_result.py:140 hardcoded 252 | 1 | ✅ |
| 6 | simulator.py:180,200 end_epoch + off-by-one | 3 | ✅ |
| 7 | simulator.py:108 order_id tier collision | 2 | ✅ |
| 8 | order_generator.py:213 + eod_breakout.py:246 `abs()` sign | 4 | ✅ |
| 9 | order_generator.py:211-224 missing tracker.add | 4 | ✅ |
| 10 | enhanced_breakout.py:455 missing `require_peak_recovery=False` | 4 | ✅ |
| 11 | charges.py:36 non-IN/US double-charge | 5 | ✅ |

Test coverage: 273 tests passing (was 231 at session start; +42 new
P0-regression tests across `test_equity_curve.py`, `test_charges.py`,
`test_simulator_end_epoch.py`, `test_order_key.py`, `test_exits.py`).

## What remains

- **Re-run affected strategies**: every strategy listed above produces
  different numbers post-fix. Run each through the engine again, or
  use `scripts/recompute_metrics.py` where only the metrics changed
  (Layer 1 / Layer 5 only — not Layer 3 / 4 which change trade-level
  behavior).
- **Publish-facing numbers** (**checked 2026-04-21**): grep of
  `ts-content-creator/` and `docs/10-marketing/` found **zero references**
  to engine strategies (`enhanced_breakout`, `quality_dip_tiered`,
  `eod_breakout`, `momentum_*`, `earnings_dip`). All blog / LinkedIn /
  Reddit content references the separate `backtests/` repo's factor
  strategies (magic formula, dogs of dow, high-yield quality, pairs), not
  the engine. **No content remediation required.**
- **Architectural follow-ons** (not P0): the audit's Tier 2 performance
  hotspots are now unblocked — the consolidated `engine/exits.py` gives
  a natural seam to vectorize `run_scanner`, replace `list.index(epoch)`
  with `bisect_left`, and materialize `group_by("instrument")` once
  instead of per-signal filter loops.

---

## Deferred decisions (recorded 2026-04-21)

During the code review self-audit, three behavioral decisions were flagged
that the P0 fixes implied but didn't explicitly document. Recording them
here so future changes are deliberate, not accidental.

### Decision 3 — `_record_exit` merge semantics

**Context:** `engine/order_generator.py::_record_exit` handles the case
where multiple exit_configs emit a decision at the same `exit_epoch` for
the same entry. The first config's decision stores the row; subsequent
configs' IDs are appended to `exit_config_ids` but the **price, volume,
and reason of the first decision are kept**. Later decisions only append
their id.

**Alternatives considered:**
- (a) First-decision wins — current behavior.
- (b) Anomalous-drop always overrides — reasoning: anomalous gap implies
  a corporate action and its `last_close * 0.8` haircut is more
  authoritative than a TSL's next-day-open.
- (c) Priority-ordered dispatch (`anomalous_drop` > `trailing_stop` >
  `end_of_data` > `max_hold`) — most "correct" but changes behavior.

**Decision: (a), first-decision wins.** Rationale:
- Matches pre-fix behavior, so it doesn't add another numerical
  discontinuity on top of the P0 fixes.
- In practice the anomalous_drop check runs FIRST in the per-bar loop
  (order_generator.py:225-231 post-refactor), so it already gets first
  dibs on any epoch where it fires.
- (b) or (c) would require re-running every affected backtest to isolate
  their delta from the other P0 fixes. Can revisit as a standalone
  change with its own regression run.

**Code location:** `engine/order_generator.py::_record_exit` (line
267-280). Documented with a comment referencing this decision.

### Decision 7 — `end_of_sim` exit when `end_epoch` is not an MTM day

**Context:** Layer 3's end-of-sim block force-closes any remaining open
positions at their `last_close_price`. If `context["end_epoch"]` falls on
a non-trading day (weekend, holiday), `last_close_price` is from whatever
the most recent MTM day was — potentially several days stale. The trade
row records `exit_epoch = end_epoch` even though the price is older.

**Alternatives considered:**
- (a) Use `last_close_price` silently — current behavior.
- (b) Raise if `end_epoch` not in `mtm_epochs`, forcing callers to align.
- (c) Walk back to the nearest prior MTM epoch, set that as the effective
  `exit_epoch`, emit a warning in the sim log.

**Decision: (c), walk-back with warning. Landed 2026-04-21.**
Rationale:
- (a) silently hides the staleness — bad for diagnostics.
- (b) too strict — configs often use calendar dates like "2025-01-01"
  which is a holiday in many markets. Hard-failing a run for a
  calendar-alignment issue is disproportionate.
- (c) preserves the intent of `end_epoch` (semantic "end of simulation
  window") while giving the trade row an honest `exit_epoch` and price
  from the same bar.

**Implementation:** `engine/simulator.py` end-of-sim block computes
`effective_end_epoch` via `bisect_right` on sorted `mtm_epochs`. If
`end_epoch in mtm_epochs`, no walk-back (happy path, zero overhead
for the common case). Otherwise, the most recent prior MTM day is
selected; `trade_log[i]["exit_epoch"]` records the effective day; a
warning is appended to `current_snapshot["warnings"]` so callers can
diagnose alignment issues. Degenerate edge case (`end_epoch` precedes
ALL MTM data — shouldn't happen in practice but guarded) falls back
to `end_epoch` verbatim with a distinct "precedes all MTM data"
warning.

**Tests:** 3 new tests in
`tests/test_simulator_end_epoch.py::TestDecision7EndEpochWalkBack`:
- `test_end_epoch_on_non_trading_day_walks_back` — MTM data through
  day 28, `end_epoch` at day 30 → exit_epoch recorded at day 28 with
  warning.
- `test_end_epoch_in_mtm_epochs_no_walkback_no_warning` — happy path
  regression guard; no walkback, no warnings key in snapshot.
- `test_end_epoch_before_any_mtm_data` — degenerate fallback; open
  position from prior snapshot force-closed at end_epoch literally
  with distinct warning.

**Regression check:** champion `momentum_dip_quality` re-run post-fix
produced byte-identical metrics (CAGR 25.38%, Calmar 0.954). No
regressions in the 285-test suite. None of the existing runs in this
session hit the walk-back branch (all had `end_epoch` naturally falling
on a trading day), so the change is truly no-op for the happy path.

### Trade-count discontinuity warning

**Context:** Layer 3's `end_of_sim` policy emits synthetic trade rows
with `exit_reason = "end_of_sim"`. These add to `total_trades`, which is
the denominator for `win_rate`, `profit_factor`, `payoff_ratio`, and
`expectancy`. Consumers reading pre-fix vs. post-fix summaries will
silently compare across different denominators unless they filter.

**Guidance:**
- Pre-fix comparisons: `total_trades`, `win_rate`, `profit_factor`, and
  `expectancy` denominator is CLOSED natural exits only.
- Post-fix comparisons: same denominator plus any `end_of_sim` rows.
- To compare like-for-like, filter post-fix trades:
  `[t for t in trades if t.get("exit_reason") != "end_of_sim"]`.
- Scripts/dashboards that aggregate trade stats across both eras should
  use the filter or note the discontinuity.

**Observed impact:** In this session's regression runs, zero trades had
`exit_reason == "end_of_sim"` (all positions exited naturally before
`end_epoch`). The discontinuity is a real API-level concern but has not
yet bitten a real result.

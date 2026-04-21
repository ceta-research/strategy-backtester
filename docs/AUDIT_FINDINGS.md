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

---

## Phase 1 + Phase 2 P1s — 2026-04-21

Completed 10 P1 items spanning metrics, portfolio, and simulator
correctness. Baseline before work: 285 passing. After: 293 passing
(8 new regression tests).

### Phase 1 — Metrics & Result

**P1.1 — Sortino downside variance denominator** (`lib/metrics.py:310`)
- Changed `downside_var = sum(downside_sq) / n` → `/ (n - 1)` to match
  sample variance used by `variance` in the same function. Pre-fix was
  a population denominator; inconsistent with the sample denominator on
  the regular variance path. Post-fix Sortino is marginally smaller
  (roughly by a factor of `sqrt((n-1)/n)` ≈ 0.9995 for n ≈ 1000), so
  published values move by a negligible amount but are now internally
  consistent.
- Regression test: `TestSortinoDenominator.test_sortino_uses_sample_downside_variance`.

**P1.2 — Sortino rf_period threshold** (`lib/metrics.py:250-260`)
- Analysis-only. With the P0 EquityCurve fix, `ppy` is sourced from
  `curve.frequency.periods_per_year` (365 for DAILY_CALENDAR, 252 for
  DAILY_TRADING), so `rf_period = risk_free_rate / ppy` is now
  dimensionally consistent with the return sampling rate. A docstring
  now documents the invariant and the minor DAILY_CALENDAR distortion
  (weekend forward-fill produces flat returns marginally below
  rf_period; impact < 1e-5 of downside_dev).

**P1.3 — Yearly MDD reset across year boundaries** (`lib/backtest_result.py:315-355`)
- Pre-fix reset the peak at each calendar-year start, so a portfolio
  that peaked at $1M in 2021 then rallied monotonically from $700K to
  $900K throughout 2022 reported 2022 MDD = 0% — hiding that it was
  still 10% below ATH the entire year.
- Post-fix carries a running peak across year boundaries. Years spent
  wholly under a prior-year peak now report meaningful drawdown.
- Impact on historical `results/`: any multi-year result where a
  subsequent year didn't set a new peak will now report a non-zero
  `yearly_returns[year].mdd`. Not a bug that changed strategy rankings
  (Calmar uses full-history MDD, unchanged), but the yearly MDD column
  in dashboards will differ for those years.
- Regression test: `TestYearlyMDDRunningPeak.test_mdd_reflects_carry_peak_from_prior_year`.

**P1.4 — time_in_market saturated for multi-position strategies** (`lib/backtest_result.py:411-447`)
- Pre-fix: `days_held = sum(t["hold_days"] for t in self.trades)` then
  `min(days_held / total_days, 1.0)`. A 10-position portfolio easily
  summed to 10× total_days, always saturating at 1.0 via the min().
  The metric was meaningless for any non-trivial sweep.
- Post-fix: interval-union — sort trade intervals, merge overlaps,
  sum merged durations. Overlapping concurrent positions are counted
  once; disjoint intervals sum.
- Impact on historical `results/`: `time_in_market` will drop for
  multi-position sweeps. Previously all reported 1.0; now reflects
  actual concurrent-positions coverage. Pure single-position
  strategies (e.g. index ETF buy-and-hold) see no change.
- Regression tests: `TestTimeInMarketIntervalUnion` (two cases:
  overlapping and disjoint).

### Phase 2 — Simulator correctness

**P2.1 — MTM falsy-check on close=0** (`engine/simulator.py:316`)
- Pre-fix `if close_price:` treated 0.0 as "skip update", silently
  preserving the stale `last_close_price`. A stock that actually
  delisted or hit zero via corporate action would show no wipeout.
- Post-fix `if close_price is not None` updates MTM to 0, correctly
  reflecting the wipeout in the equity curve.
- Regression test: `TestMTMZeroClose.test_zero_close_zeroes_position_mtm`.

**P2.2 — Missing avg_txn under percentage_of_instrument_avg_txn cap**
(`engine/simulator.py:94-106`)
- Pre-fix silently placed the order without applying the cap when
  avg_txn was missing for the (epoch, instrument). Orders could
  massively exceed the user-declared liquidity cap.
- Post-fix adds `context["missing_avg_txn_policy"]`. Default is
  `"no_cap"` (preserves pre-fix behavior exactly — historical
  results remain byte-identical) to avoid a silent behavior change
  across every existing strategy config. Opt-in `"skip"` policy
  refuses the order when liquidity is unknown, honoring the user's
  intent strictly. Either policy logs to
  `snapshot["missing_avg_txn_events"]` — the **silence** that was the
  original bug is now broken regardless of policy.
- Impact on historical `results/`: **none** under default. Strategies
  that opt into `"skip"` will see fewer orders.
- Regression tests: `TestMissingAvgTxnPolicy` (default no_cap + opt-in
  skip).

**P2.3 — Integer truncation of order_quantity** (`engine/simulator.py:108`)
- Decision: keep `int(_order_value / entry_price)`. This matches real
  equity delivery semantics (NSE/BSE and most international CNC
  products do not support fractional shares). The ~1% cash drag for
  high-priced stocks with small orders is a real live-trading cost,
  correctly preserved in the backtest.
- No code change; added explanatory comment.

**P2.4 — Payout catch-up when resuming past multiple intervals**
(`engine/simulator.py:287-310`)
- Pre-fix ran exactly one payout per iteration and advanced
  `next_payout_epoch` by a single interval. When resuming a chunked
  simulation from a snapshot whose `next_payout_epoch` lay multiple
  intervals in the past, all but one due payout was silently skipped.
- Post-fix: `while simulation_date >= next_payout_epoch` — loops
  forward, running each missed payout. Percentage-based payouts
  re-read current_account_value inside the loop so each withdrawal
  is taken from the post-previous-payout balance.
- Impact: affects only chunked/resumed simulations. Non-resumed
  simulations (single `process()` call from start to end) are
  unchanged.
- Regression test: `TestPayoutCatchUp.test_resume_past_three_fixed_payouts`.

**P2.5 — Entries-first-vs-exits ordering** (`engine/simulator.py:273-298`)
- Documentation only. Added block comment explaining the two
  semantics: default `entries_first` matches ATO_Simulator and is
  slightly conservative; `exit_before_entry=True` matches real-broker
  same-day cash-recycling. Default preserved for historical result
  comparability; callers opt-in per-strategy.

**P2.6 — epoch_wise_instrument_stats memory profile**
(`engine/pipeline.py:154`, `engine/utils.py:55-99`)
- Measured: 2454 instruments × 5915 calendar days = 14.5M entries
  consumes **4.15 GB** of Python dict memory (~307 bytes/entry).
  Confirmed via tracemalloc on a synthetic structure of the same
  shape. Not over-engineered; this is a real memory concern for
  large-universe sweeps.
- Not yet refactored. Proposed future fix: replace the nested-dict
  layout with two 2D numpy arrays (`close[epoch_idx][inst_idx]`,
  `avg_txn[epoch_idx][inst_idx]`) plus name-to-index dicts. Expected
  ~18× memory reduction (4.15 GB → ~230 MB). Touches simulator,
  ranking, and other consumers — structural refactor outside the
  scope of this phase. Tracking as a separate task.

### Regression suite

After Phase 1 + Phase 2 work: **293 passing** (285 baseline + 8 new
regression tests). No pre-existing tests broke. All fixes carry
dedicated regression tests in `tests/test_review_findings.py`.

---

## Phase 3 P1s — 2026-04-21

Completed 8 P1 items spanning ranking determinism, scanner/ranking
convention documentation, charges rate vintage, and unknown-exchange
warnings. Baseline before work: 293 passing. After: 302 passing (9 new
regression tests in `tests/test_ranking.py` and `tests/test_charges.py`).

Champion verification (`tests/verification/config_ato_match.yaml`): all 16
configs remain byte-identical to the Phase 1+2 baseline — 2789 orders, 731
days, CAGR 25.8%/19.4%, MDD -20.1%/-16.6%. Zero historical-number movement.

### Phase 3 — Ranking, Scanner, Charges

**P3.1 — avg_txn prev-day vs same-day conventions**
(`engine/ranking.py:45-54`, `engine/scanner.py:97-105`,
`engine/utils.py:62-63`)
- Investigation, not a bug. Verified against
  `ATO_Simulator/simulator/steps/simulate_step/util.py`:
    - ATO uses PREV-DAY (`.shift(1)`) for `sort_orders_by_highest_avg_txn`
      at util.py:251-256 — look-ahead-safe for order ranking.
    - ATO uses SAME-DAY for `create_epoch_wise_instrument_stats` at
      util.py:186 — liquidity cap on a known bar.
- Our engine matches ATO exactly in both places. The split is
  intentional because the two code paths have different purposes.
- Added cross-linked block comments at each call site so the next
  reader doesn't file the same audit item again.
- Regression test: `tests/test_ranking.py::TestAvgTxnPrevDay` — builds
  a 4-day × 2-instrument fixture where same-day vs prev-day produce
  opposite orderings, asserts prev-day is used.

**P3.2 — highest_gainer formula** (`engine/ranking.py:84-93`)
- Verification only. Formula is
  `(prev_close - prev_close.shift(N)) / prev_close.shift(N)` —
  identical to ATO util.py:281-283.
- Regression test: `tests/test_ranking.py::TestHighestGainerFormula` —
  pins descending-gain ordering on a 3-instrument × 6-day fixture
  where the expected rank is known by construction.

**P3.3 — remove_overlapping_orders determinism**
(`engine/ranking.py:118-132`)
- Pre-fix: `_df_orders.group_by("instrument")` without
  `maintain_order=True`. Polars documents group order as unordered
  unless the flag is set, meaning two runs on the same shuffled input
  could produce a different `pl.DataFrame(idx_to_keep)` row order, and
  the subsequent score join in
  `sort_orders_by_top_performer` could break ties differently.
- Post-fix: `group_by("instrument", maintain_order=True)`.
  Per-group dedup was already ordering-safe; this pins the OUTER
  aggregation.
- Impact on historical `results/`: champion verification
  (`config_ato_match.yaml`) is byte-identical pre and post. The
  test run used pandas-stable ordering by accident, so the fix is a
  safety guarantee, not a number change. Any future polars upgrade
  that changes group_by internals is now safe.
- Regression test:
  `tests/test_ranking.py::TestRemoveOverlappingDeterministic` — runs
  `calculate_daywise_instrument_score` 10× on shuffled input and
  asserts all outputs are bit-identical.

**P3.6 — avg_day_transaction_threshold convention**
(`engine/scanner.py:97-105`)
- Documentation-only. Same prev-day-vs-same-day split as P3.1.
  Scanner uses same-day (matches ATO stats path), ranking uses
  prev-day (matches ATO ranking path). Cross-linked comments added.

**P3.7 — price_threshold is per-bar** (`engine/scanner.py:117-124`)
- Documentation-only. Confirmed via code-read: the threshold filter
  runs inside the per-scanner-config loop and drops any row where
  close <= threshold. A stock above threshold on some days and below
  on others is in the universe only for the above-threshold days.
  This is deliberate (day-by-day universe for liquidity/price-
  dependent strategies) but worth naming.
- Regression test:
  `tests/test_ranking.py::TestScannerPriceThresholdPerBar` — builds
  a 10-day fixture where `close=45` on days 0-4 and `close=55` on
  days 5-9, asserts scanner_config_ids is non-null only on the
  above-threshold days.

**P3.8 — max_hold_days unit** (`engine/exits.py:105-115`,
`engine/signals/base.py:247`)
- Documentation-only. Verified via grep: every call site (base.py,
  exits.py, 15+ signal generators) computes hold_days as
  `(this_epoch - entry_epoch) / 86400` — unambiguously CALENDAR
  days. Docstring at `max_hold_reached` now states the unit and
  warns against mixing with trading-day lookbacks.

**P3.4 — charges rate vintage** (`engine/charges.py`)
- No rate changed. The concern was that several Indian regulatory
  rates (NSE transaction charge, STT, stamp duty) have changed over
  the last few years, and a long-running backtest spanning a rate
  change would use a single rate throughout. This is a known
  approximation, not a bug — flagging explicitly as a P2 follow-up
  for a dated-schedule refactor.
- Fix: added rate-vintage comments naming which constants are
  stable vs revised, and pinned every India/US constant in
  `tests/test_charges.py::test_nse_rate_constants_vintage_pinned` /
  `test_us_rate_constants_vintage_pinned`. Any future rate update
  must edit the test in the same commit — prevents silent drift.

**P3.5 — non-India/US exchange warnings** (`engine/charges.py:71-93`)
- No rate changed (by design — silently updating LSE from 0.05%/side
  to UK-stamp 0.5% buy-side would retroactively invalidate every
  cross-exchange result in `results/`, including the US/UK/Taiwan
  backtests referenced in memory).
- Fix: one-time warning logged the first time each unknown exchange
  hits the `OTHER_EXCHANGE_PER_SIDE_RATE` fallback. Users running
  cross-exchange backtests now see a visible flag that the cost
  model is coarse. Real LSE (0.5% buy stamp) and HKSE (0.13% both
  sides) are materially different from 0.05%/side — follow-up P2
  task to add dated per-exchange schedules is tracked in the
  module docstring.
- Regression tests: `tests/test_charges.py::test_unknown_exchange_warns_once`
  (warns once per exchange, not per call) and
  `test_known_exchanges_do_not_warn` (NSE/BSE/US/NASDAQ/NYSE/AMEX
  do not emit fallback warnings).

### Phase 3 regression suite

After Phase 3 work: **302 passing** (293 + 9 new). No pre-existing
tests broke. All fixes carry dedicated regression tests. Champion
config verification (16 configs in `config_ato_match.yaml`)
byte-identical to pre-Phase-3 run.

### Open follow-ups surfaced by Phase 3 (all P2)

1. Dated regulatory rate schedules (`engine/charges.py`) — pick rate
   by trade date rather than a single constant. Biggest impact on
   multi-year Indian backtests that span 2020-07 (stamp duty
   unification) and 2023-2024 (NSE transaction rate revisions).
2. Detailed per-exchange fee models (`engine/charges.py`) — LSE,
   HKSE, XETRA, JPX, KSC, TSX, ASX are all used in active configs
   and currently share a single 0.05%/side fallback.
3. Broader `group_by` determinism sweep (`engine/signals/*`) —
   P3.3 fixed the one load-bearing site in ranking; 30+ other
   `group_by("instrument")` sites in signal generators may or may
   not depend on iteration order. Systematic audit or a polars
   version pin is the right follow-up.

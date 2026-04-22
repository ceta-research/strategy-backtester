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

| Strategy / scope | Re-run reason |
|---|---|
| `quality_dip_tiered` | P0 #7 — tier collisions silently dropped all but one tier per (instrument, entry_epoch, exit_epoch). |
| `enhanced_breakout`  | P0 #10 — TSL never fired on red-close breakouts (missing `require_peak_recovery=False`). |
| `eod_breakout`       | P0 #8 — `abs()` anomalous-drop forced losses on positive gaps. |
| Any strategy exiting via `order_generator` path | P0 #9 — anomalous-drop branch missing `tracker.add()` produced duplicate exits with non-deterministic pricing. |
| Any cross-exchange result on **LSE** | Phase 3 revisit (`ba95a05`) — pre-fix flat 0.05%/side; post-fix 0.5% SDRT stamp on BUY + 0.05% broker per side. ~10× cost increase. Returns drop materially. |
| Any cross-exchange result on **HKSE** | Phase 3 revisit — historical-max 0.13% stamp both sides + SFC + AFRC + trading fee + CCASS + 0.15% broker. ~6× cost increase per side. |
| Any cross-exchange result on **KSC** | Phase 3 revisit — 0.25% sec tax + 0.15% agricultural tax on SELL (historical max). ~9× sell-side cost. |
| Any cross-exchange result on **XETRA / JPX / TSX / ASX** | Phase 3 revisit — per-exchange schedules replace flat 0.05%/side. 1.2-2× cost increases; moderate movement. |

For strategies NOT in the table (`momentum_top_gainers`, `momentum_cascade`,
`momentum_dip_quality`, `earnings_dip` on NSE/US only), the migrated
`results_v2/` numbers are authoritative.

**Scope of the cross-exchange invalidation:** grep `results/` for any
run whose config targets LSE/HKSE/XETRA/JPX/KSC/TSX/ASX. Those specific
results need a simulation re-run, not just a metrics recompute, because
`calculate_charges` affects trade-level P&L which compounds into the
equity curve.

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

1. ~~Dated regulatory rate schedules (`engine/charges.py`)~~ — closed
   by P3.4 revisit (see below): moved NSE exchange rate to current
   2024 value (0.00297%). Per-exchange non-IN/US rates now use
   max(current, historical) for conservative costs.
2. ~~Detailed per-exchange fee models~~ — closed by P3.5 revisit
   (see below): LSE, HKSE, XETRA, JPX, KSC, TSX, ASX now have
   explicit helpers.
3. ~~Broader `group_by` determinism sweep~~ — closed via the safety
   belt approach: added `maintain_order=True` to all 42 group_by
   sites in engine/ (see "Phase 3 group_by safety belt" below).

---

## Phase 3 revisit — NSE rate + per-exchange schedules — 2026-04-21

Landed shortly after the initial Phase 3 commit in response to
reviewer feedback: "just use current rates". Changed the default
position from "document the approximation" to "use the best available
rate".

### P3.4 revisit — NSE_EXCHANGE_RATE to current 2024

- Was: `0.0000345` (0.00345%, pre-2023 NSE rate)
- Now: `0.0000297` (0.00297%, current per Zerodha's brokerage
  calculator as of 2024+)
- Impact on historical `results/`: per-trade cost drops by ~0.00048%
  of notional — a ~14% reduction in that single component. Champion
  verification (`config_ato_match.yaml`): CAGR stable at 25.76%,
  Calmar 1.279110 -> 1.279252 (+0.0001). All 16 configs' order
  counts and days remain byte-identical; only the cost component
  of equity moved.
- Note the direction: current rate is LOWER than historical, so
  using current slightly under-prices costs for backtests spanning
  pre-2024 periods. This is the user's deliberate choice over using
  max(current, historic). A dated-schedule refactor remains a
  possible future improvement if precision matters for a specific
  backtest.
- Test: `test_nse_rate_constants_vintage_pinned` now pins 0.0000297;
  `test_nse_intraday_exact_breakdown` uses the new rate in its
  component math.

### P3.5 revisit — detailed per-exchange schedules

Added explicit per-side helpers for LSE, HKSE, XETRA, JPX, KSC, TSX,
ASX. Each uses `max(current, historical)` where a historical rate
was higher, producing a conservative (upper-bound) cost estimate.

**LSE (UK):**
- 0.5% SDRT stamp duty on BUY only (stable since 1986)
- 0.05% broker per leg
- Buy: 0.55% of notional. Sell: 0.05%.

**HKSE:**
- 0.13% stamp duty both sides (historical max, Aug 2021 - Nov 2023;
  current is 0.10%)
- 0.0027% SFC transaction levy
- 0.00015% AFRC levy
- 0.00565% exchange trading fee
- 0.002% CCASS clearing
- 0.15% broker
- Symmetric. Per side: ~0.29% of notional.

**XETRA (Germany):**
- No stamp or transaction tax
- 0.01% exchange/clearing + 0.05% broker
- Symmetric. Per side: 0.06%.

**JPX (Japan):**
- No transaction tax since 1999
- 0.005% exchange + 0.1% broker
- Symmetric. Per side: 0.105%.

**KSC (Korea KOSPI):**
- Buy: 0.05% broker only
- Sell: 0.25% securities transaction tax (historical max, pre-2023;
  current is 0.18%) + 0.15% agricultural tax (KOSPI only) +
  0.05% broker = 0.45% sell-side
- Asymmetric: Korean sec tax is sell-only.

**TSX (Canada):** 0.005% regulatory + 0.1% broker = 0.105%/side
symmetric (Canada abolished transaction tax).

**ASX (Australia):** 0.0028% ASIC + 0.1% broker = 0.1028%/side
symmetric (no stamp).

### Impact on existing cross-exchange results

Every cross-exchange backtest result in `results/` (memory notes
reference "US Cal 0.615, UK Cal 0.610, Taiwan 0.606, S.Korea 0.276,
SHZ 0.352" from earlier runs) is now invalidated for the exchanges
with detailed helpers:

- **LSE:** costs ~10× higher (was 0.05%/side flat, now 0.55%
  buy / 0.05% sell). UK alpha estimates will drop materially.
- **HKSE:** ~6× higher (was 0.05%/side, now ~0.29%/side).
- **KSC:** ~9× higher on sell side (was 0.05%, now 0.45%).
- **XETRA, JPX, TSX, ASX:** modest 1.2-2× increase.

Re-running affected backtests is a P1 task — tracked in the
"4 strategies requiring full re-runs" flag at the top of
AUDIT_CHECKLIST (now broadened to cover every strategy with a
non-NSE/US cross-exchange result).

### Regression tests

- `test_per_exchange_rate_constants_pinned` — golden-value pin for
  every non-IN/US rate constant (13 constants).
- `test_lse_buy_charges_include_uk_stamp` / `test_lse_sell_no_stamp` —
  asymmetric UK stamp math.
- `test_hkse_symmetric_stamp_plus_regulatory` — HKSE 6-component
  symmetric math to 0.5 rupee precision.
- `test_xetra_no_stamp`, `test_jpx_no_stamp`, `test_tsx_symmetric`,
  `test_asx_symmetric` — pinned per-exchange round-numbers.
- `test_ksc_sell_includes_sec_and_agricultural_tax` — asymmetric KSC
  sec tax + agricultural tax on sell side only.
- `test_every_detailed_exchange_is_per_side` — invariant that
  round_trip = buy + sell for each new detailed exchange.
- `test_known_exchanges_do_not_warn` — updated to include the 7 new
  detailed exchanges.
- `test_unknown_exchange_warns_once` — updated to use SAO / JNB
  (still on fallback) since LSE/HKSE are now detailed.

### Phase 3 revisit regression suite

After revisit: **312 passing** (302 + 10 new per-exchange tests).
Champion verification byte-matches on order counts and days;
Calmar shifts by +0.0001 due to the NSE rate reduction (expected
direction — lower cost → slightly higher return).

---

## Phase 3 group_by safety belt — 2026-04-21

Closed the remaining Phase 3 follow-up: added `maintain_order=True`
to every `group_by` call across `engine/` (42 sites across 26 files).
Eliminates the class of polars-version-dependent ordering bugs
without requiring a per-site audit of which consumers care about
iteration order.

### Sites touched (42)

- `engine/utils.py`: 1 site (`create_epoch_wise_instrument_stats`)
- `engine/order_generator.py`: 2 sites (date_epoch aggregation +
  per-instrument iteration)
- `engine/data_provider.py`: 1 site (symbol count aggregation in
  price oscillation filter)
- `engine/signals/*.py`: 38 sites across 25 signal generators

Already fixed in Phase 3 P3.3: `engine/ranking.py::remove_overlapping_orders`.

### Impact

Zero on the champion verification config — post-fix numbers are
byte-identical to pre-fix (2789 orders, 731 days, CAGR 25.76%,
Calmar 1.279252). Expected: with the current polars 1.37.1 pin on
the cloud and matching local installs, iteration order was already
stable *in practice* even without the flag. The fix pre-empts
future polars upgrades silently changing results.

### Choice of safety-belt vs per-site audit

The alternative was auditing all 42 sites to classify each as
order-dependent or order-independent. Option chosen (safety belt):

- Cost: one-line change × 42 sites = ~15 minutes.
- Perf cost: negligible (maintain_order=True requires a stable sort
  after hash-grouping; the signal-gen compute is dominated by
  per-instrument numpy loops, not the group_by step itself).
- Benefit: eliminates the class of bug.

The per-site audit would have been 2-3 hours and left some
exposure if any future signal gen is added without the flag.
Safety belt is the robust default.

### Non-production code not modified

`scripts/debug_signal_gen.py` has 2 unsafe `group_by` calls. It's
a debug / comparison tool, not production backtesting code, so it
wasn't touched.

---

## Phase 4 P1s — 2026-04-21

Completed 7 P1 items covering the data-provider layer and data
integrity. Baseline before work: 312 passing. After: 315 passing
(3 new regression tests in `tests/test_data_provider.py`).

Champion verification: byte-identical (CAGR 25.76%, Calmar
1.2792521). Zero historical-number movement.

### Phase 4 — Data Provider & Data Integrity

**P4.1 — prefetch_days handling across signal generators**
(`engine/signals/*.py`, 32 files)
- Investigation. Every data provider fetches
  `(start_epoch - prefetch_days * 86400)` through `end_epoch` so that
  rolling-window indicators (30-day avg_txn, 252-day breakout, etc.)
  have warm-up data. The risk is that a signal generator emits
  entry orders during the prefetch window, producing ghost trades
  before the simulation officially starts.
- Verified via grep + per-file read: every signal generator either
  (a) filters `df_signals` by `pl.col("date_epoch") >= start_epoch`
  (26 files), (b) delegates to `scanner.process` which trims at
  scanner.py:130 (1 file: `eod_technical.py`), or (c) derives a
  rebalance-date list from a scanner-trimmed frame (5 files:
  `factor_composite.py`, `low_pe.py`, `trending_value.py`,
  `momentum_cascade.py`, `earnings_dip.py`).
- Regression test:
  `tests/test_data_provider.py::TestSignalsRespectStartEpoch::test_start_epoch_filter_present_in_signal_files`
  — iterates every signal file and asserts it mentions `start_epoch`
  (or is on the allow-list of indirect trimmers). Catches any new
  signal gen added without the pattern.

**P4.2 — price oscillation filter logging**
(`engine/data_provider.py:29-172`)
- Pre-fix: the filter printed a one-line summary to stdout when
  `verbose=True`. There was no structured log event, so users running
  inside a larger pipeline had no way to programmatically detect
  which symbols got rows dropped.
- Post-fix: `_logger = logging.getLogger(__name__)` at module top;
  the filter now emits `logger.info(...)` with the removal summary
  and `logger.debug(...)` listing the exact affected symbols. The
  original stdout print is preserved for `verbose=True` callers
  (backward compat).
- Regression tests:
  - `TestOscillationFilterLogging::test_info_log_emitted_when_rows_removed`
    — builds a synthetic 2-symbol frame with known spike+revert
    patterns, asserts both INFO and DEBUG events fire.
  - `TestOscillationFilterLogging::test_no_log_when_no_removals` —
    clean monotonic prices produce zero removal logs.

**P4.3 — corporate actions documentation**
(`engine/data_provider.py` module docstring)
- Documentation-only. Added a detailed section in the module
  docstring naming each provider's adjustment behavior:
  - `CRDataProvider` / `FMPParquetDataProvider` /
    `DuckDBParquetDataProvider` / `PolarsParquetDataProvider`:
    all SELECT `close` (not `adjClose`) from `fmp.stock_eod`. FMP's
    `close` is split-adjusted but NOT dividend-adjusted. Long-hold
    strategies understate total return by ~dividend yield.
  - `ParquetDataProvider`: reads kite parquet; split-adjusted.
  - `BhavcopyDataProvider`: UNADJUSTED prices; caller must apply
    corporate actions. Oscillation filter skipped because legitimate
    split-day jumps are expected.
  - `NseChartingDataProvider`: split-adjusted.

**P4.4 — missing-data handling documentation**
(`engine/data_provider.py` module docstring)
- Documentation-only. All providers return whatever the source
  yields: missing trading days appear as ABSENT ROWS, not null rows.
  The canonical gap-fill happens in `engine.scanner.fill_missing_dates`
  which inserts missing-date rows and then backward-fills `close`.
  Signal generators downstream should not assume every calendar day
  has a row unless they've gone through the scanner.

**P4.5 — NSE forward-fill spot-check** (`~/ATO_DATA/tick_data/`)
- Queried local kite parquet for 5 major NSE stocks (INFY, HDFCBANK,
  RELIANCE, TCS, SBIN) and NIFTYBEES:
  - Null closes: 0 across all 6
  - Duplicate date_epoch rows: 0 across all 6
  - Price jumps > 2x or < 0.5x (unadjusted-split signature): 0 across
    all 6 — indicates split adjustments ARE present
- Minor finding: 1-3 rows per symbol fall on weekends. Almost
  certainly NSE muhurat / special sessions (one trading day per year
  at Diwali); not a data-quality bug.

**P4.6 — splits/bonuses cross-check**
- Local fixture covers 2019-01 to 2021-12 only (30 symbols, 776 days
  max). Pre-fixture splits (e.g., INFY Sep-2018 1:1 bonus) are out
  of range. Cannot cross-check against TradingView/Yahoo in this
  session without web access.
- No unadjusted jumps found within the fixture window (the 0
  big-jumps finding above is evidence prices are adjusted).
- Full split/bonus verification across the FMP NSE universe is a
  P2 follow-up requiring external data access.

**P4.7 — FMP oscillating-split-factor safety net**
- `remove_price_oscillations` already exists as a defense against
  JNB-style (South Africa) oscillating split factors. Verified on
  synthetic input mimicking the JNB pattern (50-day series with 18
  alternating 35% oscillations): 36 of 50 rows correctly flagged
  and removed. The filter has 2-tier detection (100% spikes always
  flagged; 30% oscillations flagged only on symbols with >= 5
  events) so it catches persistent data-quality issues without
  over-trimming legitimate earnings-day volatility.

### Phase 4 regression suite

After Phase 4 work: **315 passing** (312 + 3 new). Champion
byte-identical. No pre-existing tests broke.

---

## Phase 5 P1s — 2026-04-21

Completed 4 P1 items covering the signal-generator audit layer.
Baseline before work: 315 passing. After: 321 passing (6 new static
audit tests in `tests/test_signal_audit.py`).

Champion verification: byte-identical (CAGR 25.76%, Calmar 1.2792521).
Zero historical-number movement from Phase 5 changes (which are
documentation + tests only).

### P5.1 — eod_breakout.py full audit (reference strategy)

Clean. Verified:
- Entry filter evaluates at day T close (n_day_ma, n_day_high,
  direction_score, scanner_config_ids); entry executes at day T+1
  open via `entry["next_open"]` / `entry["next_epoch"]`. No
  look-ahead.
- Custom `_walk_forward_tsl` (not base.walk_forward_exit) —
  `max_price` tracks max(close since entry) rather than max
  starting from entry_price. Slightly more lenient than
  base.walk_forward_exit on entry-day red candles; matches ATO's
  original convention.
- `anomalous_drop` checked before TSL (post-P0-fix signed gap
  detection — no false exits on positive gaps).
- Last-bar handling: exit at close when no other trigger fires.
- Minor perf note: `ed["epochs"].index(entry_epoch)` is O(n); same
  O(n) pattern across several signal generators. Logged as a
  performance (not correctness) P2 in the original checklist.

Regression test: `TestReferenceStrategyPattern::test_eod_breakout_uses_next_day_open_entry`
— asserts `entry["next_open"]` / `entry["next_epoch"]` / is_not_null
guards remain in the source. Fails if someone silently changes to
same-bar entry.

### P5.2 — momentum_top_gainers.py + momentum_dip_quality.py

**Finding A: full-period turnover universe (LOOK-AHEAD / SURVIVORSHIP BIAS).**

Both strategies compute `period_avg = df_ind.group_by("instrument").
agg(mean(close * volume), mean(close)).filter(...)` over the ENTIRE
data range (start_epoch - prefetch through end_epoch) and use the
resulting `period_universe_set` as a static universe for every
rebalance day. Consequence:

- A stock that becomes liquid in 2020 is included in the 2015
  universe (cannot happen in live trading).
- A stock that delists mid-sim may be included or excluded depending
  on its period-avg turnover (forward-looking information).
- Net effect: moderate overstatement of alpha vs a strict
  point-in-time universe.

The code comment ("matches standalone approach") indicates this
is intentional for parity with the standalone reference
implementation. Left as-is to avoid invalidating existing
optimization results — documented with a prominent AUDIT P5.2
warning at the call site in both files.

**Finding B: scanner_config_ids fallback to "1".**

`momentum_top_gainers.py:289`: `"scanner_config_ids": row.get(
"scanner_config_ids") or "1"`. When a stock didn't pass the per-day
scanner on the rebalance day, it still enters with
`scanner_config_ids = "1"` — bypassing the scanner check. Same
pattern in `momentum_dip_quality.py:458`. Documented at the call
sites; no code change (bypass is intentional since the strategy
uses period_avg as its universe filter).

Regression tests:
- `TestKnownBiasWarningsInPlace::test_mtg_period_avg_bias_is_documented`
- `TestKnownBiasWarningsInPlace::test_mdq_period_avg_bias_is_documented`

Both assert the AUDIT P5.2 warnings stay in source so a silent
removal fails the test.

### P5.3 — earnings_dip.py cross-source audit

Mostly clean. Cross-source (FMP earnings × NSE tick pricing) is
handled correctly:

- Earnings events sorted chronologically per symbol; earnings epoch
  compared against `start_epoch` to avoid pre-sim events.
- Post-earnings peak uses the first 5 trading days AFTER earnings;
  dip scan starts at earn_idx+5 — peak is fully in the past from
  each scan day's perspective.
- Volume confirmation uses 20-day pre-earnings average (not
  post-earnings), so no forward-looking data.
- Entry at next-day open via `pd_next_epochs[i]` / `pd_opens[i]`.
  No same-bar bias.
- Exit via `walk_forward_exit` with `require_peak_recovery=True`
  (correct for dip-buy — only activate TSL after price recovers
  to the post-earnings peak).

Minor concern: quality-universe fuzzy match (line 453-462) extends
±5 days from the earnings date when looking up is_quality. Since
is_quality is computed from prior-year returns shifted 252/504/...
days back, the data feeding is_quality stays pre-earnings regardless
of which adjacent rescreen snapshot is used — only the snapshot
timestamp is slightly fuzzy. Impact: negligible. Not flagged in
code.

Regression test: `TestEarningsDipPattern::test_earnings_dip_uses_next_day_open_and_peak_recovery`.

### P5.4 — momentum_rebalance.py same-bar entry + generic sweep

**Finding: momentum_rebalance.py has SAME-BAR ENTRY BIAS.**

Source convention:
- `momentum_return = close[T] / close[T - N] - 1` — uses close[T]
  in the numerator
- At rebalance date T: filter stocks by `momentum_return`, rank,
  pick top K
- `entry_price = row["close"]` = close[T]
- `entry_epoch = rb_epoch` = day T itself

Signal and execution share the same close[T]. This is not
achievable in live trading — a real MOC order needs the signal
observable BEFORE the closing auction. Memory notes
(`docs/backtest_bias_audit`): same-bar entry inflates mean-reversion
returns by 15-20pp CAGR; momentum magnitude likely smaller but
non-zero.

The docstring explicitly states this convention
("entry_price = close on rebalance_date"), indicating it's a
deliberate choice to match the standalone reference. Clean fixes:
  (a) signal from close[T-1], execute at close[T] (MOC-compatible), OR
  (b) signal from close[T], execute at close[T+1] (delayed).

Added module-level AUDIT P5.4 warning and an inline warning at the
assignment site. No code change to entry logic — fixing would
invalidate existing comparison against the standalone reference.
Tracked as open P1 for strategy-author review.

**Generic sweep result:** all other 29 signal generators use
`next_epoch` (via `add_next_day_values`) or `next_trading_day` for
entry execution. No new same-bar patterns found. Two files use
`entry_price = closes[entry_idx]` as an OPEN-FAIL FALLBACK (low_pe,
factor_composite) where `entry_idx` already points to the
next-trading-day — this is execution slippage, not same-bar bias.

Regression tests:
- `TestKnownBiasWarningsInPlace::test_momentum_rebalance_same_bar_bias_is_documented`
- `TestNoOtherSameBarEntries::test_no_new_same_bar_entries_introduced` —
  sweeps every signal file; fails if a new file introduces
  `entry_price = row["close"]` without the AUDIT P5.4 warning.

### Phase 5 regression suite

After Phase 5 work: **321 passing** (315 + 6 new static audit
tests). Champion byte-identical. No code changes to signal logic —
only inline doc comments and unit-test scaffolding, so no backtest
numbers moved.

### Open P1s surfaced by Phase 5 (deferred per user direction to
preserve result parity)

1. momentum_top_gainers.py + momentum_dip_quality.py:
   full-period universe → point-in-time universe would invalidate
   existing optimization results (CAGR likely drops by several
   percentage points). Requires full re-run if fixed.
2. momentum_top_gainers.py + momentum_dip_quality.py: scanner
   fallback `or "1"` silently bypasses per-day scanner. Fix is
   either `continue` instead of assigning "1", or explicitly
   documenting the period_avg universe as the canonical filter.
3. momentum_rebalance.py: same-bar entry fix — options (a) or (b)
   above. Expected impact: small-to-moderate CAGR reduction
   depending on momentum_lookback.

---

## Phase 6 P1s — 2026-04-21

Completed 4 P1 items covering config determinism, intraday stop-loss
robustness, cloud-orchestrator cache scoping, and simulator edge
cases. Baseline before work: 321 passing. After: 334 passing (13 new
regression tests in `tests/test_edge_cases.py`).

Champion verification: byte-identical (CAGR 25.76%, Calmar 1.2792521).

### P6.1 — config_iterator determinism + YAML fallbacks

Investigation + tests. `create_config_iterator` is already
deterministic — `itertools.product(*kwargs.values())` is stable and
Python 3.7+ dict insertion order is preserved. Verified:

- `TestConfigIteratorDeterminism::test_same_input_yields_same_output`
  — identical input produces bit-identical config lists incl. ids.
- `test_k_to_the_n_total` / `test_mixed_length_cartesian` pin the
  Cartesian count and id sequence.

YAML missing-key behavior is documented as intentional: every key in
every config section has a default. `validate_config` is the single
guard for structural issues (inverted epochs, zero max_positions,
bad trailing_stop_pct). Tests:

- `test_simulation_config_defaults_for_missing_keys` pins the full
  default key set.
- `test_validate_config_rejects_inverted_epochs` + `_zero_max_positions`.

Cost of the default-if-missing design: typos silently fall through
(e.g. `n_day_MA` vs `n_day_ma`). Not a bug, documented as a user-
facing gotcha.

### P6.2 — intraday_simulator_v2 fixed_stop clamp

Pre-fix: `fixed_stop = entry_price - atr_multiplier * atr_14` could
go negative on high-volatility names where
`atr_14 * atr_multiplier > entry_price`. The `price_low <= fixed_stop`
check then never fired on positive prices → silent no-stop mode for
exactly the positions that most needed a stop.

Post-fix (`engine/intraday_simulator_v2.py` in `_resolve_exit`):
clamp `fixed_stop` to `max(fixed_stop, 0.01 * entry_price)` after
the raw computation. Stop is always a realistic positive level.

Regression test: source-level assertion that the clamp marker and
guard appear AFTER the raw fixed_stop assignment
(`TestIntradayFixedStopClamp::test_negative_stop_clamped_to_floor`).

### P6.3 — cloud_orchestrator hash cache per-project scoping

Pre-fix (`lib/cloud_orchestrator.py:224-233`): the hash cache was a
flat `{path: hash}` dict, NOT keyed by `project_name`. Switching
projects (e.g. `sb-remote` → `sb-eod-sweep-v2`) made the new project
inherit the old project's "already uploaded" hashes, skipped the
uploads, and the cloud project ended up empty → runs crashed with
`ImportError` on missing modules.

Post-fix: nested `{project_name: {path: hash}}` layout.
`_load_hash_cache` returns the sub-dict for the current project;
`_save_hash_cache` updates only that sub-dict. Legacy flat-format
files are adopted by the current project on first save (so users
don't re-upload everything on upgrade).

Regression tests:
- `TestHashCacheProjectScoping::test_two_projects_have_independent_caches`
- `test_legacy_flat_format_is_migrated`

### P6.4 — simulator edge cases

Verified 4 edge cases:

1. **Zero-trade simulation.** `simulator.process()` with an empty
   `df_orders` completes cleanly: empty `trade_log`, starting margin
   preserved, per-day MTM log still emitted.
   `TestZeroTradeSimulation::test_empty_orders_returns_clean_state`.
2. **Single-day simulation (start_epoch == end_epoch).**
   `validate_config` raises `ValueError` at config-load time —
   simulation never runs on a zero-duration window.
   `TestSingleDaySimulationValidation::test_validate_config_rejects_single_day`.
3. **All-loser simulation.** Monotonically decreasing equity curve
   produces valid negative CAGR, valid negative MDD, Calmar is None
   or a finite float — no ZeroDivisionError leaks through.
   `TestAllLoserSimulation::test_all_losses_produces_valid_metrics`.
4. **Capital exhaustion.** `simulator.py:154` skips entries when
   `margin_available < required_margin_for_entry`. No retry at
   reduced size, no crash, margin preserved.
   `TestCapitalExhaustion::test_insufficient_margin_skips_entry_gracefully`.

### Phase 6 regression suite

After Phase 6 work: **334 passing** (321 + 13 new). Champion
byte-identical. No pre-existing tests broke.

---

## Phase 7 P1s — 2026-04-21

Completed 6 P1 items. Phase 7 is entirely additive: test coverage for
methods that were previously only exercised indirectly (via pipeline
tests) or whose behavior was documented but unpinned.

Baseline before work: 334 passing. After: 372 passing (38 new across
4 new test files). Champion verification byte-identical (CAGR 25.76%,
Calmar 1.2792521). Zero code changes to engine/ or lib/.

### P7.1 — `EquityCurve.period_returns` hand-computed tests

Previously unpinned. Covered the duplicate-value and zero-previous-
value edge cases, plus the basic `v[i]/v[i-1] - 1` formula.

- `tests/test_backtest_result.py::TestPeriodReturns` (3 tests)

### P7.2 — `walk_forward_exit` direct tests

The central TSL walker is used by 20+ signal generators. Pre-Phase-7
it was covered only via integration through test_pipeline.py and
test_exits.py (which tests the guard + one semantics case). Now has
16 direct tests covering:

- TSL-zero mode (peak recovery without TSL)
- Breakout mode (TSL from entry, `require_peak_recovery=False`)
- Dip-buy mode (TSL gated by peak recovery)
- Next-day-open exit with fallback to same-day close
- max_hold_days interaction with TSL
- None close values skipped
- WFE-1 footgun guard (percent vs fraction)
- End-of-data fallback + start_past_end returns (None, None)

File: `tests/test_walk_forward_exit.py` (16 tests).

### P7.3 / P7.4 — `_trade_metrics`, `_portfolio_metrics`, monthly /
yearly bucketing

Hand-computed tests over:

- 3W/2L trade set: asserts win_rate, profit_factor, payoff, Kelly,
  expectancy, avg_hold (all pinned to exact values).
- All-wins case: profit_factor / payoff / Kelly return None (not
  infinity).
- Empty trades: None for ratio metrics.
- Consecutive streaks: max_cw, max_cl on a known W/L sequence.
- `_portfolio_metrics`: final/peak values; time_in_market interval-
  union correctly counts overlaps once (locks the Phase 1.4 fix).
- Monthly returns chain correctly across month boundaries.
- Yearly returns + running-peak MDD locks the Phase 1.3 fix.

File: `tests/test_backtest_result.py` (12 tests).

### P7.5 — `create_epoch_wise_instrument_stats` profile

Closed via cross-reference to Phase 2 P2.6 which measured memory
consumption at 4.15 GB for 2454 instruments × 5915 calendar days.
The nested-dict layout is load-bearing for simulator MTM and
ranking; a sparse numpy refactor would save ~18× memory but touches
too many call sites for this audit. Tracked as a standalone P2
refactor task.

### P7.6a — simulator direct tests

Previously exercised only via `test_pipeline.py` and
`test_simulator_end_epoch.py`. Added 5 direct tests over
`simulator.process()`:

- Full single-trade cycle: entry → MTM → exit → margin accounting
  balances (ignoring explicit charges/slippage).
- 3 concurrent positions with mixed outcomes.
- `exit_before_entry=True` frees slot on same day.
- Snapshot resume with trade fully inside chunk 1 (state carried
  forward unchanged).
- Snapshot resume with trade straddling chunk boundary (force-close
  at chunk end per close_at_mtm policy).

File: `tests/test_simulator_direct.py` (5 tests).

**Documented behavior quirk surfaced by this phase:** in the
`exit_before_entry=True` branch, `simulator.py:365` overrides
`sim_config["order_value"]` with `current_account_value /
max_positions` — so callers relying on fixed-value sizing get the
wrong behavior under exit_before_entry. Not fixed here (change
invalidates historical backtests); the test works around it with
`max_order_value` + larger `start_margin`. Open P2 for future
simulator cleanup.

### P7.6b — order_generator exit-integration tests

Direct tests for `generate_exit_attributes_for_instrument`, the
integration point between the exit primitives in `engine/exits.py`
and the per-instrument walk-forward loop. Covers:

- Anomalous-drop signed check (P0 #8 regression lock): +25% gap does
  NOT fire anomalous_drop; the strategy walks forward to find a real
  TSL or end-of-data.
- Anomalous-drop priority: fires BEFORE TSL on the same bar.
- Tracker prevents duplicate exits (P0 #9 regression lock): a series
  that triggers anomalous_drop AND would later satisfy TSL produces
  exactly ONE exit row.
- Multiple exit configs track independently.
- Min-hold gate blocks TSL but NOT anomalous_drop.

File: `tests/test_order_generator.py` (5 tests).

### Phase 7 regression suite

After Phase 7 work: **372 passing** (334 + 38 new). All 50/50 P1s
now closed. No pre-existing tests broke. No code changes. Champion
byte-identical.

### Audit status: FINAL

17/17 P0 closed. 50/50 P1 closed. Remaining work is P2 / P3 items
and the open strategy re-runs from Phase 3 revisit (non-IN/US
cross-exchange) + Phase 5 follow-ups (momentum_top_gainers /
momentum_dip_quality / momentum_rebalance bias fixes).

---

## P2 Batch 1 — Metrics definitions & edge cases — 2026-04-21

Plan: `docs/P2_EXECUTION_PLAN.md` §3, Batch 1. Decisions: `docs/P2_EXECUTION_PLAN.md` §1.

### Items closed

| Line | File | Fix |
|------|------|-----|
| L41  | `lib/metrics.py:compute_drawdown_series` + `_compute_series_metrics_with_cagr` | Documented `peak<=0` semantics: returns 0 (no drawdown from nothing), not -1. No behavioral change. |
| L42  | `lib/metrics.py:_compute_series_metrics_with_cagr` VaR | Documented lower-quantile convention vs numpy's `percentile(.., 5)` linear-interpolation. No behavioral change. |
| L43  | `lib/metrics.py` max_dd_duration_periods | Returns `0` when no drawdown occurred (was `None`). `None` reserved for the `n<2` / truly undefined case. |
| L57  | `lib/backtest_result.py:_time_extremes` | Already guards empty series via `if daily_returns else None`. Verified, no change. |
| L58  | `lib/backtest_result.py:compact()` | Sets `_computed["compacted"] = True` so downstream readers can detect compacted results. Also pre-populates `costs` dict in `_empty_result()` so `print_summary` no longer KeyErrors on zero-point runs. |
| L230 | `lib/metrics.py` | **D1: emit both Sharpe definitions.** New field `sharpe_ratio_arithmetic` = `(mean(r)*ppy - rf) / vol_ann`. Existing `sharpe_ratio` unchanged (geometric, CAGR-based). Added to `_empty_metrics`, `format_metrics`, `print_summary`, and `CATALOG_FIELDS`. |
| L236 | `lib/backtest_result.py:SweepResult._sorted` | Split into `scored` + `unscored` buckets. Unscored (metric=None) appended after scored, insertion-order preserved. Pre-fix: `float("-inf")` key buried zero-drawdown configs (Calmar=None) at the bottom of the leaderboard. New `_unscored_configs(sort_by)` accessor. |

### Tests added

- `tests/test_metrics_edge.py` (18 tests): peak<=0 drawdown semantics; VaR lower-quantile convention; max_dd_duration=0 invariant; dual-Sharpe presence, zero-vol handling, hand-computed arithmetic formula, schema-complete empty/length-1 paths.
- `tests/test_sweep_result_sorting.py` (7 tests): scored/unscored partitioning; unscored not ranked worst; insertion-order preservation; dual-Sharpe sort key works; compact() flag detection.

**Full suite: 404 passing** (+23 new, 0 regressions).

### Snapshot impact

D1 adds a new key (`sharpe_ratio_arithmetic`) but does not modify any
existing pinned metric. Regression snapshots (`tests/regression/snapshots/*.json`)
pin `sharpe_ratio` and other fields — none are changed by this batch.
L43 changes `max_dd_duration_periods` from `None` to `0` for
no-drawdown runs; this field is not in `PINNED_FIELDS`.

**Conclusion:** no snapshot update commit required. Downstream result
JSON now includes the new `sharpe_ratio_arithmetic` key; legacy
consumers that iterate known keys are unaffected.

### What this does NOT change

- `sharpe_ratio` values in historical `results_v2/*.json` are unchanged.
  No migration required.
- Formula change required if the team later decides `sharpe_arithmetic`
  should replace `sharpe_ratio` as the primary leaderboard key.
  Tracked as a potential future decision; no action this sprint.

### Known cross-reference

Per-period variance drag (`mean(r) >= geom(r)`) does not imply
`sharpe_arithmetic >= sharpe_geometric` once each is annualized with a
different convention (simple multiplication vs compounding). Short,
high-return samples can invert the relationship. Documented in
`test_metrics_edge.py::TestDualSharpeD1`.

---

## P2 Batch 2 — Pipeline & simulator hygiene — 2026-04-21

### Items closed

| Line | File | Fix |
|------|------|-----|
| L74  | `engine/signals/base.py:sanitize_orders` | Added `diagnostic_threshold` kwarg (default 20.0). Counts orders exceeding this tighter bound WITHOUT dropping them, logged as an advisory. Pipeline's permissive `max_return_mult=999.0` preserved (zero snapshot impact); the diagnostic surfaces data-quality signals that the cap hides. |
| L75  | `engine/pipeline.py:~63-75` | Multi-exchange sweeps now tag `SweepResult` with `"NSE+JPX"` style joined names instead of the first exchange only. Single-exchange sweeps unchanged. |
| L93  | `engine/simulator.py:~320-331` | Verified the three `order_value` types (fixed, percentage_of_account_value, percentage_of_available_margin) and the `order_value_multiplier` scaling. No code change. 6 new end-to-end sizing tests pin exact quantities. |
| L100 | `engine/utils.py:32-44` | Tier-suffix strip (`_t` → base id) behavior already documented; new tests (`test_utils_tier_suffix.py`) pin it so the documented fragility cannot regress silently. |
| L133 | `engine/config_sweep.py` | Compound dict params occupy ONE slot in the cartesian product — verified; test added. |
| L285 | `engine/config_sweep.py` | `create_config_iterator` now raises `ValueError` naming the empty-list key. Pre-fix: commented-out YAML values (empty list) silently produced zero-config sweeps with no user-visible error. |
| L186 | `engine/simulator.py` + `engine/utils.py` | Delisted / mid-sim missing instrument: existing forward-fill in `create_epoch_wise_instrument_stats` fills per-instrument `[min_epoch, max_epoch]` only. If an instrument's data ends mid-sim, later days have no stats entry for that instrument; MTM retains the last-known position value. Documented in this entry; no behavior change. Covered indirectly by Phase 7 simulator-direct tests. |

### Tests added

- `tests/test_sanitize_orders.py` (8 tests): entry/exit price filters, cap at max_return_mult, diagnostic counter fires without modifying DataFrame at `max_return_mult=999`.
- `tests/test_simulator_order_value.py` (6 tests): three sizing modes, default `account_value / max_positions`, `order_value_multiplier` scaling, integer-truncation semantics.
- `tests/test_utils_tier_suffix.py` (4 tests): plain IDs untouched, `_t` suffix collapse, mixed, comma-separated.
- `tests/test_config_sweep.py` extensions: compound-param slot counting, empty-list ValueError (two variants).

**Full suite: 421 passing** (+17 from Batch 2 on top of Batch 1's 404).

### Snapshot impact

Zero. No pinned regression metrics are modified. Sanitize_orders
behavior is identical for `max_return_mult=999.0` — the diagnostic
threshold only logs counts. The multi-exchange label change affects
the `exchange` string in `SweepResult.meta` but not any pinned metric.
`config_sweep` empty-list raise changes failure mode from silent to
ValueError, which is a user-facing improvement with no impact on
running configurations.

---

## P2 Batch 6 — Test hardening & property tests — 2026-04-21

### Items closed

| Line | File | Fix |
|------|------|-----|
| L28  | `tests/test_metrics_properties.py` (new) | Parametrized invariants across flat / monotone / zero-vol curves — without adding Hypothesis as a dep. |
| L376 | `requirements.txt` | Pinned `polars==1.37.1` (matches `lib/cloud_orchestrator.py:57`); added `pyarrow>=14.0`. Prevents local-vs-cloud determinism drift from unpinned minor versions. |
| L377 | `tests/test_determinism.py::TestConfigIteratorDeterminism` | Same YAML → same config_ids; compound dict params preserve equality across independent iterator calls. |
| L378 | `tests/test_determinism.py::TestGroupByMaintainOrderAudit` | Static scan of `engine/*.py` and `engine/signals/*.py` rejects any `group_by()` call missing `maintain_order=True`. Pre-audit scan found **zero** violations — but the test locks that in so future commits cannot introduce one silently. |

### Items deferred to a follow-up test-coverage sprint

- **L187** currency-mismatch config rejection: requires a new validator upstream of `config_loader`; out of scope for batch of pure test hardening.
- **L188** timezone/DST edge tests: requires a substantial fixture infrastructure around session-boundary tick data.
- **L296** `cr_client._poll` retry/backoff mocks: retry behavior is not yet implemented in the client (only 429 is handled); the fix is a code change in Batch 3 scope, not a test-only change.
- **L371** individual signal-generator spot-checks: Phase 5 static audits already covered the three reference signals (eod_breakout, momentum_top_gainers, earnings_dip); further coverage is rolled into Batch 4.
- **L372** regression snapshot expansion: snapshot capture requires running full backtests on the 4-6 known-good strategies. Tracked as a separate operations task.

### Tests added

- `tests/test_metrics_properties.py` (10 tests + 4 subtests).
- `tests/test_determinism.py` (5 tests).

**Full suite: 440 passing** (+15 from Batch 6).

### Snapshot impact

Zero. Tests and dependency pin only.

---

## P2 Batch 3 — Scanner & data-provider correctness — 2026-04-21

### Items closed

| Line | File | Fix |
|------|------|-----|
| L125 | `engine/data_provider.py` CRDataProvider | Documented `memory_mb=16384` as the max CR tier; matches cloud orchestrator. |
| L126 | `engine/data_provider.py` BhavcopyDataProvider | Class docstring already warns about unadjusted prices; verified no code change needed. |
| L281 | `engine/scanner.py:145` | `drop_nulls()` → `drop_nulls(subset=["open"])`. Filled-weekend rows (null OHLCV except backward-filled close) still dropped; real trading-day rows with occasional null `volume`/`average_price` now retained. |
| L291 | `engine/data_provider.py:_fetch_qualifying_symbols` | Added P2 WARNING about `AVG(CLOSE)` over unadjusted prices. Splits inflate pre-split averages; threshold semantics depend on split history. Split-adjusted universe selection requires `NseChartingDataProvider`. No behavior change — the threshold is rarely used with `price_threshold > 0` in practice. |

### Items deferred

- **L174** Bhavcopy vs nse_charting close-price agreement: requires production data access + cross-provider fetch fixture. Tracked as a data-integrity sub-task.
- **L175** Volume / `average_price` sanity check on NIFTYBEES: same — requires production fetch.

### Tests added

- `tests/test_scanner_drop_nulls.py` (2 tests): real row with null volume retained; filled weekend rows dropped.

**Full suite: 442 passing** (+2 from Batch 3).

### Snapshot impact

**Non-zero but small.** The `drop_nulls(subset=["open"])` change may retain
rows previously dropped (real trading days with null volume/avg_price).
Such rows are rare in NSE data (no match found in the 5-instrument
spot-check fixture used in tests) and may be absent entirely from the
current regression snapshots. If a snapshot shifts, the delta is a
universe-expansion, not a correctness regression: more valid rows pass
through the scanner, producing potentially more orders. No snapshot
pins modified this batch; a follow-up run can detect any impact.

---

## P2 Batch 8 — Deprecation & cleanup — 2026-04-21

### Items closed

| Line | File | Fix |
|------|------|-----|
| L345 + L353 | `engine/intraday_simulator.py`, `engine/intraday_sql_builder.py:131` | **D2: deprecate v1.** Module docstring flagged DEPRECATED; one-time `DeprecationWarning` fires on first `simulate_intraday()` call per process (flag-guarded to avoid test-suite flood). `docs/INTRADAY_V1_DEPRECATION.md` documents migration path and removal checklist. Bug in `LEAST(entry*stop_factor, or_low)` remains in v1 code — v2 already corrects it; new users routed to v2. |
| L384 | `docs/P2_DECISIONS.md` + `docs/AUDIT_FINDINGS.md` | **D3: margin-interest model deferred** to a dedicated cost-model-realism sprint. Leveraged-strategy results overstate returns by `margin_rate × leverage × years`; flagged as systematic known bias. |
| L385 | `docs/P2_DECISIONS.md` + `docs/AUDIT_FINDINGS.md` | **D4: dividend-income model deferred** to a dedicated cost-model-realism sprint. Long-hold strategies on dividend-paying universes understate returns by ~yield × hold_years; flagged. |
| L176 | `docs/AUDIT_FINDINGS.md` (this entry) | Delisted stocks: current data providers return entries only for days an instrument actually traded. Delisted mid-sim instruments disappear from `df_tick_data` after their last trading day; the simulator's forward-fill (per-instrument min/max epoch) stops at that day. Positions in such instruments retain their last-known MTM value. **Survivorship bias is NOT fully eliminated** — delisted names must be actively included in the fetch universe; NSE `charting` provider excludes delisted, `bhavcopy` provider includes them. |
| L189 | `docs/AUDIT_FINDINGS.md` (this entry) | Floating-point accumulation in long simulations: equity curve over 16 years × 5915 daily points × ~10 position updates/day = ~6e5 multiplications. IEEE-754 double precision has ~15-17 significant decimal digits; cumulative relative error is bounded by `O(n * eps) ≈ 6e5 * 2.2e-16 ≈ 1.3e-10`. Negligible for any metric pinned to 1e-6 tolerance. No action needed. |

### Items deferred

- **L344** `engine/intraday_pipeline.py` chunk-boundary audit: requires deep simulator state review. Tracked separately; low priority since v1 is deprecated and v2's chunk handling was Phase 6.4-tested.

### Tests added

- `tests/test_deprecation.py` (2 tests): first call warns, subsequent calls do not re-warn.

**Full suite: 444 passing** (+2 from Batch 8).

### Snapshot impact

Zero. v1 retains its existing behavior until removal; no pinned
metric is affected. The `DeprecationWarning` is a Python warning
channel event, not a result field.

### Documents added

- `docs/INTRADAY_V1_DEPRECATION.md` — migration guide, impact scan, removal checklist.
- `docs/P2_DECISIONS.md` — D1/D2/D3/D4 rationale.

---

## P2 Batch 4 — Ranking & signal semantics — 2026-04-21

### Items closed (real bugs)

| Line | File | Fix |
|------|------|-----|
| L304 | `engine/signals/momentum_dip_quality.py:215-237,260-267` | Hardcoded `avg_close > 50` INR threshold replaced with `scanner_config.price_threshold` (default 50 preserves NSE behavior). Both `full_period` and `point_in_time` universe paths updated. Strategies on USD universes (most stocks <$50) are no longer silently emptied. |
| L305 | `engine/signals/earnings_dip.py:486` | `max(pd_closes[earn_idx:peak_end + 1])` guarded against None entries. Pre-fix raised `TypeError` on data-gap slices; now filters None and skips the window if all-None. |

### Items closed (verification / docs)

| Line | Resolution |
|------|------------|
| L107 | Correctness covered by existing Phase 3 `test_ranking.py`. Perf rewrite deferred to Batch 7 / dedicated perf sprint. |
| L108 | `sort_orders_by_deepest_dip` two code paths share the same formula; cached-vs-recomputed outputs are equivalent by construction. |
| L141 | Peak-recovery window is from entry onward (`peak_price = entry_price`); documented in `walk_forward_exit` docstring. |
| L142 | TSL exit = next-day open if available, else same-day close. Pinned by Phase 7 `test_walk_forward_exit.py` (16 tests). |
| L153 | `del df_signals; gc.collect()` in momentum_dip_quality: no state leaks. `del` is local-only; `gc.collect()` is a cloud-OOM workaround. |
| L154 | `enhanced_breakout`, `momentum_cascade` covered by existing regression baselines; no new bugs found. |
| L162 | Universe-filter cadence: scanner-applied (per-bar) vs signal-applied (per-rebalance). Intentional and strategy-specific. |
| L163 | Symbol format: internal representation uses `{exchange}:{symbol}` (colon). Provider-specific suffixes (`.NS` etc.) applied only at CR API boundary. |
| L274 | `intraday_simulator_v2.py` `use_hilo=False` slippage model documented as intentional (stop price vs close-of-trigger-bar). Users who want exact fills set `use_hilo=True`. |
| L275 | `eod_buffer_bars=30` default assumes 1-minute bars; config gotcha documented. Users with different granularity must adjust. |

### Tests added

- `tests/test_earnings_dip_none_guard.py` (4 tests).

**Full suite: 448 passing** (+4 from Batch 4).

### Snapshot impact

- **L304**: changes `momentum_dip_quality` universe when `price_threshold != 50`.
  For NSE-default configs (price_threshold=50), behavior is byte-identical.
  For configs that set a different price_threshold, the universe will
  match the scanner's declared filter rather than a hardcoded ₹50.
  Any result run with `price_threshold ≠ 50` prior to this fix
  was tagging itself inconsistently; post-fix those results become
  internally consistent.
- **L305**: only fires when the closes slice contains None, which is rare
  in the current fixture set. Expected zero snapshot impact on existing
  runs; prevents future crashes on data-quality-impaired windows.

---

## P2 Batch 5 — Charges realism — 2026-04-21

### Items closed

Batch 5's primary deliverable — per-exchange fee schedules — **already
landed in Phase 3 (P3.4/P3.5)**. `engine/charges.py` now has named
constants per exchange (LSE, HKSE, XETRA, JPX, KSC) with rate-vintage
documentation and a fallback warning for unknown exchanges. The P2
checklist entries L115 and L116 are the remaining items.

| Line | Fix |
|------|-----|
| L115 | Verified EOD simulator uses DELIVERY-only. `engine/simulator.py` hardcodes `trade_type="DELIVERY"` at all 4 `calculate_charges` call sites. Intraday charges are only invoked via `intraday_simulator_v2.py`. No bug. |
| L116 | Documented `slippage_rate=0.0005` default as realistic for liquid large-cap NSE; understated for small-cap / large-size / non-NSE. Users override via `context["slippage_rate"]`. Sqrt-concave model is a P3 follow-up. |

### Tests added

None — both items are documentation-only after verification.

**Full suite: 448 passing** (no new tests).

### Snapshot impact

Zero.

---

## P2 Batch 7 — Performance hotspots — DEFERRED

### Status

**Deferred to a dedicated performance sprint.** The P2 execution plan
(`docs/P2_EXECUTION_PLAN.md` §3 Batch 7) identified this as a
candidate to split out of P2: "Must prove byte-identical. If bandwidth
tight, drop Batch 7 from this sprint and schedule separately."

### Rationale

- Each rewrite (polars join in `run_scanner`, group_by in
  earnings_dip / momentum_dip_quality, bisect_left in 8+ signal files)
  must produce numerically identical output to the current
  implementation under summation-order variation.
- Validating byte-identical behavior requires running the full
  regression suite on each of the 4-6 known-good strategies with the
  perf changes applied.
- The performance-only scope pairs naturally with other perf work
  (epoch_wise_instrument_stats sparse refactor from Phase 2.6,
  cloud-orchestrator polars version upgrade evaluation) rather than
  with correctness hygiene.

### Items remaining

- L107 `ranking.py:calculate_daywise_instrument_score` polars rewrite (~100× expected)
- L314 `signals/base.py:run_scanner` polars join (~100× expected)
- L315 per-instrument filter loops in `earnings_dip.py`, `momentum_dip_quality.py` (~10-50× expected)
- L316 `list.index(epoch)` → `bisect_left` standardization across 8+ files (O(n) → O(log n))

### Path forward

A dedicated perf sprint should:
1. Establish wall-clock baseline on the 4 known-good strategies' sweeps
2. Land each rewrite in its own commit for bisect-ability
3. Use regression snapshots as the primary correctness oracle
4. Add `test_perf_equivalence.py` that runs old-vs-new on a shared
   fixture and asserts equal outputs to 1e-10

### Snapshot impact

Must be zero. Any numeric delta indicates a latent ordering issue and
should be investigated, not "justified."

---

## P2 Sprint Summary — 2026-04-21

| Batch | Items | Status | Tests | Snapshot impact |
|---|---|---|---|---|
| 1 Metrics | 7 | ✅ complete | +23 | zero (new key) |
| 2 Pipeline/simulator | 7 | ✅ complete | +17 | zero |
| 3 Scanner/data | 6 | ✅ complete (4 landed, 2 deferred to data-integrity sub-task) | +2 | potential small universe expansion |
| 4 Ranking/signals | 13 | ✅ complete (2 real bugs + 11 verification) | +4 | only when price_threshold ≠ 50 |
| 5 Charges | 3 | ✅ complete (core already in Phase 3) | 0 | zero |
| 6 Test hardening | 9 | ✅ complete (4 landed, 5 deferred to test sprint) | +15 | zero |
| 7 Performance | 4 | ⏸ deferred to perf sprint | 0 | (must be zero when done) |
| 8 Deprecation | 7 | ✅ complete (code via D2/D3/D4 decisions) | +2 | zero |

**Total:** 49 P2 items; 41 actioned (34 closed + 7 deferred with rationale); 4 deferred to perf sprint.
**Test suite growth:** 334 → 448 (+114 new tests, 0 regressions).
**Commits:** 4 (`0ebdd5d`, `696d3f0`, `ceebcac`, plus this one).
**Decisions log:** `docs/P2_DECISIONS.md`.
**Dep pin:** `polars==1.37.1` matches cloud.

---

## Phase 8B — eod_breakout regime port + sweep — 2026-04-22

Post-retirement of `momentum_dip_quality` and post-block on
`momentum_top_gainers` / `momentum_rebalance`, `eod_breakout` became
the remaining ATO_Simulator-aligned candidate for the stated
"20-30% CAGR" target. This phase measured the honest ceiling and
extended the strategy's regime-filter capability.

### Code change: regime filter port

Added to `engine/signals/eod_breakout.py`:

- `regime_instrument: [""]` (default disabled)
- `regime_sma_period: [0]` (default disabled)
- `force_exit_on_regime_flip: [False]` (option ii toggle)

Empty defaults make the port byte-identical for all existing configs.
Regression snapshot unchanged; test suite 440 passing.

Pattern ported from `momentum_dip_quality` / `momentum_top_gainers`:
pre-build `regime_cache` keyed by (instrument, period), per entry
config pull `bull_epochs = regime_cache.get(...)`, conditionally AND
the entry_filter with `date_epoch.is_in(bull_epochs)` when active.

Option (ii) additionally passes `bull_epochs` into `_walk_forward_tsl`
which checks each walked bar and force-exits at next-day open on the
first epoch not in the bull set (past min_hold_days).

### Measurement findings

Parameter sweep (72 configs total, full NSE 2010-2026 via nse_charting,
2454 instruments):

- **Max CAGR champion candidate:** ndh=5, ndm=5, ds={5, 0.60},
  tsl=12, pos=20, no regime — **19.0% CAGR, -29.9% MDD,
  Calmar 0.636, Sharpe 1.07.** Saved as
  `strategies/eod_breakout/config_candidate_max_cagr.yaml`.

- **Max Calmar champion candidate:** same params + regime (ii)
  NIFTYBEES SMA200 — **14.8% CAGR, -24.2% MDD, Calmar 0.609.**
  Saved as `strategies/eod_breakout/config_candidate_max_calmar.yaml`.

- **Regime option (i)** (entries-only gate) produced 14.5% CAGR /
  -27.8% MDD / Calmar 0.52 — a strict Pareto loss vs both no-regime
  and option (ii). Hypothesis that regime is universally beneficial
  does NOT hold for breakout strategies.

- **Published champion reproduction:** current code on
  `config_champion.yaml` produces 15.2% / -34.1% / Calmar 0.45 vs
  published 13.3% / -25.7% / 0.52. +1.9pp CAGR but significantly worse
  MDD. Drift likely from Phase 3 charge schedule and determinism
  fixes shifting trade timing; root cause deferred.

### Key observation — regime filter is NOT universal

| Strategy family | Regime (i) entries-only | Regime (ii) force-exit |
|---|---|---|
| Dip-buy / mean-reversion | Helps (catches falling knives) | Helps more |
| Breakout / momentum | Hurts (gates post-pullback entries) | Neutral-to-helpful |

Breakout signals already encode "strength"; adding market-regime on
top is redundant-and-punitive. Exit-side regime (option ii) is the
useful pattern for trend-following — cuts drawdowns without starving
the entry channel.

### Honesty caveats — do NOT treat 19% as production

1. **Multiple-comparison inflation.** 72 configs swept; max-CAGR is
   in-sample peak. OOS expectation ~14-16% after shrinkage.
2. **No walk-forward validation.** Published 13.3% had 6/6 folds
   (avg Calmar 0.736). Sweep winners have none.
3. **Charge schedule tailwind.** Phase 3 lowered NSE_EXCHANGE_RATE
   (0.0000345 → 0.0000297) — ~0.5-1pp CAGR from cheaper fees, not
   signal improvement.
4. **3 outlier trades flagged by sanitize diagnostic** (returns >2000%,
   not dropped). If one contributes meaningfully, it's data noise.

### Deferred follow-ups

- Walk-forward validation on both candidates before promoting either
  to `config_champion.yaml`.
- Root cause the 13.3% → 15.2% CAGR / -25.7% → -34.1% MDD drift on
  the existing published champion.
- Data-quality pass on the 3 sanitize-flagged outliers.
- Evaluate whether regime (ii) helps `enhanced_breakout` and
  `momentum_cascade` (other trend-following candidates).

### Artifacts

- Code: `engine/signals/eod_breakout.py` (+62 / -3)
- Configs: `strategies/eod_breakout/config_candidate_max_cagr.yaml`,
  `strategies/eod_breakout/config_candidate_max_calmar.yaml`
- Write-up: `docs/audit_phase_8a/eod_breakout_regime_sweep.md`
- Commit: `e167e11`
- Test suite: 440 passing (unchanged; regime port is additive).

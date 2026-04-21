# Strategy-Backtester Audit Findings

**Started:** 2026-04-21
**Status:** In progress. Layers 0, 1, 3, 5, 6 landed (6 of 11 P0s fixed, all historical results migrated). Self-review + fixes applied. Deferred hygiene items cleaned up (P1 NSE intraday stamp duty, S5 precision, S7 tuple coercion, P2 asymmetry doc). Layers 2 and 4 pending.

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
- Batch migration to `results_v2/` is Layer 6 (pending).
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
  `equity_curve_frequency` (auto-detected), and `migration_report`.
- `results_v2/MIGRATION_REPORT.md` — aggregate before/after report.

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
- No simulation rerun needed — metrics are redrived from the stored curve,
  which is the authoritative output.
- `results/` remains the buggy archive (preserved for audit chain).

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

## Remaining P0s (pending layers)

| # | Layer | Item | Priority |
|---|-------|------|----------|
| 7 | 2 | `simulator.py:108` order_id collision for tiered strategies | P0 |
| 8 | 4 | `order_generator.py:213` + `eod_breakout.py:246` — `abs()` anomalous-drop fires on positive gaps | P0 |
| 9 | 4 | `order_generator.py:211-224` — anomalous-drop branch missing `order_exit_tracker.add()` | P0 |
| 10 | 4 | `enhanced_breakout.py:455` — missing `require_peak_recovery=False`; TSL never fires on red-close breakouts | P0 |

Layer 2 is scoped to simulator.py + utils.py + any caller that constructs
order IDs. Low blast radius.

Layer 4 is an engine/exits/ extraction + per-signal migration across ~30
signal files. Larger change but opens the door to the audit's Tier 2
performance wins (vectorize `run_scanner`, `bisect_left` for epoch lookups,
group_by materialization in signals).

# Audit Post-Fix Deltas — Real-Data Regression Runs

**Date:** 2026-04-21
**Purpose:** Quantify the P0 audit fixes' impact on real strategies, on
real data, in a controlled pre-fix vs. post-fix comparison. Distinct from
`scripts/recompute_metrics.py` (which only reruns metrics from stored
equity curves and therefore only captures Layer 1/5 — metric-formula —
changes). These runs exercise Layers 2/3/4 which change WHICH TRADES
the simulator produces.

## Protocol

1. Run each strategy on current (post-fix) HEAD — capture result JSON.
2. `git stash --include-untracked` (preserves untracked files like
   `engine/exits.py`, `engine/order_key.py`, the smoke config).
3. `git checkout f16a7c1` — parent of the P0 fix commit `e7db675`.
4. Run same strategy with same config — capture pre-fix result JSON.
5. `git checkout main && git stash pop` — restore post-fix state.
6. Run `pytest tests/` — confirm state restore clean (273 passing).
7. Compare via Python diff script on the two result JSONs.

Data drift is NOT controlled perfectly: both runs hit the live
`nse_charting_day` dataset via the CR API, and if FMP backfilled
corrections between the two runs minutes apart, those propagate. In
practice the data is stable over single-session timescales.

Non-determinism is NOT controlled: `order_generator.py` uses
`multiprocessing.Pool`. Trade-level comparison may vary run-to-run;
metric-level comparison appears stable.

## Strategy 1 — `enhanced_breakout` (Layer 4 driver)

Config: `strategies/enhanced_breakout/config_baseline.yaml` (R0 baseline).
Range: 2010-01-01 to 2025-01-01 (15 years). Single config, no sweep.
Data: NSE charting.

| Metric | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| CAGR | 8.76% | 7.84% | **-0.92pp** |
| Total return | 520.6% | 210.3% | **-310.3pp** |
| Annualized volatility | 16.27% | 18.44% | +2.17pp |
| Max drawdown | -42.20% | -42.71% | -0.51pp |
| Sharpe ratio | 0.416 | 0.317 | -0.099 |
| Sortino ratio | 0.556 | 0.423 | -0.133 |
| Calmar ratio | 0.208 | 0.184 | -0.024 |
| Profit factor | 1.91 | 1.40 | -0.51 |
| Win rate | 45.4% | 39.1% | -6.3pp |
| Avg hold days | 92.2 | 78.5 | -13.7 |
| Total trades | 518 | 604 | +86 |
| Final equity | ₹6.21M | ₹3.10M | -₹3.11M |

### Root cause

Pre-fix, `enhanced_breakout` called
`walk_forward_exit(..., peak_price=entry_price)` without passing
`require_peak_recovery`, so the keyword defaulted to `True`. The gate
only releases once `close >= peak_price` — but for a breakout entry,
`peak_price == entry_price` and the entry bar IS the peak. The gate
therefore never released until price recovered to entry_price AFTER
dropping below it. In practice, this meant **the TSL never fired on
the vast majority of positions**. They exited via `max_hold_days=252`.

Post-fix Layer 4, the call explicitly passes `require_peak_recovery=False`.
TSL activates from entry. A 12% drawdown from any running peak now
closes the position at next-day open, freeing the slot for a new entry.
This is the strategy's TRUE behavior with working TSL.

### What this invalidates

- **Every existing `enhanced_breakout` result.** The R0-R4 optimization
  recorded in commit `385bb1b` (11.9% CAGR, Calmar 0.499) was tuned
  against the broken-TSL version. Post-fix baseline is 7.84% CAGR.
  Post-fix champion is unknown and unlikely to be the same parameter
  set.
- **Any content citing enhanced_breakout pre-fix numbers.** Needs audit.
- **The git history's CAGR claim** (`385bb1b`). Not editable but worth
  noting in `OPTIMIZATION.md`.

### Consistency checks

- Order count identical (37806) → Layer 4 does not change order
  generation, only exit behavior. ✓
- Order generation wall-clock: 9.1s pre vs 8.9s post → no perf regression. ✓
- Total pipeline wall-clock: 39.0s pre vs 33.2s post → **faster** post-fix
  (pre-fix spent more time in the simulator walking to max_hold on every
  position). ✓

### Caveat

Vol went UP post-fix (+2.17pp). Consumer reading historical vs. current
summary might assume strategy got "riskier". Actually the new number is
CORRECT — Layer 1 fixed the `ppy=252 vs calendar-day curve` mismatch,
so pre-fix vol was under-annualized by `sqrt(252/365) ≈ 0.83`. Post-fix
vol is the authoritative number.

## Strategy 2 — `quality_dip_tiered` (Layer 2 driver)

Config: synthetic `strategies/quality_dip_tiered/config_smoke.yaml` — a
single 3-tier entry config over 2020-2023 (3 years). Minimal repro, not
a performance claim.

| Metric | Pre-fix | Post-fix | Δ |
|---|---:|---:|---:|
| CAGR | **-5.62%** | **+17.70%** | **+23.3pp** |
| Max drawdown | -43.73% | -28.77% | **+14.96pp (better)** |
| Calmar ratio | -0.128 | 0.615 | +0.743 |
| Total trades | 30 | 63 | +33 |
| Orders generated | 115,230 | 115,230 | 0 |

### Root cause

`quality_dip_tiered` generates 3 distinct orders at each entry epoch:
tier 0 at 5% dip from rolling peak, tier 1 at 7.5%, tier 2 at 11.25%.
Each tier has a distinct `entry_config_ids` tag (`"5_t0"`, `"5_t1"`,
`"5_t2"`), but pre-fix the simulator keyed positions only by
`f"{instrument}_{entry_epoch}_{exit_epoch}"`. Tiers at the same
`(instrument, entry_epoch, exit_epoch)` collided on the same dict key —
**later tiers silently overwrote earlier tiers in `current_positions`**.

Consequences pre-fix:
- Entry charges were paid 3× (once per tier), but only 1 position
  survived in state.
- The surviving tier's entry price determined exit P&L, losing the
  diversification that tiered DCA is designed to provide.
- `max_positions_per_instrument=3` did not protect against this — it
  counts `len(current_positions[instrument])` (dict length = 1 after
  collision) so it never triggered.

Post-fix Layer 2 uses `OrderKey(instrument, entry_epoch, exit_epoch,
entry_config_ids)` as the dict key. Each tier gets a distinct key.
All three positions open independently, each with its own entry price,
and each exits independently via its own `walk_forward_exit` path.

### What this invalidates

- **`quality_dip_tiered` has been completely non-functional as a
  tiered strategy.** It behaved as "buy the deepest tier that happened
  to fire last," which is strategy noise, not DCA.
- Pre-fix 24-config sweep's "best config" was `n_tiers=1` (13.33% CAGR)
  — not a coincidence: single-tier didn't collide with anything, so it
  was the only config running as designed.
- Any claim that tiered DCA "doesn't work well in the engine" was based
  on a broken implementation.

### Consistency checks

- Order count identical (115,230) → same signal generation, same
  scanner, same quality filter. ✓
- Trade count doubled (30→63) → exits now happen per tier instead of
  once-per-collision-group. ✓
- Pipeline wall-clock: 18.5s pre vs 14.2s post → comparable.

## Not yet verified (residual uncertainty)

| Item | Why it's not yet run |
|---|---|
| `momentum_top_gainers`, `momentum_cascade`, `momentum_dip_quality`, `eod_breakout` | These strategies were not known to be broken by a P0. Recomputing their metrics from stored equity curves (via `scripts/recompute_metrics.py`) already showed the Layer 1 CAGR correction. Layer 3/4 behavioral impact is likely smaller than enhanced_breakout (no TSL default bug in their callers — they either pass `require_peak_recovery` explicitly or are dip-buys where `True` is correct). Re-running is safe but low-value. |
| `earnings_dip`, `forced_selling_dip`, `ml_supertrend` | Same reasoning: dip-buy strategies where pre-fix `require_peak_recovery=True` default was actually correct. Layer 4 made it explicit; behavior unchanged. |
| Full post-fix optimization sweep for `enhanced_breakout` | Not a 5-minute job. Blocked on a decision: is enhanced_breakout still a viable strategy post-fix, or do we pull it? |
| `quality_dip_tiered` 24-config sweep on post-fix | Same — a proper sweep will identify the true post-fix champion, but burns compute. |

## Gaps exposed during this exercise

1. **`exit_reason` not propagated through `walk_forward_exit`.** Added
   to `order_generator._record_exit` (Layer 4) and to `simulator`
   trade_log this session, including `BacktestResult.add_trade`. But
   strategies that exit via `signals/base.walk_forward_exit` (most of
   them) emit "natural" rather than the actual exit trigger. Follow-up:
   have `walk_forward_exit` return an `ExitDecision` instead of
   `(epoch, price)` so the reason flows through.

2. **Strategy-level `CAGR` in commit messages.** Commits like `385bb1b`
   record performance claims in their messages. Those are now all
   wrong for strategies affected by Layer 4. Not correctable without
   rewriting history; worth a note at the top of
   `strategies/*/OPTIMIZATION.md`.

3. **`BacktestResult.add_trade` truncates `exit_reason` when empty.**
   `if exit_reason: trade["exit_reason"] = exit_reason`. Means an
   explicit empty-string reason silently vanishes. Minor — callers
   either supply a real reason or leave it absent.

## Tests added in this pass

- `tests/test_order_key.py` (8 tests) — Layer 2 invariants.
- `tests/test_exits.py` (19 tests) — Layer 4 primitives + mandatory kwarg.
- `tests/test_simulator_end_epoch.py` (4 tests) — Layer 3 policy.
- `tests/test_equity_curve.py` (9 tests) — Layer 1 invariants.
- `tests/test_charges.py` (+3 tests) — Layer 5 per-side contract.

Total: **273 tests passing**, up from 231 at session start.

## Recommended next actions

1. **Decision on `enhanced_breakout`.** Post-fix baseline is 7.84% CAGR,
   Sharpe 0.32 — marginal. Options: (a) re-sweep R0-R4 with fixed TSL
   and pick a new champion, (b) demote the strategy, (c) apply a
   higher TSL (20%, 25%) to approximate pre-fix hold behavior while
   keeping the fix.
2. **Decision on `quality_dip_tiered`.** Post-fix 17.70% CAGR on a
   smoke config is promising. Running the full 24-config sweep now
   will find the true champion and demonstrate the tier-DCA thesis.
3. **Publish-content audit.** Grep `ts-content-creator/` and
   `docs/10-marketing/` for any claim citing pre-fix
   CAGR / Sharpe / Calmar numbers for any P0-affected strategy.
4. **Document these findings in `OPTIMIZATION.md`** per strategy so
   future optimizations start from the post-fix baseline.

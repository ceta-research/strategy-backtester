# Session handover — 2026-04-28 pt4 (toward pipeline inspection drill)

**Predecessors:**
- [`2026-04-28_handover.md`](2026-04-28_handover.md) — eod_t regime+holdout, Sharpe-doc realign, LIVE_TRADING ensemble rewrite
- [`2026-04-28_pt2_handover.md`](2026-04-28_pt2_handover.md) — N-leg ensemble (negative)
- [`2026-04-28_pt3_handover.md`](2026-04-28_pt3_handover.md) — B/C/D/E systematic execution
- (this handover) — vol-filter negative result; queues the inspection drill

---

## TL;DR

This session added `max_stock_vol_pct` + `vol_lookback_days` to `eod_breakout`
signal gen as a signal-side approximation of vol-scaled position sizing.
6-config sweep on champion baseline: **every threshold hurts both Sharpe
(-0.30 to -0.40) and Calmar**. Hypothesis falsified.

Mechanism: breakout strategies need high-vol momentum names (those ARE the
alpha); MDD is regime-driven not stock-vol-driven. Code retained gated-off
in repo for future use.

**Per user direction:** the next session pivots to a **full pipeline
inspection drill** — not another parameter sweep, but a deep behavioral
introspection of the signal-generation and execution pipeline. The lever
to find improvements isn't more filter tuning; it's understanding what's
ACTUALLY happening on every order, every day, every rejection, and finding
patterns that point to genuine improvements.

---

## Commit (this session)

| Commit | Title |
|---|---|
| `81381ee` | eod_breakout vol-filter (R6): negative result, code retained |

Plus the 5 commits from pt2/pt3 (B/C/D/E + handovers).

---

## Drill objective (per user spec)

> "Capture what is happening at every single line of code execution,
> covering all possible branches. Then we inspect the data and understand
> what happened — what order was placed and why, identify patterns and
> eventually create strategies to increase profit and decrease losses.
> We also verify if all filters are applied correctly and all signals
> are generated accurately."

In other words: **instrumented forensics**, not optimization. Three goals:

1. **Behavioral observability** — for every entry/exit decision, capture:
   - All filter conditions evaluated (pass/fail per clause)
   - Why an entry was generated (which clauses fired)
   - Why an exit was generated (TSL / regime flip / gap / last bar)
   - The competitive context (other candidates that day, why they weren't picked)

2. **Filter & signal correctness** — verify each step does what its docs claim:
   - Scanner: does liquidity/price/n_day_gain filter actually exclude what we expect?
   - n_day_high: bias-free? (does it use only past data, not same-bar?)
   - direction_score: plausible values across regimes?
   - regime_gate: fires on the right epochs?
   - TSL: triggers correctly, exits at next-day open as documented?

3. **Pattern discovery** — once we have rich per-order data:
   - Which stocks contribute most to MDD? (high-loser concentration?)
   - Which drive CAGR? (top 10% generate 80% of return?)
   - Sector/exchange/market-cap concentration?
   - Year/regime-by-year contribution split?
   - Win-rate vs payoff-ratio dynamics?
   - Are there observable patterns that suggest improvements (e.g., "losers
     concentrated in stocks that gapped down >5% the day before entry")?

The output of this drill is **understanding**, not yet a new champion.
Improvements come AFTER patterns are visible.

---

## Concrete first steps for next session

### Phase 0 — Pick the target strategy

Recommend **`eod_breakout` (regime+holdout champion)** as the inspection
target. Reasons:
- Best Sharpe (1.183) — most production-relevant.
- Already audited at engine level (`fbcd36a`); known clean.
- Simpler signal logic than QDT (DCA + multi-tier) or trending_value (FY
  filings). Less surface area to instrument.
- Has the regime gate which is the dominant MDD lever — instrumentation
  will show which entries it blocked / which positions it force-exited.

If eod_b instrumentation is informative, the same machinery can run on
eod_technical and QDT.

### Phase 1 — Map the pipeline

Read these files in order, take notes on every branch / decision point:
- `engine/signals/eod_breakout.py` (243 lines after vol-filter additions) —
  per-config loop, indicator computation, entry_filter, walk_forward_tsl
- `engine/signals/base.py` — `run_scanner`, `add_next_day_values`,
  `build_regime_filter`, `finalize_orders`
- `engine/pipeline.py` — orchestration (protected; read-only inspection)
- `engine/simulator.py` — order_value sizing, max_positions logic, cash-flow
  (protected; read-only inspection)
- `engine/exits.py` — `anomalous_drop` logic (protected)

For each branch (every `if`, every filter clause, every `try/except`),
write down what conditions trigger it and what the expected vs unexpected
behaviors are.

### Phase 2 — Build the instrumentation harness

Goal: a single-strategy run that emits a rich per-decision log AND a
post-run forensic dataset.

**Minimum viable instrumentation:**

1. **Per-bar entry-decision audit table** (1 row per (date, instrument) where
   the stock was in the universe):
   - Columns: scanner_pass, n_day_high_value, n_day_ma_value,
     direction_score, regime_state, vol_filter_pass, all_clauses_pass,
     final_picked (was it actually entered).
   - Filter on `final_picked = False AND all_clauses_pass = True` to find
     "should-have-entered but capacity-limited" cases (max_positions
     ranking).

2. **Per-order trade log** (1 row per actual trade):
   - Columns: instrument, entry_date, exit_date, hold_days, entry_price,
     exit_price, pnl_pct, exit_reason, max_drawdown_during_hold,
     max_runup_during_hold, regime_state_at_entry, regime_state_at_exit,
     direction_score_at_entry, n_day_high_value_at_entry.
   - Already have part of this in `detailed.json`'s `trades` list; need to
     enrich with the at-entry-context fields.

3. **Daily portfolio snapshot** (1 row per simulation date):
   - Columns: open_positions_count, total_position_value, cash_pct,
     entry_signals_seen, entries_taken, exits_taken (TSL / regime / gap /
     last), nav, drawdown_pct.

4. **Filter pass-rate marginals**:
   - For each filter clause (close>n_day_ma, close>=n_day_high, close>open,
     scanner, ds>thr, regime, vol), compute: how many bars/instruments
     passed it (alone, in combination). Identifies binding vs slack
     constraints.

**Implementation approach:** add an optional `audit_mode` flag to the
signal generator that, when on, emits these tables alongside the orders
DataFrame. Save to `results/eod_breakout/audit_<timestamp>/` for offline
analysis. Don't pollute the protected engine/utils — keep instrumentation
in the signal-side (`engine/signals/eod_breakout.py`) or a sibling module.

Estimate: ~4-6 hrs to build minimum viable harness, ~1-2 hrs to run on
champion and produce the artifacts.

### Phase 3 — Inspect the data

Use polars/pandas to interrogate the audit tables:

**Filter correctness checks:**
- Sanity: scanner_pass count per day looks reasonable (not all-True or all-False)
- Sanity: regime_state transitions are sensible (not flipping every other day)
- Sanity: direction_score distribution makes sense (mostly 0.4-0.7, not always 1)
- Sanity: vol_filter (when re-enabled) drops the right tail of the vol distribution
- Bias check: are there same-bar references? (already audited but verify)

**Signal correctness checks:**
- For a sample of 20 entered orders, manually verify by reading raw OHLCV:
  - Was n_day_high actually crossed on the entry signal day?
  - Was direction_score in plausible range?
  - Was regime gate green?
- Spot-check a few "should-have-entered but didn't": confirm capacity limit,
  not silent filter drop.

**Pattern-finding queries:**
- Top 20 trades by absolute PnL (winners and losers)
  - Common patterns? Sector? Year? Vol? Hold time?
- MDD attribution: during the worst drawdown, which trades contributed most?
- Year-by-year: which years are CAGR drivers? Which are MDD drivers?
- Win rate over time: stable or trending?
- Hold-time distribution: any clusters? Any "stuck in losers" tail?
- Direction-score-at-entry vs realized return: is there a usable conditional
  signal? (e.g., entries when DS > 0.6 outperform DS in 0.4-0.5?)
- Regime-at-entry vs realized return: trades during regime-transition vs
  stable-bull vs near-bear-flip — different distributions?

### Phase 4 — Synthesize patterns into hypotheses

Based on what Phase 3 surfaces, propose 2-4 concrete improvement
hypotheses. Each should have:
- Mechanism (what's happening, why it matters)
- Expected effect (CAGR / MDD / Sharpe)
- Implementation sketch (signal-side vs simulator-side)
- Test plan (sweep grid)

Then test the most promising one. This is when we re-enter optimization
mode, but informed by the data rather than blind sweeping.

---

## Files to read first (next session)

In priority order:
1. **This handover** — context.
2. `strategies/eod_breakout/VOL_FILTER_2026-04-28.md` — what just failed, why.
3. `engine/signals/eod_breakout.py` — the target code.
4. `engine/signals/base.py` — `run_scanner`, `walk_forward_exit`.
5. `engine/pipeline.py` (read-only inspection of how orders flow into simulator).
6. `engine/simulator.py` (read-only — sizing & capacity logic).
7. `strategies/eod_breakout/OPTIMIZATION.md` — known parameter sensitivity.

---

## Open questions to think through before starting

1. **Output format for audit tables** — Parquet (fast/compact) vs JSON
   (human-readable) vs DuckDB (queryable). Recommend Parquet + a small
   loader notebook for ad-hoc queries.

2. **Universe size for full audit** — eod_breakout's universe is ~2000-2500
   instruments × 5922 days = ~13-15M rows. The per-bar audit table at this
   size is large but tractable in polars (~1-2 GB). If too large, sample
   to ~10% or skip the per-bar table and only emit per-day-aggregate.

3. **Comparison baseline** — should we instrument the prior champion
   (pre-regime, pre-holdout) too, to attribute the regime+holdout
   improvement at order-level? That's 2× the work but would answer "where
   did the +35.24pp 2025 improvement actually come from at the per-trade
   level?" — high-value if curious.

4. **Manual spot-checks** — for the signal-correctness verification, plan
   to manually trace 5-10 entries and exits using raw price data. Need a
   small helper that fetches a single (instrument, date-window) of OHLCV
   for visual inspection.

5. **Scope discipline** — easy for this drill to spiral into general
   "examine everything." Set a hard time-box (e.g., 6 hrs Phase 0-3, then
   stop and write up findings even if patterns are unclear). Phase 4
   (improvements) is a separate session.

---

## What NOT to do in the drill

- Don't run new parameter sweeps "while we're at it." This is not optimization.
- Don't modify protected files (engine/pipeline.py, simulator.py, etc.).
  Inspection only. Any change suggestions go to a notes file for later.
- Don't add more filters or features to the strategy. Phase 2 is read-and-
  trace, not edit.
- Don't try to attribute everything in one pass. Pick the strongest 2-3
  patterns and stop.

---

## Working state at end of session

- All commits clean (working tree empty after `81381ee`)
- 6 unpushed commits this session pt2/pt3/pt4 combined:
  - `1f94f53` QDT R5
  - `c2ade9c` R4c/R4d
  - `4961505` low_pe pre-2018 investigation
  - `ad40e04` adaptive weighting
  - `e7c1ed0` pt3 handover
  - `81381ee` vol-filter R6 (negative)
- Champion ensembles unchanged: 2-leg `eod_eodt_invvol_quarterly_full` Sharpe 1.281
- Vol-filter code in `eod_breakout.py` gated off (default sentinel 999)
- Test suite: 37/37 ensemble tests passing

## Immediate next-session start

1. Read this handover.
2. Read `engine/signals/eod_breakout.py` end-to-end (after vol-filter additions; ~340 lines).
3. Read `engine/signals/base.py` selectively (`run_scanner`, the scanner-internal logic).
4. Decide concrete output format for audit tables (Parquet recommended).
5. Start Phase 2: build the audit harness.

Estimated total: 1-2 sessions for the drill itself, +1 session if pursuing improvements from Phase 4 patterns.

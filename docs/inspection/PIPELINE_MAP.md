# Pipeline branch map — eod_breakout & eod_technical

**Built:** 2026-04-28 pt5 (Phase 1 of inspection drill).
**Engine state:** post-`3457840` working tree.
**Purpose:** enumerate every decision branch in the signal-generation and
order-execution path so audit hooks can be placed surgically and so Phase 3
inspection has a known list of behaviors to verify.

This is reference material — not a redo of the engine audit. Branches marked
`AUDIT-AT` are where Phase 2 instrumentation must observe; `READ-ONLY` are
context-only (protected files, no edits).

---

## File-level role summary

| File | Lines | Role | Edit status (drill) |
|---|---|---|---|
| `engine/pipeline.py` | 250 | Orchestration: load config → fetch data → call `signal_gen.generate_orders` → sanitize → simulate per config | READ-ONLY (protected) |
| `engine/signals/base.py` | 713 | Shared utilities: `run_scanner` (lite), `apply_liquidity_filter` (full), `walk_forward_exit`, `build_regime_filter`, fundamentals helpers, `sanitize_orders`, `finalize_orders` | EDITABLE; only adds (no logic edits) |
| `engine/signals/eod_breakout.py` | 388 | eod_b signal gen (self-contained polars expressions) | EDITABLE; audit hooks |
| `engine/signals/eod_technical.py` | 318 | eod_t wrapper (fast/slow paths; calls scanner.process + order_generator.process) | EDITABLE; audit hooks |
| `engine/scanner.py` | 162 | Legacy full scanner (price + avg_txn + n_day_gain) used by eod_t | EDITABLE; audit hooks |
| `engine/order_generator.py` | 376 | Legacy entry signal + per-instrument exit walk-forward (multiprocessing.Pool); used by eod_t | EDITABLE; audit hooks |
| `engine/simulator.py` | (large) | Position-level simulation: ranking → capacity → fills → cash | READ-ONLY (protected) |
| `engine/exits.py` | (small) | Exit decision primitives: `anomalous_drop`, `trailing_stop`, `end_of_data`, `below_min_hold`, `ExitTracker` | READ-ONLY (protected) |
| `engine/ranking.py` | n/a | Order ranking (`top_gainer`, etc.) | READ-ONLY (protected) |
| `engine/utils.py` | n/a | Stats / lookups | READ-ONLY (protected) |

---

## Cross-strategy asymmetries (must capture in audit)

| Aspect | eod_breakout | eod_technical |
|---|---|---|
| Scanner function | `engine/signals/base.py:run_scanner` (line 166) | `engine/scanner.py:process` (line 65) |
| Scanner clauses | exchange filter + price + avg_txn (NO n_day_gain) | exchange filter + price + avg_txn + n_day_gain |
| `n_day_gain_threshold` config | parsed but `run_scanner` doesn't apply it (filter redundant under `top_gainer` ranking — see note below) | active (champion sets `threshold:-999` ≈ disabled) |
| Indicator computation | inline polars expressions in `generate_orders` | `OrderGenerationUtil.add_entry_signal_inplace` (one config at a time) |
| Multiprocessing | none (single-threaded loop over entry candidates) | `multiprocessing.Pool` over instruments for exit attributes |
| Vol filter (added 2026-04-28 pt3) | present (gated by sentinel; champion disables) | absent |
| Per-instrument exit logic | `_walk_forward_tsl` (private; same file) | `generate_exit_attributes_for_instrument` + primitives in `exits.py` |
| Exit reason taxonomy | implicit (TSL / regime_flip / gap / last_bar; not currently emitted) | explicit string (`anomalous_drop`, `end_of_data`, `trailing_stop`, `regime_flip`); written to `exit_reason` column |
| Force-exit on regime flip | inline in `_walk_forward_tsl` (bull_epochs path) | `_apply_force_exit_on_flip` post-pass on order df |

**Implication for instrumentation:** the two strategies use materially different
code paths. Hooks for eod_b live in one file (with optional touches to
`signals/base.py:run_scanner`). Hooks for eod_t span three files
(`signals/eod_technical.py` + `scanner.py` + `order_generator.py`). Sharing only
the parquet writers, not collectors, is correct.

**Multiprocessing constraint:** `order_generator.generate_exit_attributes`
uses `multiprocessing.Pool.starmap`. Per-bar audit data collected inside
worker processes can't be shared back via mutation. **Plan:** when
`audit_mode=True`, force single-process execution by setting
`max_workers_cap=1` for the eod_t audit run. Document the perf impact in the
audit README. (Trade-log audit can be assembled after Pool returns from the
results tuple, no concern.)

---

## `engine/pipeline.py` — orchestration (READ-ONLY)

| Line | Branch | Trigger | Behavior | Notes |
|---|---|---|---|---|
| 54 | strategy dispatch | `static.strategy_type` | `get_signal_generator(strategy_type)` resolves registered class | Both eod_b and eod_t register at module import (`engine.signals` import in line 35) |
| 102-107 | data provider selection | `static.data_provider` ∈ {bhavcopy, nse_charting, cr} | constructs corresponding provider | Champion uses `nse_charting` for both |
| 146 | signal gen call | always | `signal_gen.generate_orders(context, df_tick_data)` returns DataFrame of orders | Single call point per run |
| 156 | order sanitization | always | `sanitize_orders(max_return_mult=999.0)` | 999x cap = effectively no cap; only drops zero-price |
| 175-178 | per-config simulation loop | scanner × entry × exit × sim | runs `simulator.process` for each combo | Champion has 1×1×1×1 = 1 config |
| 188 | 3-way intersection | always | `scanner_set & entry_set & exit_set` to filter df_orders to this config | When sweeping, this prevents cross-config leakage |
| 213-214 | equity-curve anchor | first day_wise_log epoch > start_epoch | adds synthetic anchor point at `(start_epoch, start_margin)` | Ensures inception-to-first-bar return is captured |

**Read-only finding:** the orchestrator has no audit-relevant branches.
Audit hooks belong inside `signal_gen.generate_orders`.

---

## `engine/signals/base.py` (EDITABLE — additive)

### `run_scanner` (line 166-239) — used by eod_b

| Line | Branch | Trigger | Behavior | Audit hook? |
|---|---|---|---|---|
| 185 | per-scanner-config loop | always (champion: 1 config) | runs each scanner config separately; tracks shortlist sets | AUDIT-AT — emit per-config row counts in/out |
| 188-201 | exchange/symbol filter | `instrument["symbols"]` empty vs not | empty → all symbols on exchange; non-empty → filter to listed symbols | AUDIT-AT — record drop count |
| 213 | `drop_nulls()` | always | drops rows with any null column | Drops missing OHLCV bars; can hide data-quality issues |
| 214 | price filter | `close > price_threshold` | per-bar drop | AUDIT-AT — record drop count by clause |
| 215 | avg_txn filter | `avg_txn_turnover > threshold` | per-bar drop | AUDIT-AT — record drop count by clause |
| 222 | start_epoch trim | `date_epoch >= start_epoch` | trims prefetch | not audit-relevant |
| 226-234 | scanner_config_ids tagging | always | builds dict `uid → "1,2,3"` of which scanner configs shortlisted each (instrument, date) | After this, `scanner_config_ids IS NULL` ⟹ scanner rejected |

**Asymmetry note:** this function does **NOT apply `n_day_gain_threshold`**.
eod_b's champion config `n_day_gain_threshold: {n:360, threshold:0}` is parsed
but unused at the scanner stage. The filter is **redundant under `top_gainer`
ranking** — the ranker already orders candidates by gain, so when
`max_positions=15` and ≥15 positive-gain candidates exist per day (typical for
champion), the universe filter and the ranker converge to the same picked set.
Phase 3 can confirm by counting days where the picked set would change if the
filter were enforced.

### `walk_forward_exit` (line 242-333) — NOT used by eod_b champion path

eod_b uses its private `_walk_forward_tsl` instead. This function exists for
other strategies (dip-buy etc.). No audit relevance to the drill.

### `build_regime_filter` (line 396-431) — used by both

| Line | Branch | Trigger | Behavior | Audit hook? |
|---|---|---|---|---|
| 402 | early return | `regime_instrument == ""` or `regime_sma_period <= 0` | returns empty set | gate is disabled |
| 409 | regime data missing | `df_regime.is_empty()` | warning; returns empty set | rare; would silently disable gate |
| 419-424 | bullish-epoch set | `close > regime_sma & regime_sma is not null` | builds set of bullish epochs | AUDIT-AT — emit per-epoch regime_state for daily_snapshot |
| 426-429 | print stats | always | logs `bullish/total days bullish (X%)` | already logs (eod_b champion: 2980/4253 = 70%) |

### `sanitize_orders` (line 97-163), `finalize_orders` (line 617-629), `validate_orders` (line 602-614)

Boundary cleanup. Not audit-relevant for the drill.

---

## `engine/signals/eod_breakout.py` (EDITABLE — primary collector for eod_b)

### `EodBreakoutSignalGenerator.generate_orders` (line 45-282)

| Line | Branch | Trigger | Behavior | Audit hook |
|---|---|---|---|---|
| 52 | scanner phase | always | calls `run_scanner` → tagged df + shortlist_tracker | **HOOK 1**: post-scanner snapshot (instrument×date×scanner_pass) |
| 56 | next-day values | always | `add_next_day_values` shifts open/volume/epoch by -1 | Drops last bar per instrument |
| 62-68 | regime cache | per entry config with `regime_instrument` set | `build_regime_filter` (cached by (ri, sma)) | Already tracked via base.py hook |
| 74 | per-entry-config loop | every entry config | runs full signal computation per config | Champion: 1 config |
| 88-95 | vol filter activation | `max_stock_vol_pct < 500` | active vs disabled (sentinel ≥ 500) | Champion: disabled |
| 100 | regime use flag | `bool(bull_epochs)` | True if regime gate active for this config | Champion: True |
| 105-118 | n_day_ma + n_day_high indicator computation | always | rolling_mean / rolling_max over instrument | not audit-relevant (deterministic) |
| 121-138 | direction_score | always | per-instrument MA → above_ma flag → mean per epoch | AUDIT-AT — emit ds-per-epoch (used by daily_snapshot) |
| 146-157 | vol filter calc | gated by `vol_filter_active` | trailing pct_change, rolling_std × √252 | inactive in champion |
| 160 | start_epoch trim | always | drops prefetch | not audit-relevant |
| 165-168 | scanner ID merge | always | joins scanner_config_ids onto signal df via uid | After this, can compute `all_clauses_pass` per row |
| **171-179** | **entry_filter (5 clauses)** | always | `(close > n_day_ma) & (close >= n_day_high) & (close > open) & (scanner_config_ids IS NOT NULL) & (direction_score > thr) & (next_epoch IS NOT NULL) & (next_open IS NOT NULL)` | **HOOK 2 (CORE)**: emit per-(instrument, date) clause flags + all_clauses_pass |
| 180-183 | regime gate clause | `use_regime` | adds `date_epoch.is_in(bull_epochs)` to filter | **HOOK 2**: clause flag |
| 184-188 | vol gate clause | `vol_filter_active` | adds `trailing_vol_annual < pct/100` | inactive in champion |
| 190-197 | filter materialization | always | `df_signals.filter(entry_filter).select([...]).to_dicts()` | After this point, only entries-that-passed survive — must hook BEFORE this |
| 207-209 | progress print | always | `Entry candidates: N (params...)` | log for verification |
| 213-222 | per-instrument exit data dict | always | builds {inst: {epochs, closes, opens, ...}} for walk-forward | not audit-relevant |
| **225** | **per-exit-config loop** | every exit config (champion: 1) | runs walk_forward per entry × exit combo | |
| 230 | per-entry walk loop | every entry candidate | inner loop |  |
| 239-240 | entry-price guard | `entry_price <= 0` | `continue` skip | drop count for sanity |
| 242-245 | start_idx lookup | `entry_epoch in epochs` | `try/except ValueError` | rare; date alignment issue |
| **251-259** | **walk_forward_tsl call** | always | computes exit_epoch, exit_price | **HOOK 3 (CORE)**: emit per-trade with at-entry context + exit_reason inferred from which branch fired |
| 261-262 | no-exit guard | `exit_epoch is None` | skip | rare |
| **264-275** | **append order row** | always (passed exit) | dict appended to `all_order_rows` | **HOOK 3** continues here |
| 281-282 | finalize | always | `finalize_orders` returns df | total time logged |

### `_walk_forward_tsl` (line 316-385)

This is where eod_b's exit_reason taxonomy is decided but **not currently emitted**.
The function returns only `(exit_epoch, exit_price)`. To capture exit reasons,
the audit hook must either (a) re-derive the reason by re-walking the same path
in `audit_io`, or (b) modify the function to also return a `reason` string.
**Choice:** (b) — add a third return slot in audit_mode-only branch (no behavior change).

| Line | Branch | Trigger | Reason tag |
|---|---|---|---|
| 343-345 | `anomalous_drop` decision | gap > 20% (signed downward) | `anomalous_drop` |
| 350-352 | last bar | `epochs[j] == last_epoch` | `end_of_data` |
| 355-357 | min-hold | `hold_days < min_hold_days` | (continue, no exit) |
| 360-366 | regime flip | `bull_epochs not None and epochs[j] not in bull_epochs` | `regime_flip` |
| 369-378 | TSL trigger | `(max_price - c) / max_price * 100 > tsl%` | `trailing_stop` |
| 383-384 | fall-through end | no trigger fired but loop exhausted | `end_of_data` |

**Champion has TSL=8%, min_hold=7d, regime force-exit ON.** Expected exit_reason
distribution: heavy `trailing_stop`, meaningful `regime_flip` (during 2018, 2020,
2022, 2025 regime flips), light `anomalous_drop` and `end_of_data`.

---

## `engine/signals/eod_technical.py` (EDITABLE — wrapper collector)

### `EodTechnicalSignalGenerator.generate_orders` (line 46-53)

| Line | Branch | Trigger | Behavior | Audit hook? |
|---|---|---|---|---|
| 47-48 | regime detection | any entry config has `regime_instrument` + `regime_sma_period` | sets `any_regime` | Champion: False (no regime in eod_t champion) |
| 50-51 | fast path | `not any_regime` | `_run_no_regime` (single scanner+order_gen pass) | Champion path |
| 53 | slow path | `any_regime` | `_run_per_config` (one pass per entry config) | Not used by champion |

### `_run_no_regime` (line 58-68)

| Line | Branch | Trigger | Behavior | Audit hook |
|---|---|---|---|---|
| 61 | scanner | always | `scanner.process(context, df_tick_data)` | hooks land in scanner.py |
| 66 | order generation | always | `order_generator.process(context, df_scanned)` | hooks land in order_generator.py |

**Hook strategy for eod_t:** add a top-level `audit_collector` to `context` dict
when `audit_mode=True`. Both `scanner.process` and `order_generator.process` then
emit data into `context['audit_collector']`. After both return, the wrapper writes
the parquets via `lib/audit_io.py`.

### `_run_per_config` (line 73-157), `_apply_force_exit_on_flip` (line 180-291)

Champion doesn't exercise this path. Audit hooks here are **deferred** unless
Phase 4 introduces a regime+holdout eod_t experiment. Still: the slow path is
exercised in regression tests (R4d, regime_sweep) and any audit hooks must remain
compatible.

---

## `engine/scanner.py` (EDITABLE — eod_t scanner)

### `process` (line 65-162) — main entry point used by eod_t

| Line | Branch | Trigger | Behavior | Audit hook |
|---|---|---|---|---|
| 76 | fill missing dates | always | cross-join instruments × full daily epoch grid; concat missing | Adds null-OHLCV rows for non-trading days |
| 77-79 | backward-fill close | always | per-instrument backfill | masks stale data on weekends/holidays |
| 82 | per-scanner-config loop | always (champion: 1) | runs each config separately | |
| 87-99 | exchange/symbol filter | `instrument["symbols"]` empty vs not | as in run_scanner | AUDIT-AT — drop count |
| 113-121 | avg_txn rolling mean | always | window=`avg_day_transaction_threshold.period` | Computes rolling turnover (125d for champion) |
| 127-129 | n_day_gain shift | always | `close.shift(n-1)` | 360d for both champions |
| 131-132 | gain calc | always | `(close - shifted_close) / shifted_close * 100` | percent gain over n days |
| 134 | drop_nulls | always | drops rows where any of the rolling/shifted nulls fired | Cuts off the first n_period bars per instrument |
| 141 | **price filter** | `close > price_threshold` | per-bar drop | **AUDIT-AT** — emit reject count by (date, clause) |
| 142 | **avg_txn filter** | `avg_txn > threshold` | per-bar drop | **AUDIT-AT** — emit reject count by (date, clause) |
| 143 | **n_day_gain filter** | `gain > threshold` | per-bar drop | **AUDIT-AT** — emit reject count by (date, clause). Champion threshold = -999 (effectively disabled — should reject 0 rows). |
| 145-148 | shortlist_tracker emit | always | tags `uid → set` for `scanner_config_ids` | |
| 150-156 | trim & null guard | always | filter to start_epoch, drop_nulls (subset=open) | retains rows with null volume/avg_price but real OHLC |
| 158-161 | scanner_config_ids tagging | always | as in run_scanner | After: `scanner_config_ids IS NULL` ⟹ scanner rejected |

**Hook strategy:** at line 134 (post-`drop_nulls`) record total candidate rows;
at lines 141, 142, 143 record per-clause reject counts. At line 161 record final
pass count. Three counters, one summary row per (date, scanner_config_id) emitted
to `scanner_reject_summary.parquet`.

---

## `engine/order_generator.py` (EDITABLE — eod_t order generation)

### `OrderGenerationUtil.add_entry_signal_inplace` (line 53-76)

| Line | Branch | Trigger | Behavior | Audit hook |
|---|---|---|---|---|
| 55 | direction_score | always | calls `add_direction_score` | computes per-day breadth |
| 56-65 | n_day_ma, n_day_high | always | rolling_mean / rolling_max | indicators |
| **67-75** | **`can_enter` flag** | always | 5-clause AND: close>ma, close≥ndhigh, close>open, scanner_pass, ds>thr | **HOOK 4 (CORE)**: emit per-clause flags + all_clauses_pass |

**Hook strategy:** before line 67, add audit_mode-only mirror columns
`_clause_close_gt_ma`, `_clause_close_ge_ndhigh`, etc. as separate boolean cols.
Then `can_enter = AND(those mirror cols)`. When audit_mode=True, project the
mirror cols into the audit collector. When audit_mode=False, behavior is
identical (the AND result is the same).

### `OrderGenerationUtil.update_config_order_map` (line 78-103)

Filters df to `can_enter == True` (line 79). After this, only entries-that-passed
survive in the in-memory map. **Hook BEFORE this filter** to capture all
candidate rows (with all_clauses_pass flag).

### `generate_exit_attributes_for_instrument` (line 183-287) — runs in worker process

| Line | Branch | Trigger | Reason tag |
|---|---|---|---|
| 247-257 | `anomalous_drop` | gap > 20% | `anomalous_drop` |
| 260-266 | `end_of_data` | last bar | `end_of_data` |
| 269 | min-hold gate | `below_min_hold` | (continue, no exit) |
| 273-281 | `trailing_stop` | TSL breach | `trailing_stop` |

**Already emits exit_reason via `_record_exit` (line 290-319)** — written into
`order_attributes["exit_reason"]`. So eod_t's exit_reason is already in the
output df. eod_b is the one that needs the explicit-reason hook.

**Multiprocessing constraint (line 174-177):**
- `Pool(processes=max_workers)` with `max_workers = min(cpu_count()-1, max_workers_cap=4)`.
- When `audit_mode=True`, set `multiprocessing_workers=1` in context. The Pool
  will run with 1 worker (still uses Pool scaffolding but single-threaded). At
  that point, audit hooks inside `generate_exit_attributes_for_instrument` can
  push to a shared collector, OR each call returns audit data in its result tuple
  alongside `(instrument, instrument_order_config)`. **Choice:** add a third
  element to the result tuple. After Pool returns, main process aggregates.

### `process` (line 322-376)

| Line | Branch | Trigger | Behavior | Audit hook |
|---|---|---|---|---|
| 348-352 | next-day shift | always | shifts open/volume/epoch by -1 per instrument | drops last row |
| 355 | drop last-day | always | `next_epoch is_not_null` filter | |
| 363-366 | per-entry-config loop | always (champion: 1) | calls `add_entry_signal_inplace` + `update_config_order_map` | hooks fire here |
| 370 | exit attribute computation | always | `generate_exit_attributes` (Pool) | trade_log audit aggregated post-Pool |
| 373 | df materialization | always | `generate_order_df()` | already includes `exit_reason` |

---

## `engine/simulator.py` (READ-ONLY — protected)

Not detailed here; the drill assumes simulator behavior is correct (audited at
`fbcd36a`). Daily portfolio snapshot is built post-simulation in audit_io
helper, replaying trades against the equity curve emitted by `simulator.process`.

Relevant context only:
- `simulator.process` returns `(day_wise_log, config_order_ids, snapshot, day_wise_positions, trade_log)`.
- `trade_log` has per-trade entries (entry_epoch, exit_epoch, prices, qty, charges, exit_reason).
- `day_wise_log` has per-date `invested_value` + `margin_available` → NAV.
- Sizing: `max_order_value` (% of avg_txn or fixed), `max_positions`, `order_value_multiplier`.
- Capacity: at the per-day-rank level — orders ranked by sim config; only top-N fit.

---

## `engine/exits.py` (READ-ONLY — protected)

Decision primitives used by eod_t's `order_generator.generate_exit_attributes_for_instrument`:

- `anomalous_drop(close, last_close, threshold_pct, this_epoch)`
  → `ExitDecision(reason="anomalous_drop", exit_epoch=this_epoch, exit_price=last_close*0.8)`
  when `(last_close - close) / last_close * 100 > threshold_pct`.
- `end_of_data(this_epoch, last_epoch, close_price)`
  → `ExitDecision(reason="end_of_data", exit_epoch=this_epoch, exit_price=close_price)`
  when `this_epoch == last_epoch`.
- `trailing_stop(close, max_price, tsl_pct, next_epoch, next_open, this_epoch)`
  → `ExitDecision(reason="trailing_stop", exit_epoch=next_epoch, exit_price=next_open)`
  when `(max_price - close) / max_price * 100 > tsl_pct` and next-day data available.
- `below_min_hold(this_epoch, entry_epoch, min_hold_days)` → bool gate.
- `ExitTracker` — per (instrument, entry_epoch) per exit_config_id firing tracker.

eod_b reimplements this logic inline in `_walk_forward_tsl`. The two paths
should produce equivalent decisions for shared cases (anomalous_drop,
trailing_stop, end_of_data), differing primarily in regime_flip handling and the
min_hold semantics. Worth verifying as a Phase 3 sanity check.

---

## Audit-hook placement summary (Phase 2 plan reference)

### eod_breakout (Phase 2b)

1. **HOOK 1** — `eod_breakout.py:52` (post-scanner): emit candidate rows. ~10 lines.
2. **HOOK 2** — `eod_breakout.py:171` (entry_filter): break monolithic AND into
   per-clause expressions; emit clause flags + all_clauses_pass. ~25 lines.
3. **HOOK 3** — `eod_breakout.py:264-275` (post-walk-forward): emit per-trade
   row with at-entry context (regime_state, ds, n_day_high, rank). ~15 lines.
4. **HOOK 3a** — `eod_breakout.py:316` (`_walk_forward_tsl`): augment to return
   `reason` (audit_mode-only branch). ~10 lines.
5. **Total:** ~60 lines, all gated by `context.get("audit_mode", False)`.

### eod_technical legacy path (Phase 2c)

1. **HOOK A** — `signals/eod_technical.py:_run_no_regime`: instantiate audit
   collector, attach to context, write parquets after both subprocesses return.
   ~15 lines.
2. **HOOK B** — `scanner.py:process` (lines 134, 141-143, 161): per-clause
   reject counts. ~15 lines.
3. **HOOK C** — `order_generator.py:add_entry_signal_inplace` (lines 67-75):
   per-clause flag mirror columns. ~20 lines.
4. **HOOK D** — `order_generator.py:update_config_order_map` (line 78): emit
   all candidates (passed and failed) before filter. ~10 lines.
5. **HOOK E** — `order_generator.py:generate_exit_attributes_for_instrument`:
   collect at-entry context per trade; return alongside instrument_order_config.
   ~15 lines.
6. **HOOK F** — `order_generator.py:generate_exit_attributes`: aggregate audit
   tuples from Pool results. ~10 lines.
7. **Constraint:** when `audit_mode=True`, force `multiprocessing_workers=1` in
   `signals/eod_technical.py` before calling `order_generator.process`.
8. **Total:** ~85 lines, all gated by `context.get("audit_mode", False)`.

### Both — `lib/audit_io.py` (Phase 2a)

Pure parquet writer module. No conditional logic. Schema constants + writers
+ post-run `build_daily_snapshot(trades_df, equity_curve_df)` helper. ~150 lines
estimated.

### Regression test (Phase 2d)

`tests/test_audit_noninvasive.py`:
1. Run both champions with `audit_mode=False` against pre-Phase-2 baseline
   (`champion_pre_audit_baseline.json`); diff trades + equity_curve must be
   byte-identical.
2. Run both champions with `audit_mode=True`; diff trades + equity_curve
   against (1) — must be byte-identical (audit_mode is observation-only).

If either diff is non-zero, Phase 2 hooks have side effects → fix before
Phase 3.

---

## Open questions surfaced by the map

1. **`n_day_gain_threshold` redundant under `top_gainer` ranking in eod_b.**
   `run_scanner` doesn't apply it; ranker selects high-gain stocks anyway when
   ≥`max_positions` positive-gain candidates exist per day. Phase 3 sanity
   check: count days where filter-vs-ranker would diverge (zero/few = redundant
   in practice).

2. **Multiprocessing nondeterminism in eod_t (low practical impact).**
   `order_generator.py:335-343` documents Pool task ordering is not
   deterministic. However, `generate_order_df()` line 153 sorts by
   `[instrument, entry_epoch, exit_epoch]` which uniquely identifies each row
   in a single-config run (champion is single-config). Expected outcome:
   regression test will pass byte-identical without intervention.
   **Mitigation if needed:** force `multiprocessing_workers=1` in both
   baseline and audit runs. Apply only if the natural test fails.

3. **eod_b's `_walk_forward_tsl` doesn't currently emit exit_reason.** The
   `trade_log` reaching the simulator has all reasons collapsed to "natural"
   for eod_b. STATUS line 181 already flags this as deferred engine-level
   work. **Implication for drill:** to get accurate exit_reason in audit, the
   reason emission must be added (via audit_mode-only branch in
   `_walk_forward_tsl`). This is a hook, not a behavioral change.

4. **`run_scanner` vs `apply_liquidity_filter` divergence.** Two near-duplicate
   scanner functions exist in `signals/base.py`. eod_b uses `run_scanner`
   (lite, no n_day_gain); other strategies use `apply_liquidity_filter` (full).
   Out of scope for the drill but worth noting as code-debt observation.

---

## Phase 1 deliverable status

✅ This document is the Phase 1 deliverable.
✅ Branch enumeration complete for the 4 hook-target files.
✅ Cross-strategy asymmetries surfaced (3 hard-to-spot issues: dead n_day_gain
   config, multiprocessing constraint, missing exit_reason in eod_b).
✅ Phase 2 hook placement quantified (~60 lines for eod_b, ~85 for eod_t,
   ~150 for audit_io).

Next: Phase 2a — build `lib/audit_io.py`.

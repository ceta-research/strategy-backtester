# Strategy-Backtester Audit Checklist

**Created:** 2026-04-20
**Last updated:** 2026-04-21
**Status:** 17/17 P0 closed + 38/50 P1 closed (Phases 1-6 landed 2026-04-21). Authoritative log in `docs/AUDIT_FINDINGS.md`. Remaining work: 12 P1 + 49 P2 + 32 P3 open. Strategies requiring full re-runs: every cross-exchange LSE/HKSE/KSC/XETRA/JPX/TSX/ASX result (Phase 3 revisit). Signal-strategy re-runs deferred for Phase 5 P1 follow-ups: momentum_top_gainers / momentum_dip_quality / momentum_rebalance (user decision on when to fix known biases).
**Scope:** 24 core files (engine/ non-signals + lib/). Signals spot-checked only.

Priority tags: **P0** = known bug, must fix. **P1** = high-impact, likely bug. **P2** = medium, needs investigation. **P3** = low, hygiene/edge case.

---

## Context

Two bugs already found:
1. **Engine regression** (fixed in working tree, not committed) — top-200 cap, epoch filter, forward-fill removal. Inflated momentum_top_gainers 1.53x, earnings_dip 3.6x.
2. **CAGR formula bug** — `ppy=252` divided into calendar-day inputs (forward-filled weekends). Deflates all CAGRs by ~1/1.45. Not fixed.

Fixing the metrics formula will invalidate all historical numbers. Everything in `results/` was produced by the buggy formula.

---

## Test Harness (build FIRST, before any fix lands)

- [x] **P0** Create `tests/fixtures/synthetic_backtest.yaml` — a tiny, deterministic config with hand-computed expected outputs
- [x] **P0** Write `tests/test_metrics_fixtures.py` — hand-compute CAGR, Sharpe, Sortino, Calmar, MDD from a known equity curve (e.g. `[100, 110, 121, 100, 110, 121]`); assert metrics match
- [x] **P0** Include edge cases: all-positive returns, all-negative, zero-trade simulation, single-day simulation
- [x] **P1** Capture current numbers for all 4 known-good strategies (eod_breakout, enhanced_breakout, momentum_cascade, momentum_dip_quality). These are regression snapshots — any audit fix must document how they move. *— Landed as `tests/regression/snapshot.py` + pinned snapshots in `tests/regression/snapshots/`.*
- [ ] **P2** Add Hypothesis-style property tests: CAGR of `[1, 1, 1, ..., 1]` is 0%, Calmar is None if MDD=0, Sharpe is None if vol=0

---

## Tier 1 — Metrics Library (lib/metrics.py, lib/backtest_result.py)

### lib/metrics.py

- [x] **P0** Line 124: `years = n / ppy` — **known CAGR bug**. With forward-fill, n = calendar days, but ppy=252 (trading days). Fix: use `years = (last_epoch - first_epoch) / (86400 * 365.25)` OR change ppy to 365 when forward-fill is on OR stop forward-filling equity curve entries. Decide which.
- [x] **P0** Line 135: `vol = math.sqrt(variance) * math.sqrt(ppy)` — same ppy issue. Volatility annualization assumes trading-day returns. With calendar-day inputs + ppy=252, vol is understated by `sqrt(252/365)`.
- [x] **P0** Line 138: `sharpe = (cagr - risk_free_rate) / vol` — compound effect of the ppy bug. Both numerator and denominator are wrong by different factors.
- [x] **P1** Line 148: `downside_var = sum(downside_sq) / n` — uses `/n` (population) while `variance` on line 134 uses `/(n-1)` (sample). Inconsistent. Sortino denominator is slightly too small. *— Phase 1.1: changed to `/(n-1)`. See AUDIT_FINDINGS.md.*
- [x] **P1** Line 143: downside deviation compares `r - rf_period` where `rf_period = risk_free_rate / ppy`. If ppy is wrong, rf_period threshold is wrong. *— Phase 1.2: analysis-only. With the P0 EquityCurve fix, ppy tracks sampling frequency; rf_period is now consistent. Invariant documented in metrics.py.*
- [ ] **P2** Line 113: `dd = (cumulative - peak) / peak if peak > 0 else 0` — if peak = 0 (total wipeout) returns 0 not -1. Plausible but check expected behavior.
- [ ] **P2** Line 156-158: `var_index = max(0, int(math.ceil(n * 0.05)) - 1)` — 5th percentile index. Verify against numpy's `percentile` on a known array.
- [ ] **P2** Line 203: `max_dd_duration_periods` returns `None` if 0. Strange API — should be 0 if no drawdown. Investigate callers.
- [ ] **P3** Line 184-188: Skewness formula — uses sample-adjusted form. Verify against scipy.stats.skew(bias=False).
- [ ] **P3** Line 191-197: Excess kurtosis formula — verify against scipy.stats.kurtosis(bias=False).
- [ ] **P3** Line 268-277: Beta/alpha computation uses sample covariance. For very short series, beta is unstable. Add minimum-period guard?
- [ ] **P3** `_compute_comparison` win_rate: treats `excess == 0` as loss. Ties should probably be excluded, not counted as losses.

### lib/backtest_result.py

- [x] **P0** Line 140: `periods_per_year=252` hardcoded. Becomes wrong if equity curve uses calendar days (forward-fill) or intraday bars. Make configurable based on equity curve density.
- [ ] **P1** `_returns_from_values` (need to read): how are per-point returns computed? If equity_curve has duplicate values (weekends with no trading), returns are 0%, which deflates vol correctly ONLY if ppy matches.
- [ ] **P1** `_trade_metrics` (need to read): win rate, profit factor, avg win/loss. Verify on a hand-computed trade set.
- [ ] **P1** `_portfolio_metrics` (need to read): turnover, avg holding period, exposure. Check definitions.
- [x] **P1** Line 397-407: `_portfolio_metrics.time_in_market` broken for multi-position strategies. `days_held = sum(t["hold_days"] for t in self.trades)` aggregates across all concurrent positions — a 10-position portfolio easily exceeds calendar-days-total, so `min(days_held/total_days, 1.0)` saturates at 1.0. The metric is meaningless for any non-trivial strategy. Correct: count unique calendar days with ≥1 open position, OR average concurrent positions / max_positions. *— Phase 1.4: implemented interval-union. See AUDIT_FINDINGS.md.*
- [ ] **P1** `_monthly_returns` / `_yearly_returns`: how are months/years bucketed? Partial months at start/end handled?
- [ ] **P2** `_time_extremes`: best/worst day/month/year. Straightforward but check for empty-series handling.
- [ ] **P2** Line 186: `compact()` strips `equity_curve`, `trades`, etc. Confirm downstream consumers don't silently fail after compaction.
- [ ] **P3** `set_benchmark_values`: if benchmark length != equity length, zeros are used. Silently wrong. Should error.

### lib/data_utils.py

- [ ] **P3** Line 175 + 200: `get_prices` builds SQL via f-string interpolation of `symbols` list into `IN ({sym_list})`. If a symbol string contains `'`, `;`, or `--`, the query is malformed or allows injection. Low severity (DuckDB is local, symbols come from internal CR API), but use parameterized queries (`con.execute(sql, params)`) for hygiene.

---

## Tier 2 — Pipeline & Simulator (engine/)

### engine/pipeline.py

- [x] **P0** Line 136: signal generator is dispatched once, produces all orders up front. Verify no state leaks between config combinations in the outer loop.
- [x] **P1** Line 186: `create_config_df_loc_lookup` — in `utils.py:32-44`, entry_config_id strips `_t` suffixes for tiered strategies. Confirm this doesn't accidentally collapse real config IDs. *— Addressed as Layer 2 (OrderKey). `_t`-strip preserved at the pipeline layer (intended — groups tiers under base config); per-tier uniqueness now enforced in simulator via `OrderKey`. See `engine/utils.py:35-44` comment.*
- [x] **P1** Line 155-170 (after revert): `epoch_wise_instrument_stats` built from ALL instruments and ALL epochs in `df_tick_data`. Memory scales as O(instruments × calendar_days). For 2454 × 5915 = 14.5M entries. Is this actually a memory issue or was it over-engineered? *— Phase 2.6: profiled and confirmed 4.15 GB at full scale (~307 bytes/entry). Real issue. Refactor to numpy 2D arrays tracked as future work; expected ~18× reduction.*
- [ ] **P2** Line 145: `sanitize_orders(df_orders, max_return_mult=999.0)` — 999x return threshold effectively disables this filter. Original ATO may have had different behavior.
- [ ] **P2** Line 64-69: extracts `exchange` from first scanner_cfg only. For multi-exchange sweeps, `SweepResult` tags with one exchange. Misleading but not a result bug.

### engine/simulator.py (ported from ATO process_step.py)

- [x] **P0** Line 197-201: `end_epoch = df_orders["entry_epoch"].max()` when orders exist. So simulation stops at last entry date, not at `context["end_epoch"]`. This means positions opened late are not MTM'd to the full simulation end. Verify: if last entry is 2024-12-15, do 2024-12-16 onwards get simulated? Check against ATO.
- [x] **P0** Line 197-201: conversely, `end_epoch = context["end_epoch"]` when NO orders. Inconsistent behavior.
- [x] **P0** Line 197: `if simulation_date_epoch >= end_epoch: break` — strict `>=` means `end_epoch` itself is NOT simulated. Off-by-one?
- [x] **P1** Line 83-90: `max_order_value.percentage_of_instrument_avg_txn` uses `epoch_wise_instrument_stats[simulation_date_epoch]`. If instrument was dropped (e.g. top-200 cap on broken engine) OR if epoch missing from stats, silently uses no cap. Now engine is reverted, risk is smaller, but still: what if instrument has no tick data that day? *— Phase 2.2: added `missing_avg_txn_policy` (default "no_cap" preserves pre-fix behavior; opt-in "skip" refuses the order). Event recorded in `snapshot["missing_avg_txn_events"]` under either policy.*
- [x] **P1** Line 92: `order_quantity = int(_order_value / entry_order["entry_price"])` — integer truncation. For a 100-rupee stock and 10k rupee order, that's 100 shares exactly. For a 999-rupee stock, int(10000/999) = 10 shares = 9990 rupees. ~1% cash-drag per trade. Acceptable or should use fractional-share simulation? *— Phase 2.3: decided to keep integer. Matches real CNC/delivery semantics. Added explanatory comment.*
- [x] **P1** Line 103: `required_margin_for_entry = qty * price + charges + slippage` — slippage is applied on entry. Is exit slippage also applied? (Line 35 yes). Good. *— Verified during audit; both sides apply slippage.*
- [x] **P1** Line 275: MTM update — `if close_price:` falsy-check. If close is 0.00 (dividend adjustment), MTM isn't updated. Edge case. *— Phase 2.1: changed to `is not None`.*
- [x] **P1** Line 180: `end_epoch = df_orders["entry_epoch"].max()` — what if df_orders has `exit_epoch` > `entry_epoch.max()`? Exits after last entry won't be processed because loop breaks at entry.max. *— Fixed as part of Layer 3 end_epoch refactor: `end_epoch` now always `context["end_epoch"]`; open positions force-close at `end_of_sim_policy="close_at_mtm"`.*

Wait — that's a real concern. Verify by reading the loop carefully.

- [x] **P0** Re-read the loop termination logic. If an order has entry=2024-12-01 and exit=2025-06-01 and `end_epoch = df_orders["entry_epoch"].max() = 2024-12-01`, the exit at 2025-06-01 is never processed. Position stays open forever in the MTM calc? Or simulator breaks at 2024-12-01 and never MTMs thereafter?
- [x] **P1** Line 231-262: entries-first-then-exits by default. `exit_before_entry=True` reverses. Document which matches ATO_Simulator and which matches real broker semantics. *— Phase 2.5: documented semantics inline in simulator.py.*
- [x] **P1** Line 207-216: payout logic. If `next_payout_epoch` is before simulation_date_epoch at start (e.g. resuming from snapshot), payout runs immediately. Verify. *— Phase 2.4: fixed catch-up loop; previously silently skipped missed payouts when resuming past multiple intervals.*
- [ ] **P2** Line 219-229: `order_value` computation. Multiple types: fixed, pct of account value, pct of margin. Confirm each yields expected order sizing.
- [ ] **P3** `copy.deepcopy(current_positions)` on every MTM day — expensive for large sweeps. Profile if sweep perf matters.

### engine/utils.py

- [ ] **P1** `create_epoch_wise_instrument_stats`: forward-fill uses `range(start, end+one_day, one_day)`. If an instrument has a gap > 1 year, the fill iterates every day in that gap. Memory and time scale linearly. For 2454 instruments × max gap years, could be slow. Profile.
- [x] **P1** `avg_txn` uses `rolling_mean(window=30)` with `min_samples=1` — first 29 days use partial windows. Is this consistent with ATO? (ATO: `rolling(30, min_periods=1).mean()` — yes, matches.) *— Verified during audit; matches ATO behavior.*
- [ ] **P2** Line 32-44: `create_config_df_loc_lookup` tier-suffix stripping — hard-coded `_t` prefix. Will break if any non-tier strategy ever uses `_t` in a config ID. Fragile.

### engine/ranking.py

- [x] **P1** `sort_orders_by_highest_avg_txn`: uses `prev_volume * prev_average_price` (yesterday's values) vs ATO which uses same-day values. Look-ahead safer, but verify this was intentional. *— Phase 3 P3.1: confirmed matches ATO util.py:251-256 (prev-day for ranking) and ATO util.py:186 (same-day for stats). Cross-linked comments added.*
- [x] **P1** `sort_orders_by_highest_gainer`: rank = `(prev_close - ref_close) / ref_close` where ref = `prev_close.shift(order_ranking_window_days)`. Verify against ATO's implementation. *— Phase 3 P3.2: verified matches ATO util.py:281-283. Synthetic test pins ordering.*
- [x] **P1** `sort_orders_by_top_performer`: `remove_overlapping_orders` — iterates per-instrument groups. Verify polars `group_by` yields identical ordering to pandas (unstable without `maintain_order=True`?). *— Phase 3 P3.3: fixed with `maintain_order=True`. Champion config byte-identical; determinism regression test runs 10× on shuffled input.*
- [ ] **P2** `calculate_daywise_instrument_score`: O(entries × orders) double loop. For 486 configs × 32K orders × 2000 entry_epochs = potentially billions of iterations. Profile.
- [ ] **P2** `sort_orders_by_deepest_dip`: uses pre-computed `dip_pct` if present, else computes from tick data. Two code paths, only one tested per strategy. Verify agreement.
- [ ] **P3** All sort types use `join(how="inner")` which drops orders not in rank_df. If instrument missing from rank data (e.g. IPO in middle of simulation), order silently dropped. Should log.

### engine/charges.py

- [x] **P1** Full audit of `calculate_charges`. Must match current NSE STT (0.1% on sell-side), brokerage, GST, stamp duty, SEBI turnover fees, exchange transaction fees. Each exchange has its own fee schedule. *— Phase 3 P3.4: rate-vintage comments added naming stable vs revised constants; golden-value pin tests prevent silent drift. Dated-schedule refactor surfaced as P2 follow-up.*
- [x] **P1** Confirm US/UK/Germany/HK/etc. fee schedules exist or use a sensible default. *— Phase 3 P3.5: fallback warning added (one-time per unknown exchange). Detailed per-exchange schedules (esp. UK 0.5% stamp, HKSE 0.13%) are a P2 follow-up to avoid silently invalidating cross-exchange results.*
- [ ] **P2** Intraday vs delivery charges differ significantly. Confirm only DELIVERY is used by EOD pipelines.
- [ ] **P2** Slippage: `slippage_rate = 0.0005` default (5 bps). Is this realistic for large-cap NSE? For small-cap it's probably too low.
- [ ] **P3** Rounding of charges — paise-level precision. Check against broker contract notes.

### engine/data_provider.py

- [x] **P1** `NseChartingDataProvider.fetch_ohlcv`: prefetch_days handling. If config start=2010-01-01 and prefetch=600 days, data is fetched from 2008-05. Verify signal gen uses prefetch correctly (not as signal period). *— Phase 4 P4.1: all 32 signal files verified to trim at start_epoch (directly, via scanner, or via rebalance-date derivation). Regression test scans source files.*
- [x] **P1** Price oscillation filter (`spike_threshold`, `mild_threshold`, `min_mild_count`). When does this filter trigger? Any instruments silently dropped? Log when filter fires. *— Phase 4 P4.2: added `logger.info` summary + `logger.debug` affected-symbol list. Regression tests verify both log levels fire.*
- [x] **P1** Are corporate actions (splits, bonuses, dividends) handled? `adjClose` vs `close` — which does the simulator use? *— Phase 4 P4.3: documented in module docstring. FMP `close` is split-adjusted but NOT dividend-adjusted (long-hold strategies understate returns by yield). Kite/NSE charting both split-adjusted. Bhavcopy unadjusted (explicit warning).*
- [x] **P1** Missing data handling: if an instrument has no data for a day, does it appear in `df_tick_data` at all? Or is it filled elsewhere? *— Phase 4 P4.4: documented. Providers return absent rows for missing days; `scanner.fill_missing_dates` is the canonical gap-fill + backward-fill point.*
- [ ] **P2** `CRDataProvider.fetch_ohlcv`: memory_mb=16384 default. Verify this matches the CR API's actual memory tier.
- [ ] **P2** `BhavcopyDataProvider`: unadjusted prices. Document the difference clearly. Confirm no one accidentally uses bhavcopy for a strategy that depends on split adjustments.
- [ ] **P3** Data cache invalidation: if the parquet files are updated (new data), does the pipeline refetch? Or is stale data silently served?

### engine/config_loader.py, engine/config_sweep.py

- [ ] **P1** `create_config_iterator`: confirm that for N params each with K values, generates K^N combinations in deterministic order. Config IDs must be stable across runs.
- [ ] **P1** YAML parsing: if a param is missing from YAML, what's the fallback? Silent default or error?
- [ ] **P2** Compound params (e.g. `direction_score: [{n_day_ma: 3, score: 0.54}]`) — how are they counted in the iterator?
- [ ] **P3** Scanner instrument format: `[{exchange: NSE, symbols: []}]`. Empty symbols = all symbols. Document explicitly.

### engine/scanner.py, engine/order_generator.py

- [x] **P1** `avg_day_transaction_threshold`: same rolling-30 logic as `avg_txn` in utils.py. Confirm consistent. *— Phase 3 P3.6: confirmed same rolling-mean convention across scanner/utils/ranking. Cross-linked comments explain the prev-day (ranking) vs same-day (scanner+stats) split — matches ATO.*
- [x] **P1** `price_threshold`: filtered at what granularity — entry day only, or every day? *— Phase 3 P3.7: confirmed per-bar filter. Comment added at scanner.py:119. Regression test locks in per-bar semantics.*
- [x] **P1** Entry/exit epoch scheduling: for `max_hold_days=252`, does exit_epoch = entry_epoch + 252 calendar days or trading days? *— Phase 3 P3.8: confirmed CALENDAR days across exits.py, base.py, and 15+ signal generators. Docstring at max_hold_reached now states the unit explicitly.*
- [ ] **P2** Peak-recovery logic: `require_peak_recovery=True` — what's the peak window? Is it from entry or from pre-entry?
- [ ] **P2** Exit price computation: if using TSL exit, what exact price does the exit_price field hold? Close price on trigger day? Next day's open?

---

## Tier 3 — Signal Generators (engine/signals/, 30 files)

Spot-check approach: read 3 representative strategies cover-to-cover, then skim the rest for pattern violations.

- [x] **P1** `eod_breakout.py` — reference strategy. Must be correct. Audit fully. *— Phase 5 P5.1: clean. Next-day open entry, anomalous_drop pre-TSL priority (post-P0), custom TSL convention matches ATO.*
- [~] **P1** `momentum_top_gainers.py` — high-CAGR strategy, biggest impact if wrong. Audit fully. *— Phase 5 P5.2: flagged. Full-period turnover universe (look-ahead/survivorship bias) + scanner fallback to "1". Documented inline; left as-is for result parity. Open P1 for author review.*
- [x] **P1** `earnings_dip.py` — relies on FMP earnings data + NSE pricing. Cross-data-source audit. *— Phase 5 P5.3: clean. Post-earnings peak fully in past; MOC next-day open entry; dip-buy walk_forward_exit with require_peak_recovery=True.*
- [ ] **P2** `momentum_dip_quality.py` — was the victim of the cloud-OOM hack. Audit the `del df_signals; gc.collect()` boundary — any state leaks?
- [ ] **P2** `enhanced_breakout.py`, `momentum_cascade.py` — second-tier verification.
- [ ] **P3** Remaining 25 signals: skim for common pitfalls (look-ahead bias, divide-by-zero, inconsistent epoch arithmetic).

### Generic per-signal checks

- [~] **P1** Look-ahead bias: entry signal on day T must NOT use any data from day T's close. Must use day T-1 or prior. *— Phase 5 P5.4: swept all 32 signal gens. 29 use next-day open for entry (safe). 1 flagged (momentum_rebalance.py same-bar entry, documented as open P1). 2 (momentum_top_gainers/momentum_dip_quality) have full-period universe look-ahead, documented separately as P5.2. Regression test blocks new same-bar entries.*
- [x] **P1** Exit price: for TSL, what price triggers the stop? Close below threshold uses next-day open? Or same-day close? Consistency across signals. *— Phase 5: eod_breakout custom TSL exits at next-day open when drawdown > threshold. walk_forward_exit in base.py exits at next-day open if available, else same-day close. Consistent per-strategy convention.*
- [x] **P1** Date-range handling: `start_epoch - prefetch_days` gives warm-up window. Signals must NOT emit orders in the prefetch period. *— Phase 4 P4.1 + Phase 5: all 32 signal gens verified to filter entries to >= start_epoch.*
- [ ] **P2** Universe filter: does the filter evaluate on every day or only at rebalance? Inconsistent across signals.
- [ ] **P2** Symbol normalization: `NSE:TCS` vs `TCS.NS` — data providers differ. Check for hardcoded format assumptions.

---

## Tier 4 — Data Integrity

Separate from code audit: check the data itself.

- [x] **P1** NSE forward-fill: for each instrument, are weekends/holidays filled with previous close? Or gaps? *— Phase 4 P4.5: spot-checked 6 major NSE stocks in local kite parquet. 0 null closes, 0 duplicates, 0 unadjusted jumps. Minor finding: 1-3 weekend rows per symbol (NSE muhurat sessions).*
- [~] **P1** Corporate actions: compare `nse_charting_day` close series against a known reference (TradingView, Yahoo Finance) for 5-10 stocks that had splits/bonuses in 2015-2024. Ensure adjustment is correct. *— Phase 4 P4.6: partial. Local fixture is 2019-2021 only; no unadjusted jumps found within range. Full cross-check requires external data access (P2 follow-up).*
- [x] **P1** FMP NSE quality: the memory note says FMP's SA stocks have oscillating split factors. Check if NSE has similar issues. *— Phase 4 P4.7: `remove_price_oscillations` filter verified working on synthetic JNB-style pattern (36/50 rows correctly flagged). The filter runs on every FMP fetch as a safety net.*
- [ ] **P2** Bhavcopy vs nse_charting: same-day close prices should agree. Large discrepancies indicate adjustment differences.
- [ ] **P2** Volume and `average_price` fields: for liquid stocks (NIFTYBEES), compare against exchange-published daily volume.
- [ ] **P3** Delisted stocks: do they appear in `df_tick_data` with final-price entries? Or drop entirely after delisting? Affects survivorship bias.

---

## Tier 5 — Edge Cases & Failure Modes

- [ ] **P1** Zero-trade simulation: signal gen emits no orders. Does pipeline return empty sweep gracefully or crash?
- [ ] **P1** Single-day simulation: start=end. Metrics undefined — should error, not silently return junk.
- [ ] **P1** All-loser simulation: every trade loses. MDD = -99%, Calmar undefined. Check division-by-zero guards.
- [ ] **P1** Capital exhaustion: simulator runs out of margin. Does it halt or keep trying failed entries?
- [ ] **P2** Data with huge gaps (delisting mid-simulation): position still open when instrument disappears.
- [ ] **P2** Currency mismatch: multi-currency portfolios not supported. Ensure config rejects multi-exchange configs that would mix currencies.
- [ ] **P2** Time zone: all epochs assumed UTC. Actual NSE close is 15:30 IST = 10:00 UTC. If daily data uses "end of day UTC" vs "end of day IST", a day's data could be misaligned.
- [ ] **P3** Floating point accumulation error in long simulations: equity curve over 16 years × 5915 days. Compound multiplication error. Likely negligible but measure.

---

## Expected Audit Output

After executing this checklist:

- [ ] `docs/AUDIT_FINDINGS.md` — one entry per bug found, with file:line, impact, and fix sketch
- [ ] Updated `lib/metrics.py` and `lib/backtest_result.py` with CAGR fix
- [ ] Test suite passing on the synthetic fixture
- [ ] Decision doc on how to handle historical `results/*.json` (invalidate? recompute metrics from equity curves?)
- [ ] Updated `OPTIMIZATION_QUEUE.yaml` with all 6 completed strategies re-verified against the corrected metrics

---

## Effort Estimate (rough)

| Task | Hours |
|------|-------|
| Build test harness + synthetic fixture | 2-3 |
| Tier 1 metrics audit + fix | 3-4 |
| Tier 2 pipeline/simulator audit | 4-6 |
| Tier 3 signal spot-checks (3 strategies) | 2-3 |
| Tier 4 data integrity spot-checks | 2-3 |
| Tier 5 edge cases | 1-2 |
| Document findings + update results | 2-3 |
| **Total** | **16-24 hours** (2-3 focused sessions) |

Don't attempt in one session. Build harness first (1 session), then audit/fix by tier (2 more sessions).

---

## Additions from Second-Pass Review (2026-04-21)

Scope: broader than the original checklist — covered all 28 signal generators, `engine/charges.py`, `engine/intraday_simulator_v2.py`, `engine/signals/base.py` (walk_forward_exit), `engine/config_sweep.py`, `lib/cr_client.py`, `lib/cloud_orchestrator.py`. Each item below was **confirmed** by re-reading the specific lines cited. Items duplicating the original checklist are not repeated; items that refine an existing entry are cross-referenced.

### Tier 1 — Metrics & Result (additions)

**lib/metrics.py**

- [ ] **P2** Line 138: `sharpe = (cagr - risk_free_rate) / vol` — numerator uses CAGR (geometric), not annualized arithmetic mean excess return. Standard definitions (QuantStats, PyPortfolioOpt, textbooks) use arithmetic. CAGR is always ≤ arithmetic mean (variance drag), so this Sharpe is systematically lower than external comparisons. Methodology choice, not a bug — but document prominently or switch to arithmetic-mean-based definition.
- [ ] **P3** Line 134 (vol uses `/(n-1)` sample variance) vs Line 268-272 (beta uses population sums that cancel). Cosmetic inconsistency; beta is numerically correct, but worth aligning for clarity.

**lib/backtest_result.py**

- [x] **P1** Line 309-318: `_yearly_returns` resets the running peak at each calendar-year boundary. Example: portfolio enters 2022 at $700K after peaking at $1M in 2021, then rallies monotonically to $900K by year-end. Reported 2022 MDD = **0%**, masking that the portfolio is still ~10% below all-time peak. Fix: carry over running peak across years, OR rename the output column to "intra-year MDD" to match behavior. *— Phase 1.3: carry running peak across years. See AUDIT_FINDINGS.md.*
- [ ] **P2** Line 550-555: `SweepResult._sorted` substitutes `float("-inf")` for `None` metrics, burying configs with `calmar_ratio=None` (MDD=0 → divide by zero → None) at the bottom of the leaderboard. A genuinely zero-drawdown config is reported as the *worst*. Report `None`-metric configs as "N/A" in a separate section.
- [ ] **P3** Line 127-128 + 244+: when `len(equity_curve) < 2`, `_empty_result()` produces dict with `"costs": {}`. `print_summary` then accesses `c["total_cost"]` → KeyError. Use `.get(..., 0)` or pre-populate.
- [ ] **P3** Pipeline equity curve (`engine/pipeline.py:199-201`) feeds only `day_wise_log` entries, which start at the first MTM day, not at `start_epoch` with initial margin. `daily_returns` therefore miss the inception-to-day-1 period. Small but systematic.

### Tier 2 — Simulator & Order Generation (additions)

**engine/simulator.py**

- [x] **P0** Line 108-121: `order_id = f"{instrument}_{entry_epoch}_{exit_epoch}"` collides for tiered strategies. `quality_dip_tiered` can produce multiple orders with identical `(instrument, entry_epoch, exit_epoch)` but different tier suffixes in `entry_config_ids` (e.g. `"5_t1"`, `"5_t2"`). `utils.create_config_df_loc_lookup:38-40` strips `_t` suffix, so all tiers map to the same base entry config → all rows selected in the 3-way intersection → simulator sees duplicate `order_id` and silently overwrites positions. `max_positions_per_instrument=1` (default) accidentally masks the overwrite by blocking the second entry entirely, which means **tiered DCA is non-functional out of the box**. Fix: include tier index / raw `entry_config_ids` in `order_id`, or reject collisions explicitly.
- [ ] **P3** Line 207-215: payout `type` has only `"fixed"` / `"percentage"` branches; no `else`. A typo (`"Fixed"`, `"pct"`) silently produces `pay_out_sum = 0`. Raise `ValueError` on unknown type.
- [ ] **P3** Line 73-91 + 219-229: `order_value` is `base × order_value_multiplier`, then capped by `max_order_value`. If user intent is "2× leverage", the cap may silently truncate to < 2× for particular instruments depending on `avg_txn`. Document the interaction.

**engine/order_generator.py**

- [x] **P0** Line 212-213: anomalous-drop check uses `abs(diff_since_reference_price) > drop_threshold`. A +25% gap up (positive earnings, short squeeze) triggers the forced-exit path and sets `exit_price = last_close * 0.8` → books a ~20% loss on a day the stock actually rallied 25%. Fix: change to signed check `diff_since_reference_price < -drop_threshold`. Same bug exists in `engine/signals/eod_breakout.py:244-248`.
- [x] **P0** Line 211-224: the anomalous-drop branch does NOT call `order_exit_tracker.add(exit_config["id"])` (only the TSL branch at line 247 does). Consequence: on day N anomalous-drop records an exit; on day N+1 the TSL check for the same exit_config can fire and record a **second** exit row with a different exit_epoch. `generate_order_df` emits both; pipeline's 3-way intersection selects both; simulator creates two "entry" orders for one intended position with different `order_id`s; `max_positions_per_instrument=1` silently drops one. Which one wins is dict-iteration-order-dependent → non-deterministic exit price. Fix: `order_exit_tracker.add(exit_config["id"])` inside the anomalous-drop block.
- [x] **P1** Line 212: `diff_since_reference_price = (close_price - last_close) * 100 / last_close` — no guard against `last_close == 0`. `sanitize_orders` and `remove_price_oscillations` don't guarantee `close > 0` mid-walk-forward. `ZeroDivisionError` possible on raw data with zero-close bars. Add `if last_close > 0:` guard. *— Landed via consolidation: `engine.exits.anomalous_drop` has `if last_close is None or last_close <= 0: return None` guard. Both call sites go through it.*

**engine/signals/base.py + engine/signals/enhanced_breakout.py**

- [x] **P0** `enhanced_breakout.py:455-464` calls `walk_forward_exit(...)` without `require_peak_recovery=False`. Code sets `peak_price = entry_price` and comments say "TSL activates immediately" (line 444-447), but `walk_forward_exit` defaults to `require_peak_recovery=True` (base.py:226), so `reached_peak` stays False until `closes[j] >= entry_price`. For a breakout where the entry day closes red (`close < open = next_open = entry_price`), the flag never flips and TSL **never fires** — the position can only exit at `max_hold_days` or end of data. Compare `momentum_top_gainers.py:275` which correctly passes `require_peak_recovery=False`. Fix: add `require_peak_recovery=False` to the call.

**engine/ranking.py**

- [ ] **P3** Line 123-127: `if current_end and row["exit_epoch"] <= current_end:` — Python falsy-on-zero means if `current_end == 0` the check wrongly skips. Impossible for real epochs but still. Use `if current_end is not None`.

### Tier 2 — Charges (NEW subsection)

**engine/charges.py**

- [x] **P0** Line 36-37: `if exchange not in ("NSE", "BSE"): return round(order_value * 0.001, 2)  # 0.1% round-trip estimate`. The comment claims 0.1% round-trip, but `calculate_charges` is invoked **once per side** in `simulator.py:28-34, 94-100` (buy and sell separately). Actual round-trip cost is **0.2%**. Every backtest on LSE, JPX, HKSE, XETRA, KSC, ASX, TSX, SAO, SES, SHH, SHZ, TAI, JNB is double-charged. Fix: correct the comment ("0.1% per side") OR return `order_value * 0.0005`.
- [x] **P1** Line 40-42: `brokerage = min(order_value * 0.0003, 20)` applied unconditionally for NSE/BSE regardless of `trade_type`. But Zerodha CNC (delivery) is zero brokerage, and `EXCHANGE_BROKER_MAP` explicitly tags NSE/BSE → `BROKER_KITE` (Zerodha). NSE delivery backtests overpay ~0.06% round-trip brokerage. Note `nse_delivery_charges` helper at line 90-97 already uses 0 brokerage — but the simulator calls `calculate_charges`, not that helper. Fix: `if trade_type == "DELIVERY" and exchange in ("NSE", "BSE"): brokerage = 0`. *— Landed as part of Layer 5 charges rewrite (commit `e7db675`). NSE delivery now uses 0 brokerage via the per-side contract.*

### Tier 2 — Intraday Simulator v2 (NEW subsection)

**engine/intraday_simulator_v2.py**

- [x] **P1** Line 362-365: `fixed_stop = entry_price - atr_multiplier * entry["atr_14"]` can go **negative** if `atr_14 * atr_multiplier > entry_price` (high-vol names or unit-scale error in ATR). `price_low <= fixed_stop` is then never true → stop never fires. Silent "no-stop" mode for volatile names. Fix: `fixed_stop = max(fixed_stop, 0.01 * entry_price)` or refuse the trade. *— Phase 6.2: floored at 1% of entry_price in `_resolve_exit`.*
- [ ] **P2** Line 417-422: with `use_hilo=False` (default), stop/target exits use `bar["close"]`, not the stop/target price itself. Example: stop=95, close drops to 92 → exit at 92 (3% extra slippage). Inconsistent with `use_hilo=True` which exits exactly at stop/target. Document — or pick one model.
- [ ] **P2** Line 370: default `eod_buffer_bars = 30`. Assumes 1-minute bars (30-min buffer). For 5-minute bars this would be a 150-minute buffer, truncating most of the session. Config gotcha; document.

### Tier 2 — Scanner & Config (additions)

**engine/scanner.py**

- [ ] **P2** Line 131: `df_tick_data_original = df_tick_data_original.drop_nulls()` drops rows with ANY null. Intended for filled weekend rows (null OHLCV after `fill_missing_dates`), but also silently drops real data rows with occasional null `average_price` / `volume`. Fix: `drop_nulls(subset=["close"])` — only drop when the column that matters is null.

**engine/config_sweep.py**

- [ ] **P2** Line 17-26: if any param list is `[]` (easy YAML typo — commenting out a value leaves an empty list), `total_configs = 0`, `product(*vals)` yields nothing, and the pipeline silently produces an empty sweep. No error surfaces. Fix: `raise ValueError(f"Empty param list for {key}")` in the validation path.

### Tier 2 — Data & Cloud (additions)

**engine/data_provider.py**

- [ ] **P2** Line 853-862: `BhavcopyDataProvider._fetch_qualifying_symbols` uses `HAVING AVG(CLOSE) > price_threshold` on *unadjusted* prices. A stock that historically traded at ₹5000 and did a 100:1 split now trades at ₹50; its 1500-day average is dominated by pre-split prices, so it passes `> 50` even though current price is below threshold. Refines existing line 125 (P2). Fix: `median(CLOSE)` or last-N-day average; document limitation loudly.
- [ ] **P3** Line 317-318, 855-857, 905-906: symbol names interpolated into SQL via f-string without escaping. Any symbol containing `'` breaks the query. Low practical risk (symbols are controlled) but worth parameterizing. Related to existing P3 at line 62 (data_utils) — generalize the pattern.

**lib/cr_client.py**

- [ ] **P2** Line 184-198: `_poll` handles 429 with retry but raises immediately on any other non-200 (including 5xx). A transient 502 during a 20-minute query kills the entire run. Same pattern in `_submit` (170-182) and `_download`. Add retry with backoff on `500 <= status_code < 600`.

**lib/cloud_orchestrator.py**

- [x] **P1** Line 84, 224-233: `self._hash_cache_path = os.path.join(ROOT, ".remote_hashes.json")` — hash cache is per-working-tree, not per-project. If you switch `project_name` (e.g. `sb-remote` → `sb-eod-sweep-v2`), cached hashes from the *old* project claim files are synced, but the *new* project is empty. Subsequent sync skips uploads → run fails with ImportError on the cloud. Fix: scope the cache by `project_id` (separate file per project or a top-level dict keyed by project_id). *— Phase 6.3: nested layout `{project_name: {path: hash}}` with backward-compat migration of legacy flat files.*

### Tier 3 — Signal Generators (additions)

- [ ] **P2** `engine/signals/momentum_dip_quality.py:233`: hardcoded `avg_close > 50` filter is INR-specific (₹50 lower bound). Wouldn't apply sensibly to US (excludes most sub-$50 stocks). Derive from scanner `price_threshold` or make configurable.
- [ ] **P2** `engine/signals/earnings_dip.py:486`: `post_peak = max(pd_closes[earn_idx:peak_end + 1])` raises `TypeError` if the slice contains `None` (possible when `fill_missing_dates` filled weekend rows without backfilling close). Fix: `max((x for x in pd_closes[...] if x is not None), default=None)` + guard.
- [ ] **P3** `engine/signals/factor_composite.py:339-353`: `has_fundamentals` set is populated and then checked in `if inst in has_fundamentals and gp_raw.get(inst) is not None:` — but the second condition implies the first by construction. Dead code.
- [ ] **P3** `engine/signals/factor_composite.py:518`: `vol_scale = min(vol_target / annual_vol, 1.5)` — 1.5× leverage cap is hardcoded and undocumented. Expose as config.
- [ ] **P3** `engine/signals/earnings_dip.py:254`: `del df_tick_data` removes local reference but the pipeline caller still holds one; no memory is freed. Cosmetic.

### Tier 2 — Performance Hotspots (NEW subsection)

Not correctness bugs but dominant runtime costs in large sweeps.

- [ ] **P2** `engine/signals/base.py:206-213` (`run_scanner`): Python loop with set-membership for each UID × each scanner config. For 5M UIDs × 10 configs ≈ 50M checks per run. Rewrite as Polars join + `list.join(",")` aggregation; expected ~100× speedup.
- [ ] **P2** Per-instrument `pl.col("instrument") == inst_name` filter loops in `earnings_dip.py:410-414`, `momentum_dip_quality.py:408` (inside lazy exit_data build), and others. O(N_instruments × total_rows) full scans. Fix: single `group_by("instrument")` + materialize dict of lists (as `momentum_cascade.py:44-54` already does). Expected 10-50× speedup.
- [ ] **P2** `list.index(epoch)` lookups across many signal generators: `ibs_mean_reversion.py:110`, `squeeze.py:184`, `connors_rsi.py:160`, `swing_master.py:147`, `darvas_box.py:118`, `quality_dip_buy.py:259`, `holp_lohp.py`, `index_*` variants. O(n) per call. `bisect_left` + equality check (as `momentum_dip_quality.py:427-429` does) is O(log n). Standardize.
- [ ] **P3** `engine/utils.py:49-93` (`create_epoch_wise_instrument_stats`): refines existing P1 at line 97. The dict-based forward-fill materializes `{epoch: {instrument: {close, avg_txn}}}` for every calendar day × every instrument. For 2454 NSE symbols × 5915 calendar days = 14.5M entries, several GB of RAM. A sparse representation + binary-search lookup in the simulator MTM loop would avoid the materialization.

---

## Cross-references with original checklist

- My "order_id collision" (P0) overlaps with existing line 99 (P2, tier-suffix fragility in `utils.py`). Recommend upgrading line 99 to P0 and merging.
- My "bhavcopy AVG(CLOSE)" refines line 125 (P2, bhavcopy unadjusted).
- My "SQL injection in data_provider" generalizes line 62 (P3, data_utils only).
- Original line 99 (tier `_t` suffix) and my simulator order_id finding are two halves of the same tiered-strategy bug; fix both together.

## Items from the original checklist NOT addressed by my second pass (still need attention)

These are P0/P1 items I did not independently verify; they remain open:

- Line 14-17 (Context): CAGR/ppy forward-fill bug (P0) — likely the single largest impact issue.
- Line 78, 80, 85-89: `simulator.py` end_epoch logic (P0) — `end_epoch = df_orders["entry_epoch"].max()` may prevent exits after last entry from being processed.
- Line 84 (P1): `if close_price:` falsy-check on 0.00 close in MTM.
- Line 73 (P2): `sanitize_orders(max_return_mult=999.0)` effectively disabled.
- Tier 4 (data integrity) and Tier 5 (edge cases) — not touched by my code-level review.

---

## Further Review — Gaps, Meta-Concerns, and Recommended Audit Order (2026-04-21)

### Files not yet examined for correctness (lower-probability but unreviewed)

- [ ] **P2** `engine/intraday_pipeline.py` — spot-checked only. Audit: chunk-boundary overlap in `_date_chunks`, empty-chunk handling, `_enrich_entries_rvol_atr` filter consistency when `min_rvol > 0`.
- [ ] **P2** `engine/intraday_simulator.py` (v1) — legacy path. Confirm whether any active YAML config sets `pipeline_version: v1`; if none, deprecate to reduce surface area.
- [ ] **P3** `engine/config_loader.py` — per-strategy `build_entry_config` / `build_exit_config` dispatch. Audit YAML → internal-dict transforms for all 28 strategies; ensure defaults are sensible when keys are missing.
- [ ] **P3** `lib/data_fetchers.py` (`fetch_close`, `align`, `intersect_universes`) — spot-checked clean. Low priority.

### Confirmed bugs in previously unreviewed files

**engine/intraday_sql_builder.py**

- [ ] **P2** Line 131 (v1 only): `b.close <= LEAST(e.entry_price * stop_factor, e.or_low)`. For a long position, `LEAST` picks the lower value → further from entry → **looser** stop than user's `stop_pct`. Example: entry=100, stop_pct=0.02 → entry\*stop_factor=98; if or_low=95, effective stop=95, not 98. `intraday_simulator_v2.py:361` explicitly notes "OR low no longer used as floor" — confirming v1 was buggy and v2 fixed it. For any active v1 config, stops fire ~3-5% later than user-specified. Fix: use `GREATEST(...)` or drop or_low from the expression. Alternative: deprecate v1.

**lib/indicators.py**

- [ ] **P3** Line 39-40: `math.log(closes[j] / closes[j - 1])` inside a comprehension guards only `closes[j - 1] > 0`. If `closes[j] == 0` (bad data row), `math.log(0)` raises `ValueError: math domain error`. Guard both terms: `if closes[j] > 0 and closes[j - 1] > 0`.

### Test coverage assessment

Present test files: `test_charges.py`, `test_config_loader.py`, `test_config_sweep.py`, `test_intraday_pipeline.py`, `test_intraday_simulator.py`, `test_intraday_simulator_v2.py`, `test_intraday_sql_builder.py`, `test_known_answer.py`, `test_metrics.py`, `test_pipeline.py`, `test_scanner.py`. Plus `tests/verification/` with ATO_Simulator cross-checks.

- [x] **P1** Run the existing suite end-to-end **before any fix lands** to establish a baseline. Record which tests pass/fail/skip. *— Baseline captured; suite grew 231 → 285 during P0 work.*
- [ ] **P1** Missing test modules for critical code paths:
  - `engine/simulator.py` (the state machine — no direct test; only exercised via `test_pipeline.py`)
  - `engine/order_generator.py` (anomalous-drop, TSL, multiprocessing fan-out)
  - `engine/ranking.py` (four sort modes, overlap removal, realized/unrealized P&L)
  - `engine/signals/base.py:walk_forward_exit` (the shared TSL walker used by most generators — central to correctness, zero tests)
  - `lib/backtest_result.py` (trade metrics, yearly returns, MDD, catalog write)
  - `lib/cr_client.py`, `lib/cloud_orchestrator.py` (network code — at least unit-test the retry/backoff paths with mocks)
  - Any individual signal generator (spot-check 2-3 in `test_known_answer.py`)
- [ ] **P2** Add a regression snapshot test: run each of the 4-6 known-good strategies on a fixed tiny fixture, commit the expected JSON, fail on diff.

### Dependency & reproducibility

- [ ] **P2** `requirements.txt` uses `>=` bounds only. Polars 1.x has semantic drift between minor versions (group_by ordering, `rolling_*` NULL handling). Two users on different polars versions can get *different* backtest results for the same config. `lib/cloud_orchestrator.py:57` pins `polars==1.37.1` for cloud runs — local installs should match. Fix: pin in `requirements.txt` (`polars==1.37.1`, etc.) or use `~=`.
- [ ] **P2** Determinism of sweeps: `config_sweep.create_config_iterator` enumerates via `product(*kwargs.values())` + dict iteration order. Python 3.7+ preserves insertion order and PyYAML produces insertion-ordered dicts, so config_ids *should* be stable. Verify by running the same YAML twice and diffing config_ids to prove it.
- [ ] **P2** Polars `group_by` is unordered unless `maintain_order=True`. Audit signal generators that build parallel per-instrument dicts: grep `group_by("instrument")` and confirm no code zips two independent group_by results expecting aligned order.
- [ ] **P2** Timezone / DST: all epochs are UTC-labeled; trading days are derived via `to_timestamp(epoch)::DATE`. Memory note says minute bars are "LOCAL time labeled UTC" — so `::DATE` incidentally gives local date, which is correct for NSE/US regular hours. Confirm behavior around DST transitions (March/November in US) doesn't split single sessions across two "dates."
- [ ] **P3** `engine/order_generator.py:157`: `Pool.starmap` passes `context` dict to worker processes. Must be picklable. Confirm no lambda/closure/db-connection ends up in context across all signal generators that use the pipeline.

### Cost-model gaps (not bugs, but missing realism)

- [ ] **P2** No margin interest on leveraged positions. `order_value_multiplier > 1` is treated as free leverage; real broker charges ~10-12% annual. Strategies using leverage (check OPTIMIZATION_QUEUE for any `order_value_multiplier > 1.0`) will overstate returns.
- [ ] **P2** No dividend income. Long holds miss ~1.5-3% annual yield for dividend-paying universes. Biggest impact: `trending_value`, `low_pe`, `quality_dip_buy` (multi-month holds), `factor_composite`.
- [ ] **P3** No T+1/T+2 settlement lag. Sale proceeds available immediately. Affects capital-constrained sweeps.
- [ ] **P3** Slippage is linear (`slippage_rate * notional`). Real slippage is concave (square-root law). For ₹50K positions on liquid NSE stocks, 5bps is reasonable; for ₹5M+ positions or illiquid mid-caps, understated. Refines existing line 115.
- [ ] **P3** No short-selling infrastructure (all strategies are long-only by construction). Document explicitly so users don't assume it's supported.

### Resolution of original "need to read" items

- **Line 51** (`_returns_from_values`): Read `lib/backtest_result.py:699-707`. Simple `v[i]/v[i-1] - 1` with `v[i-1] > 0` guard (returns 0 on non-positive previous value). **No bug.**
- **Line 52** (`_trade_metrics`): Read `lib/backtest_result.py:343-395`. Standard formulas (win rate, profit factor, payoff, expectancy, Kelly, consecutive streaks). Edge case: `avg_loss == 0` (all trades win) → `payoff = None` → `kelly = None`. Correct; downstream formatters handle None.
- **Line 55** (monthly/yearly bucketing): Read `_monthly_returns:276-299` and `_yearly_returns:301-341`. Buckets by `(year, month)` / `year`, uses first/last values within each bucket, chains subsequent buckets from previous bucket's last. Partial months handled. **But** the yearly-MDD-reset bug (new Tier 1 finding) affects this path — same code.

### Recommended execution order

The stacked priority across both passes:

1. **First** — Fix CAGR/ppy (original Context + lines 35-39, P0). Invalidates all historical numbers; resolve before anything else so regression snapshots are meaningful.
2. **Second** — Fix `simulator.py` end_epoch logic (original lines 78-89, P0). Position-exit correctness depends on this.
3. **Third** — Fix `engine/charges.py` double-charge (new P0) + NSE delivery brokerage (new P1). Shifts every non-NSE/US + every NSE delivery result.
4. **Fourth** — Fix `engine/order_generator.py` anomalous-drop (new P0 ×2: signed check + tracker update).
5. **Fifth** — Fix `engine/signals/enhanced_breakout.py` peak-recovery (new P0).
6. **Sixth** — Fix `engine/simulator.py` order_id collision (new P0) → unblocks tiered strategies.
7. **Build** test harness (original line 21+) around post-fix numbers.
8. Iterate through remaining P1s.
9. Clean up P2/P3 hygiene items.

**Do not re-run `results/` historical backtests** until at least items 1-6 are fixed. All current leaderboard entries are tainted by some subset of these bugs.

---

## P1 Execution Plan (2026-04-21)

26 of the 45 open P1 items selected across 7 batches. Each batch is scoped to land as one focused commit with its own tests; batches are ordered so earlier ones de-risk later ones. The 19 unlisted P1s are either lower-impact variants, items behind an architectural decision that should wait, or verification tasks that become trivial once the corresponding batch lands.

**Guardrails:**
- Every fix must pass `tests/regression/snapshots/` unchanged, OR ship an explicit snapshot-update commit that documents the delta in `docs/AUDIT_FINDINGS.md`.
- Each batch ends with a green full-suite run (`pytest tests/`) before the next begins.
- Batches 1-2 can proceed in parallel; 3-7 should land sequentially once the metrics/simulator foundation is correct.

### Batch 1 — Metrics & result correctness — LANDED 2026-04-21 (4 of 5 items)

Impact: fixes leaderboard numbers. Blast radius: pure metric code; no trade-generation change. Migrate any affected `results_v2/*.json` via `scripts/recompute_metrics.py` after shipping.

- [x] **Line 234** `_yearly_returns` resets peak at year boundary → intra-year MDD, not true MDD. *— Phase 1.3 landed.*
- [x] **Line 54** `time_in_market` saturates at 1.0 for any multi-position strategy. *— Phase 1.4 landed (interval-union).* 
- [x] **Line 38 + 39** Sortino denominator uses `/n` while variance uses `/(n-1)`; `rf_period` threshold also depends on corrected `periods_per_year`. *— Phase 1.1 + 1.2 landed.*
- [ ] **Line 53** `_portfolio_metrics` turnover / avg holding period / exposure definitions never verified against a hand-computed trade set. Write `test_portfolio_metrics_fixtures.py` with 5 known trade sequences. *— Still open; rolls into Phase 3 follow-up.*
- [ ] **Line 237 (Tier 1 additions)** Pipeline equity curve starts at first MTM day, missing the inception-to-day-1 period. Fix: prepend `(start_epoch, start_margin)` to the curve in `pipeline.py:199-201`. *— Still open; rolls into Phase 3 follow-up.*

### Batch 2 — Simulator edge behavior — LANDED 2026-04-21 (all 4 items)

Impact: correctness under edge conditions currently silently wrong. Local to `simulator.py`.

- [x] **Line 84** `if close_price:` falsy-check on 0.00 close. *— Phase 2.1 landed (`is not None`).*
- [x] **Line 82** `order_quantity = int(...)` integer truncation. *— Phase 2.3 landed (decision: keep, matches CNC semantics; commented).*
- [x] **Line 81** `max_order_value.percentage_of_instrument_avg_txn` silent skip. *— Phase 2.2 landed (`missing_avg_txn_policy`; default `"no_cap"` preserves legacy, opt-in `"skip"` refuses order; events logged under either policy).*
- [x] **Line 91** Payout logic on resume. *— Phase 2.4 landed (catch-up loop).*

Also landed incidentally:
- [x] Phase 2.5 — entries-first-vs-exits ordering documentation
- [x] Phase 2.6 — `epoch_wise_instrument_stats` memory profile (4.15 GB at 2454×5915; refactor to 2D numpy arrays tracked as future work)

### Batches 1-2 open follow-ups (2 items)

Both items are documented above as P1 but remain open after the Phase 1+2 work. They don't block Phase 3 (they're isolated from ranking/charges/scanner concerns) but should be picked up before closing the full P1 set:

1. **`_portfolio_metrics` verification** (checklist line 53) — write hand-computed fixture tests for turnover, avg holding period, exposure.
2. **Pipeline equity curve inception point** (Tier 1 additions, checklist line 237) — prepend `(start_epoch, start_margin)` to the curve so `daily_returns[0]` captures day-1 return from the initial margin state.

### Batch 3 — Ranking verification (3 items)

Impact: trust in sort-order semantics. Currently unverified against ATO reference.

- **Line 103** `sort_by_highest_avg_txn` uses prev-day values (look-ahead safer) — verify intentional, add comment, write a test.
- **Line 104** `sort_by_highest_gainer` rank formula vs ATO. Cross-check on a known fixture.
- **Line 105** `sort_by_top_performer` + `remove_overlapping_orders` — confirm Polars `group_by` stability (`maintain_order=True` if needed). Add regression test with 2 overlapping orders that must resolve deterministically.

### Batch 4 — Charges reconciliation (2 items)

Impact: accuracy of every per-trade cost. Layer 5 fixed the structural bug; this validates the numbers.

- **Line 112** Full `calculate_charges` audit for NSE/BSE against current Zerodha contract notes (STT 0.1% sell, exchange fee, SEBI, GST, stamp duty). Add `test_charges_nse_contract_note.py` with 5 real contract-note examples.
- **Line 113** US/UK/Germany/HK/etc. fee schedules — currently 0.05% flat per-side estimate. Replace with real per-exchange rates from docs; express as a YAML-backed schedule (groundwork for the data-driven fee model from Layer 5 design).

### Batch 5 — Data provider correctness (3 items)

Impact: no silent data drops; correct handling of corporate actions.

- **Line 121** Price oscillation filter (`spike_threshold`, `mild_threshold`, `min_mild_count`) — log when it fires; emit the list of dropped instruments for the run.
- **Line 122** Corporate actions: `adjClose` vs `close` — document which the simulator uses (grep `stock_eod` consumers); add a cross-check against 3 known splits (2020-2024).
- **Line 123** Missing-data handling: if instrument has no bar for day T, does it appear in `df_tick_data`? Test with a synthetic gap fixture.

### Batch 6 — Reference signal audits (3 items)

Impact: gates trust in every derived strategy. Each is a read-and-verify task — no code change unless a bug is found.

- **Line 149** `eod_breakout.py` full cover-to-cover read: signal generation, entry price, exit price, look-ahead bias at every decision point.
- **Line 150** `momentum_top_gainers.py` full audit — high-CAGR strategy, biggest impact if wrong.
- **Line 151** `earnings_dip.py` full audit — crosses FMP + NSE data sources, extra risk.
- Deliverable per signal: one `docs/audit/signal-{name}.md` with line-by-line review notes.

### Batch 7 — Edge cases & test coverage (6 items)

Impact: hardens the harness so regressions can't silently pass.

- **Line 181** Zero-trade simulation — pipeline must return empty sweep, not crash.
- **Line 182** Single-day simulation — must error, not return junk metrics.
- **Line 183** All-loser simulation — division-by-zero guards on Calmar / Sharpe verified.
- **Line 184** Capital exhaustion — simulator halts cleanly vs retries forever.
- **Line 363 (block)** Missing direct tests for `engine/simulator.py`, `engine/order_generator.py`, `engine/ranking.py`, `engine/signals/base.py::walk_forward_exit`, `lib/backtest_result.py`. Add one `test_<module>.py` per module with at least one happy-path and one edge-case test.

### Deferred P1s (19 items, rationale)

- **Line 51, 52, 55** — resolved in "Further review" section; tick inline as part of Batch 1.
- **Line 72, 97** — memory hotspots; defer to a dedicated perf sprint that also tackles the P2 performance section.
- **Line 120, 158, 159, 160** — prefetch_days / look-ahead / TSL consistency: rolled into Batch 6 (reference signal audits cover these by reading).
- **Line 130, 131** — config determinism: ship after Batch 3 (ranking needs stable config ordering to verify).
- **Line 137, 138, 139** — scanner / order_generator semantics: rolled into Batch 6.
- **Line 170, 171, 172** — data integrity (NSE forward-fill, FMP quality): requires running queries against production data; dedicated data-integrity sprint.
- **Line 272** — `intraday_simulator_v2` negative-stop: defer until an active intraday strategy exists.
- **Line 299** — `cloud_orchestrator` hash cache scoping: defer; currently only one active project on cloud.

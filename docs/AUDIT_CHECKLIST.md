# Strategy-Backtester Audit Checklist

**Created:** 2026-04-20
**Status:** Planning — execute in a dedicated audit session
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

- [ ] **P0** Create `tests/fixtures/synthetic_backtest.yaml` — a tiny, deterministic config with hand-computed expected outputs
- [ ] **P0** Write `tests/test_metrics_fixtures.py` — hand-compute CAGR, Sharpe, Sortino, Calmar, MDD from a known equity curve (e.g. `[100, 110, 121, 100, 110, 121]`); assert metrics match
- [ ] **P0** Include edge cases: all-positive returns, all-negative, zero-trade simulation, single-day simulation
- [ ] **P1** Capture current numbers for all 4 known-good strategies (eod_breakout, enhanced_breakout, momentum_cascade, momentum_dip_quality). These are regression snapshots — any audit fix must document how they move.
- [ ] **P2** Add Hypothesis-style property tests: CAGR of `[1, 1, 1, ..., 1]` is 0%, Calmar is None if MDD=0, Sharpe is None if vol=0

---

## Tier 1 — Metrics Library (lib/metrics.py, lib/backtest_result.py)

### lib/metrics.py

- [ ] **P0** Line 124: `years = n / ppy` — **known CAGR bug**. With forward-fill, n = calendar days, but ppy=252 (trading days). Fix: use `years = (last_epoch - first_epoch) / (86400 * 365.25)` OR change ppy to 365 when forward-fill is on OR stop forward-filling equity curve entries. Decide which.
- [ ] **P0** Line 135: `vol = math.sqrt(variance) * math.sqrt(ppy)` — same ppy issue. Volatility annualization assumes trading-day returns. With calendar-day inputs + ppy=252, vol is understated by `sqrt(252/365)`.
- [ ] **P0** Line 138: `sharpe = (cagr - risk_free_rate) / vol` — compound effect of the ppy bug. Both numerator and denominator are wrong by different factors.
- [ ] **P1** Line 148: `downside_var = sum(downside_sq) / n` — uses `/n` (population) while `variance` on line 134 uses `/(n-1)` (sample). Inconsistent. Sortino denominator is slightly too small.
- [ ] **P1** Line 143: downside deviation compares `r - rf_period` where `rf_period = risk_free_rate / ppy`. If ppy is wrong, rf_period threshold is wrong.
- [ ] **P2** Line 113: `dd = (cumulative - peak) / peak if peak > 0 else 0` — if peak = 0 (total wipeout) returns 0 not -1. Plausible but check expected behavior.
- [ ] **P2** Line 156-158: `var_index = max(0, int(math.ceil(n * 0.05)) - 1)` — 5th percentile index. Verify against numpy's `percentile` on a known array.
- [ ] **P2** Line 203: `max_dd_duration_periods` returns `None` if 0. Strange API — should be 0 if no drawdown. Investigate callers.
- [ ] **P3** Line 184-188: Skewness formula — uses sample-adjusted form. Verify against scipy.stats.skew(bias=False).
- [ ] **P3** Line 191-197: Excess kurtosis formula — verify against scipy.stats.kurtosis(bias=False).
- [ ] **P3** Line 268-277: Beta/alpha computation uses sample covariance. For very short series, beta is unstable. Add minimum-period guard?
- [ ] **P3** `_compute_comparison` win_rate: treats `excess == 0` as loss. Ties should probably be excluded, not counted as losses.

### lib/backtest_result.py

- [ ] **P0** Line 140: `periods_per_year=252` hardcoded. Becomes wrong if equity curve uses calendar days (forward-fill) or intraday bars. Make configurable based on equity curve density.
- [ ] **P1** `_returns_from_values` (need to read): how are per-point returns computed? If equity_curve has duplicate values (weekends with no trading), returns are 0%, which deflates vol correctly ONLY if ppy matches.
- [ ] **P1** `_trade_metrics` (need to read): win rate, profit factor, avg win/loss. Verify on a hand-computed trade set.
- [ ] **P1** `_portfolio_metrics` (need to read): turnover, avg holding period, exposure. Check definitions.
- [ ] **P1** Line 397-407: `_portfolio_metrics.time_in_market` broken for multi-position strategies. `days_held = sum(t["hold_days"] for t in self.trades)` aggregates across all concurrent positions — a 10-position portfolio easily exceeds calendar-days-total, so `min(days_held/total_days, 1.0)` saturates at 1.0. The metric is meaningless for any non-trivial strategy. Correct: count unique calendar days with ≥1 open position, OR average concurrent positions / max_positions.
- [ ] **P1** `_monthly_returns` / `_yearly_returns`: how are months/years bucketed? Partial months at start/end handled?
- [ ] **P2** `_time_extremes`: best/worst day/month/year. Straightforward but check for empty-series handling.
- [ ] **P2** Line 186: `compact()` strips `equity_curve`, `trades`, etc. Confirm downstream consumers don't silently fail after compaction.
- [ ] **P3** `set_benchmark_values`: if benchmark length != equity length, zeros are used. Silently wrong. Should error.

### lib/data_utils.py

- [ ] **P3** Line 175 + 200: `get_prices` builds SQL via f-string interpolation of `symbols` list into `IN ({sym_list})`. If a symbol string contains `'`, `;`, or `--`, the query is malformed or allows injection. Low severity (DuckDB is local, symbols come from internal CR API), but use parameterized queries (`con.execute(sql, params)`) for hygiene.

---

## Tier 2 — Pipeline & Simulator (engine/)

### engine/pipeline.py

- [ ] **P0** Line 136: signal generator is dispatched once, produces all orders up front. Verify no state leaks between config combinations in the outer loop.
- [ ] **P1** Line 186: `create_config_df_loc_lookup` — in `utils.py:32-44`, entry_config_id strips `_t` suffixes for tiered strategies. Confirm this doesn't accidentally collapse real config IDs.
- [ ] **P1** Line 155-170 (after revert): `epoch_wise_instrument_stats` built from ALL instruments and ALL epochs in `df_tick_data`. Memory scales as O(instruments × calendar_days). For 2454 × 5915 = 14.5M entries. Is this actually a memory issue or was it over-engineered?
- [ ] **P2** Line 145: `sanitize_orders(df_orders, max_return_mult=999.0)` — 999x return threshold effectively disables this filter. Original ATO may have had different behavior.
- [ ] **P2** Line 64-69: extracts `exchange` from first scanner_cfg only. For multi-exchange sweeps, `SweepResult` tags with one exchange. Misleading but not a result bug.

### engine/simulator.py (ported from ATO process_step.py)

- [ ] **P0** Line 197-201: `end_epoch = df_orders["entry_epoch"].max()` when orders exist. So simulation stops at last entry date, not at `context["end_epoch"]`. This means positions opened late are not MTM'd to the full simulation end. Verify: if last entry is 2024-12-15, do 2024-12-16 onwards get simulated? Check against ATO.
- [ ] **P0** Line 197-201: conversely, `end_epoch = context["end_epoch"]` when NO orders. Inconsistent behavior.
- [ ] **P0** Line 197: `if simulation_date_epoch >= end_epoch: break` — strict `>=` means `end_epoch` itself is NOT simulated. Off-by-one?
- [ ] **P1** Line 83-90: `max_order_value.percentage_of_instrument_avg_txn` uses `epoch_wise_instrument_stats[simulation_date_epoch]`. If instrument was dropped (e.g. top-200 cap on broken engine) OR if epoch missing from stats, silently uses no cap. Now engine is reverted, risk is smaller, but still: what if instrument has no tick data that day?
- [ ] **P1** Line 92: `order_quantity = int(_order_value / entry_order["entry_price"])` — integer truncation. For a 100-rupee stock and 10k rupee order, that's 100 shares exactly. For a 999-rupee stock, int(10000/999) = 10 shares = 9990 rupees. ~1% cash-drag per trade. Acceptable or should use fractional-share simulation?
- [ ] **P1** Line 103: `required_margin_for_entry = qty * price + charges + slippage` — slippage is applied on entry. Is exit slippage also applied? (Line 35 yes). Good.
- [ ] **P1** Line 275: MTM update — `if close_price:` falsy-check. If close is 0.00 (dividend adjustment), MTM isn't updated. Edge case.
- [ ] **P1** Line 180: `end_epoch = df_orders["entry_epoch"].max()` — what if df_orders has `exit_epoch` > `entry_epoch.max()`? Exits after last entry won't be processed because loop breaks at entry.max.

Wait — that's a real concern. Verify by reading the loop carefully.

- [ ] **P0** Re-read the loop termination logic. If an order has entry=2024-12-01 and exit=2025-06-01 and `end_epoch = df_orders["entry_epoch"].max() = 2024-12-01`, the exit at 2025-06-01 is never processed. Position stays open forever in the MTM calc? Or simulator breaks at 2024-12-01 and never MTMs thereafter?
- [ ] **P1** Line 231-262: entries-first-then-exits by default. `exit_before_entry=True` reverses. Document which matches ATO_Simulator and which matches real broker semantics.
- [ ] **P1** Line 207-216: payout logic. If `next_payout_epoch` is before simulation_date_epoch at start (e.g. resuming from snapshot), payout runs immediately. Verify.
- [ ] **P2** Line 219-229: `order_value` computation. Multiple types: fixed, pct of account value, pct of margin. Confirm each yields expected order sizing.
- [ ] **P3** `copy.deepcopy(current_positions)` on every MTM day — expensive for large sweeps. Profile if sweep perf matters.

### engine/utils.py

- [ ] **P1** `create_epoch_wise_instrument_stats`: forward-fill uses `range(start, end+one_day, one_day)`. If an instrument has a gap > 1 year, the fill iterates every day in that gap. Memory and time scale linearly. For 2454 instruments × max gap years, could be slow. Profile.
- [ ] **P1** `avg_txn` uses `rolling_mean(window=30)` with `min_samples=1` — first 29 days use partial windows. Is this consistent with ATO? (ATO: `rolling(30, min_periods=1).mean()` — yes, matches.)
- [ ] **P2** Line 32-44: `create_config_df_loc_lookup` tier-suffix stripping — hard-coded `_t` prefix. Will break if any non-tier strategy ever uses `_t` in a config ID. Fragile.

### engine/ranking.py

- [ ] **P1** `sort_orders_by_highest_avg_txn`: uses `prev_volume * prev_average_price` (yesterday's values) vs ATO which uses same-day values. Look-ahead safer, but verify this was intentional.
- [ ] **P1** `sort_orders_by_highest_gainer`: rank = `(prev_close - ref_close) / ref_close` where ref = `prev_close.shift(order_ranking_window_days)`. Verify against ATO's implementation.
- [ ] **P1** `sort_orders_by_top_performer`: `remove_overlapping_orders` — iterates per-instrument groups. Verify polars `group_by` yields identical ordering to pandas (unstable without `maintain_order=True`?).
- [ ] **P2** `calculate_daywise_instrument_score`: O(entries × orders) double loop. For 486 configs × 32K orders × 2000 entry_epochs = potentially billions of iterations. Profile.
- [ ] **P2** `sort_orders_by_deepest_dip`: uses pre-computed `dip_pct` if present, else computes from tick data. Two code paths, only one tested per strategy. Verify agreement.
- [ ] **P3** All sort types use `join(how="inner")` which drops orders not in rank_df. If instrument missing from rank data (e.g. IPO in middle of simulation), order silently dropped. Should log.

### engine/charges.py

- [ ] **P1** Full audit of `calculate_charges`. Must match current NSE STT (0.1% on sell-side), brokerage, GST, stamp duty, SEBI turnover fees, exchange transaction fees. Each exchange has its own fee schedule.
- [ ] **P1** Confirm US/UK/Germany/HK/etc. fee schedules exist or use a sensible default.
- [ ] **P2** Intraday vs delivery charges differ significantly. Confirm only DELIVERY is used by EOD pipelines.
- [ ] **P2** Slippage: `slippage_rate = 0.0005` default (5 bps). Is this realistic for large-cap NSE? For small-cap it's probably too low.
- [ ] **P3** Rounding of charges — paise-level precision. Check against broker contract notes.

### engine/data_provider.py

- [ ] **P1** `NseChartingDataProvider.fetch_ohlcv`: prefetch_days handling. If config start=2010-01-01 and prefetch=600 days, data is fetched from 2008-05. Verify signal gen uses prefetch correctly (not as signal period).
- [ ] **P1** Price oscillation filter (`spike_threshold`, `mild_threshold`, `min_mild_count`). When does this filter trigger? Any instruments silently dropped? Log when filter fires.
- [ ] **P1** Are corporate actions (splits, bonuses, dividends) handled? `adjClose` vs `close` — which does the simulator use?
- [ ] **P1** Missing data handling: if an instrument has no data for a day, does it appear in `df_tick_data` at all? Or is it filled elsewhere?
- [ ] **P2** `CRDataProvider.fetch_ohlcv`: memory_mb=16384 default. Verify this matches the CR API's actual memory tier.
- [ ] **P2** `BhavcopyDataProvider`: unadjusted prices. Document the difference clearly. Confirm no one accidentally uses bhavcopy for a strategy that depends on split adjustments.
- [ ] **P3** Data cache invalidation: if the parquet files are updated (new data), does the pipeline refetch? Or is stale data silently served?

### engine/config_loader.py, engine/config_sweep.py

- [ ] **P1** `create_config_iterator`: confirm that for N params each with K values, generates K^N combinations in deterministic order. Config IDs must be stable across runs.
- [ ] **P1** YAML parsing: if a param is missing from YAML, what's the fallback? Silent default or error?
- [ ] **P2** Compound params (e.g. `direction_score: [{n_day_ma: 3, score: 0.54}]`) — how are they counted in the iterator?
- [ ] **P3** Scanner instrument format: `[{exchange: NSE, symbols: []}]`. Empty symbols = all symbols. Document explicitly.

### engine/scanner.py, engine/order_generator.py

- [ ] **P1** `avg_day_transaction_threshold`: same rolling-30 logic as `avg_txn` in utils.py. Confirm consistent.
- [ ] **P1** `price_threshold`: filtered at what granularity — entry day only, or every day?
- [ ] **P1** Entry/exit epoch scheduling: for `max_hold_days=252`, does exit_epoch = entry_epoch + 252 calendar days or trading days?
- [ ] **P2** Peak-recovery logic: `require_peak_recovery=True` — what's the peak window? Is it from entry or from pre-entry?
- [ ] **P2** Exit price computation: if using TSL exit, what exact price does the exit_price field hold? Close price on trigger day? Next day's open?

---

## Tier 3 — Signal Generators (engine/signals/, 30 files)

Spot-check approach: read 3 representative strategies cover-to-cover, then skim the rest for pattern violations.

- [ ] **P1** `eod_breakout.py` — reference strategy. Must be correct. Audit fully.
- [ ] **P1** `momentum_top_gainers.py` — high-CAGR strategy, biggest impact if wrong. Audit fully.
- [ ] **P1** `earnings_dip.py` — relies on FMP earnings data + NSE pricing. Cross-data-source audit.
- [ ] **P2** `momentum_dip_quality.py` — was the victim of the cloud-OOM hack. Audit the `del df_signals; gc.collect()` boundary — any state leaks?
- [ ] **P2** `enhanced_breakout.py`, `momentum_cascade.py` — second-tier verification.
- [ ] **P3** Remaining 25 signals: skim for common pitfalls (look-ahead bias, divide-by-zero, inconsistent epoch arithmetic).

### Generic per-signal checks

- [ ] **P1** Look-ahead bias: entry signal on day T must NOT use any data from day T's close. Must use day T-1 or prior.
- [ ] **P1** Exit price: for TSL, what price triggers the stop? Close below threshold uses next-day open? Or same-day close? Consistency across signals.
- [ ] **P1** Date-range handling: `start_epoch - prefetch_days` gives warm-up window. Signals must NOT emit orders in the prefetch period.
- [ ] **P2** Universe filter: does the filter evaluate on every day or only at rebalance? Inconsistent across signals.
- [ ] **P2** Symbol normalization: `NSE:TCS` vs `TCS.NS` — data providers differ. Check for hardcoded format assumptions.

---

## Tier 4 — Data Integrity

Separate from code audit: check the data itself.

- [ ] **P1** NSE forward-fill: for each instrument, are weekends/holidays filled with previous close? Or gaps?
- [ ] **P1** Corporate actions: compare `nse_charting_day` close series against a known reference (TradingView, Yahoo Finance) for 5-10 stocks that had splits/bonuses in 2015-2024. Ensure adjustment is correct.
- [ ] **P1** FMP NSE quality: the memory note says FMP's SA stocks have oscillating split factors. Check if NSE has similar issues.
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

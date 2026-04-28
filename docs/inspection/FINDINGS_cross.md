# Findings — cross-strategy notes (Phase 3e + 3f)

Per pt6 framing: **descriptive-only**, not a comparison. Each strategy
has its own success criteria; this doc surfaces parallels, divergences,
and shared issues without judging which is "better".

---

## Shared structural facts

### 1. Capacity is binding for both

| Strategy | IS sim | IS audit-pass | IS block-rate | OOS sim | OOS audit-pass | OOS block-rate |
|---|---:|---:|---:|---:|---:|---:|
| eod_breakout | 1,631 | 172,715 | 99.06% | 164 | 34,720 | 99.53% |
| eod_technical | 1,187 | 160,023 | 99.26% | 116 | 33,311 | 99.65% |

Both block 99%+ of qualifying entries. Both see OOS block-rate go
**up** from IS — universe got more permissive in 2025 but the
position cap stayed fixed.

### 2. Top-blocked instruments overlap

Large-cap names appearing in BOTH strategies' top-10-blocked-IS
lists: SBIN, DLF, LT, ABREL, LICHSGFIN, SIEMENS, AXISBANK, HINDALCO.
Half the most-blocked names are shared. These are stocks that pass
both strategies' filters near-daily and routinely lose ranking ties.

### 3. Scanner-pass drift is ~3× over the backtest

| Year-month | eod_b pass-rate | eod_t pass-rate |
|---|---:|---:|
| 2010-01 | 13.95% | (similar — same scanner) |
| 2018-02 | 23.97% | (similar) |
| 2024-09 | 44.67% | (similar) |

The two strategies share the scanner stage, so drift is identical.
This drift is the single biggest free parameter affecting both
strategies' candidate counts.

### 4. 2018 is the worst year for both

| Strategy | 2018 hit rate | 2018 mean pnl% | 2018 total |
|---|---:|---:|---:|
| eod_breakout | 25.98% | -1.80% | ₹-5.7M |
| eod_technical | 27.50% | -2.36% | ₹-3.3M |

Indian small/mid-cap correction in 2018; both trend-following
strategies got hit. Hit rate ~26-28% in both cases, well below
their long-run averages (44% / ~46%).

### 5. Direction-score distribution shifts upward in OOS

Both strategies see DS-distribution mass move into the 0.7+ buckets
in 2025+. Magnitude is larger for eod_t (35.8% IS → 57.0% OOS in
DS≥0.7) than eod_b (25.2% IS → 35.0% OOS). Likely driver: liquid
universe in 2025 contains more strongly-trending names that satisfy
the breakout criteria.

---

## Divergences

### A. exit_reason instrumentation

eod_b's signal generator does **not** propagate `exit_reason` into
`entry_order`, so `simulator_trade_log.exit_reason` in eod_b is
uniformly `"natural"`. The Phase-2b audit hook captures the real
reason. eod_t propagates correctly — both simulator and audit
exit_reason taxonomies match.

This is a tooling asymmetry: `simulator_trade_log` is reliable for
eod_t but not for eod_b. Use audit's trade_log for any analysis
that depends on exit_reason for eod_b.

### B. Filter-clause binding power

| Clause | eod_b cond_fail | eod_t cond_fail |
|---|---:|---:|
| `clause_scanner_pass` | 71.93% | 69.45% |
| `clause_ds_gt_thr` | 15.90% | **41.64%** |
| `clause_close_ge_ndhigh` | 13.92% | **31.27%** |
| `clause_close_gt_open` | 15.87% | 15.30% |
| `clause_close_gt_ma` | 15.58% | **0.13%** |
| `clause_regime_bullish` | 21.90% | (no regime gate) |

Same clause names, very different binding power. eod_t's
`close_gt_ma` is essentially redundant; eod_b's is meaningfully
selective. eod_t's `ds_gt_thr` is much more selective than eod_b's.
The strategies use the *same primitives* but tune them to capture
different patterns.

### C. Open-position bias in OOS

eod_t accumulates open positions at sim-end (`end_of_data` exit
reason): 10 OOS trades with mean pnl +11.28% vs 106 OOS
trailing_stop trades with mean pnl -0.18%. The headline OOS net pnl
is positive only because of unrealised gains on still-open
positions.

eod_b has a regime force-exit, so positions don't accumulate the
same way. eod_b's OOS mean pnl is positive on its full set of
realised trades.

### D. Audit-row volume

| Strategy | Total audit rows | All-clauses-pass | Trade-log-audit rows |
|---|---:|---:|---:|
| eod_breakout | 5,637,686 | 207,435 | 207,435 |
| eod_technical | 5,637,686 | 193,691 | 193,334 |

Identical total audit rows because both strategies run on the same
universe / date range. eod_b's all-clauses-pass count is exactly
equal to its trade-log-audit row count (1:1). eod_t's
trade-log-audit row count is slightly less than its all-clauses-pass
count (193,334 vs 193,691, difference 357) — likely audit-stage rows
where the entry-config-id muxing dedupes (eod_t's audit has
`entry_config_ids` plural).

---

## Shared open issues for Phase 4

These cut across both strategies and are worth investigating
together:

1. **Capacity-cap sensitivity.** Both strategies are 99%+
   capacity-blocked. The marginal blocked trade's expectancy drives
   whether loosening the cap helps or hurts.
2. **Scanner-threshold drift.** A 3× pass-rate shift over 15 years
   means thresholds calibrated on early data are too lax now (and
   vice versa). A time-rolling threshold (e.g. percentile-based)
   would auto-correct.
3. **2018 cross-strategy stress test.** Both lost meaningfully
   in 2018. Tracing one losing trade end-to-end through both
   strategies may reveal a regime detector that should have been
   tighter.
4. **OOS reliability.** eod_t's OOS uplift is partly an
   open-position-bias artifact. eod_b's OOS regime_flip share
   doubled. Both need an honest "OOS-with-OOS-only-regime-stats"
   rerun before any production decision.

---

## Audit-tooling cross-cuts

These are observations about the audit infrastructure itself, not
the strategies:

- `compute_filter_marginals` works correctly on both audits
  (Phase-3b C1 passes byte-identical for both).
- `scanner_snapshot.parquet` (eod_b) and
  `scanner_reject_summary.parquet` (eod_t) are different shapes
  (per-row vs per-day) but Phase-2e's
  `## Files actually written` README addendum makes that obvious.
- `entry_audit` rows are deduped on (instrument, date_epoch) for
  eod_b; eod_t has the same shape but allows multi-config-id
  entries (audit row count = 5,637,686 in both).
- `simulator_trade_log` is the **only** authoritative source for
  capacity-constrained trades; everything else is pre-capacity.
- The audit drill is reproducible: re-running
  `scripts/run_audit_drill.py --all` produces identical row counts
  and identical filter-marginal numbers (verified twice in pt9).

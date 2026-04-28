# Findings — eod_breakout (Phase 3f)

_Source: `results/eod_breakout/audit_drill_20260428T124754Z`_

Champion stats from `run_metadata.json`: CAGR 17.68%, MDD −26.75%,
Sharpe 1.18, Calmar 0.66, 1,795 simulator trades over 5,922 days.

This file is descriptive-only — no recommendations, no fixes. Phase 4
(separate session) is where improvement hypotheses get proposed.

---

## TL;DR (six lines)

1. The position cap, not the entry filter, is the binding constraint.
   207,435 audit-passing entries get culled to 1,795 simulator trades
   (99.13% blocked).
2. Hit rate is 44%; median trade loses 1.3%; mean trade gains 2.0% —
   classic right-skew. The win-tail is doing all the work.
3. exit_reason is meaningless in the simulator's own trade log
   (every row says `"natural"`). The audit hook is the authoritative
   source. Investigate before trusting any historical analysis built
   on simulator exit_reason.
4. Scanner pass-rate has drifted from ~10% in 2010 to ~40% in 2025.
   The static thresholds bind less now.
5. The scanner clause is the dominant gate
   (conditional_fail_rate 71.93%); the ds/ma/high clauses each kill
   13–22% of the surviving rows; clause `next_*_present` rejects
   nothing in this run (ETF-style edge cases not present in the
   universe).
6. 2018 was the worst year (-1.8% mean, 26% hit rate, ₹-5.7M
   total). The strategy is a small-cap-mode strategy and got hammered
   in the 2018 mid/small-cap correction.

---

## 1. Capacity is the binding constraint

| Period | Audit-passing entries | Simulator trades | Block-rate |
|---|---:|---:|---:|
| IS (2010-2024) | 172,715 | 1,631 | 99.06% |
| OOS (2025+) | 34,720 | 164 | 99.53% |

The OOS block-rate is **higher** than IS — the strategy *wants* to
take ~211× more positions per OOS day than the position cap allows
(IS ratio is ~106×). The OOS deterioration in real performance is
softened by ranking selecting a sliver of the wider OOS opportunity
set; if the cap were lifted, OOS dispersion would widen.

Top-10 most-blocked instruments (IS): SBIN, DLF, LT, ABREL, LICHSGFIN,
SIEMENS, AXISBANK, ACC, HINDALCO, ADANIPORTS — all large caps that
qualify almost daily but lose ranking ties. Top-10 OOS: dominated by
liquid ETFs (LIQUID1, LIQUIDBETF, LIQUIDPLUS) plus
CUMMINSIND/POLYCAB/SANSERA.

---

## 2. Edge is right-skewed; medians lose money

Q8 (sim, by exit_reason — note that all sim trades say `"natural"`):

| Period | Trades | Mean pnl% | Median pnl% | Hit rate | Total net pnl |
|---|---:|---:|---:|---:|---:|
| IS | 1,631 | 2.95% | -1.29% | 44.33% | ₹115.1M |
| OOS | 164 | 1.74% | -1.12% | 44.51% | ₹20.1M |

Median trade loses a fraction of a percent. Mean trade is +1.7% to
+3%. Removing the top-decile of trades would erase the year's edge
in most years — confirmed by Q9 (one or two outlier years drive
total_net_pnl: 2017 ₹13M, 2021 ₹29.7M, 2023 ₹33.9M, 2024 ₹17.5M
account for >75% of IS net pnl).

---

## 3. exit_reason in simulator is uninformative

100% of `simulator_trade_log.exit_reason` rows are `"natural"`. Reason
is in `engine/simulator.py:206` — `entry_order.get("exit_reason",
"natural")`. The eod_breakout signal generator does not populate
`entry_order["exit_reason"]`, so the simulator falls back to the
default. Sanity-check C5 surfaces this; use `trade_log_audit.parquet`
for the actual reason taxonomy (`anomalous_drop`, `regime_flip`,
`trailing_stop`).

In the audit's exit_reason taxonomy (Phase-2e finding #3 from pt9):

| Period | trailing_stop | regime_flip | anomalous_drop |
|---|---:|---:|---:|
| IS | 132,722 (76.8%) | 39,543 (22.9%) | 450 (0.3%) |
| OOS | 19,913 (57.4%) | 14,789 (42.6%) | 18 (0.05%) |

`regime_flip` share nearly doubles in 2025. Either 2025 has more
SMA-cross noise, or fewer trades are reaching their TSL before regime
fires.

---

## 4. Scanner pass-rate drifted upward

Q7 — monthly scanner pass-rate:

| Year-month | candidates | scanner_passes | pass_rate |
|---|---:|---:|---:|
| 2010-01 | 16,556 | 2,310 | **13.95%** |
| 2015-06 | 23,810 | 3,652 | 15.34% |
| 2018-02 | 25,512 | 6,114 | 23.97% |
| 2021-08 | 33,665 | 11,013 | 32.71% |
| 2024-09 | 44,295 | 19,786 | **44.67%** |
| 2026-03 | 31,819 | 12,444 | 39.11% |

Roughly **3× rise** in pass-rate over the backtest period without any
threshold change in the config. Drivers (likely): NSE universe size
grew, INR inflation, broad price-level rise. The scanner config IS
the dominant filter (conditional_fail_rate 71.93%), so this drift is
the single biggest free parameter.

---

## 5. Filter marginals — what each clause actually rejects

From `filter_marginals.parquet`:

| Clause | conditional_fail_rate (P(fail | others pass)) |
|---|---:|
| `clause_scanner_pass` | **71.93%** |
| `clause_regime_bullish` | 21.90% |
| `clause_ds_gt_thr` | 15.90% |
| `clause_close_gt_ma` | 15.58% |
| `clause_close_gt_open` | 15.87% |
| `clause_close_ge_ndhigh` | 13.92% |
| `clause_next_epoch_present` | 0.00% |
| `clause_next_open_present` | 0.00% |

The four entry clauses (`ds_gt_thr`, `close_gt_ma`, `close_gt_open`,
`close_ge_ndhigh`) all have similar binding power (~14–16%), suggesting
they capture distinct information. `regime_bullish` adds 22% on top.

---

## 6. Direction-score distribution

Audit entries (all_clauses_pass=True), 0.1-wide buckets:

| Period | DS=0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 |
|---|---:|---:|---:|---:|---:|---:|
| IS | 19.0% | 27.7% | 28.1% | 18.4% | 5.4% | 1.4% |
| OOS | 22.9% | 28.0% | 14.1% | 18.2% | 13.0% | 3.8% |

OOS shifts probability mass to the **0.8–0.9** buckets — the universe
in 2025 contained more strongly-trending names that crossed the
breakout filter. This is consistent with the scanner-rate drift: more
liquid stocks → more trending stocks pass.

---

## 7. Day-of-week

Modest skew, not significant for daily-bar EOD strategy:

| dow | IS audit % | IS sim % |
|---|---:|---:|
| Mon | 19.0% | 19.9% |
| Tue | 21.6% | 16.8% |
| Wed | 19.5% | 20.7% |
| Thu | 19.6% | 20.4% |
| Fri | 19.6% | 21.5% |
| Sat (special sessions) | 0.7% | 0.6% |

Tuesday has elevated audit-entry count but disproportionately fewer
sim trades, suggesting Tuesday entries get blocked harder by the
position cap (more candidates, same cap). Weekend rows are NSE
muhurat / special-session days.

---

## 8. Open issues for Phase 4

Tagged with `[H]` for hypothesis-worthy, `[Q]` for needs-data:

- `[H]` Loosening the scanner threshold gives the biggest delta in
  candidate count. Worth a sweep on `price_threshold` and
  `avg_txn_turnover_threshold` to see if loosening shifts CAGR up
  vs makes drawdown worse.
- `[H]` Position-cap loosening: simulate with cap +50%, +100% and
  see if the marginal trade has positive expectancy or negative.
- `[Q]` Why does the eod_b signal generator not propagate
  `exit_reason`? Plumbing bug or design choice? Audit hook handles
  it correctly; simulator's `exit_reason` is now known to be a
  fallback. Decide whether to fix or accept.
- `[Q]` 2018 underperformance: was it a regime-detection failure
  (regime_bullish let through bad trades) or a filter failure
  (entries qualified that shouldn't have)? Phase-4 trace one
  losing 2018 trade end-to-end.
- `[H]` `clause_next_*_present` is inert in this run. If we ever
  enable a no-prefetch corner of the universe (delisted stocks,
  ETF-only universes), this clause might start firing. Worth a
  test fixture.
- `[H]` OOS regime_flip share doubling in 2025 deserves a separate
  investigation — could be Nifty-50 SMA crossings have become more
  frequent or the regime rule itself is brittle in 2025-style
  volatility.

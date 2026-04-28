# Findings — eod_technical (Phase 3f)

_Source: `results/eod_technical/audit_drill_20260428T124832Z`_

Champion: 1,303 simulator trades. Same NSE charting universe, same
date range as eod_breakout.

Descriptive only. Phase-4 is the place for hypotheses.

---

## TL;DR (six lines)

1. The position cap is even more binding than for eod_b: 193,334
   audit-passing entries → 1,303 simulator trades (99.33% blocked).
2. The `close > n_day_ma` clause is **redundant** in this config
   (conditional_fail_rate 0.13%). Adding/removing it would barely
   change the entry set.
3. exit_reason here is informative — it propagates correctly from
   signal generator into entry_order. `trailing_stop` dominates
   (~99% of trades).
4. Open positions at sim-end accumulate to `end_of_data`. OOS sees
   1,904 audit-level open positions vs 31,338 trailing-stop closes,
   compared to IS 259/159,111. **Open-position bias inflates OOS
   numbers.**
5. OOS trailing_stop trades show real degradation: hit_rate drops
   from 43% → 31%, mean pnl from +4.76% → -0.18%. The OOS
   `end_of_data` bucket (still-open winners marked-to-market at
   sim end) carries the headline OOS pnl.
6. 2018 was the worst year (-2.4% mean, 28% hit rate). 2020 was the
   best (+17.8% mean, ₹21.9M total).

---

## 1. Capacity is even more binding here

| Period | Audit-passing | Sim trades | Block-rate |
|---|---:|---:|---:|
| IS (2010-2024) | 160,023 | 1,187 | 99.26% |
| OOS (2025+) | 33,311 | 116 | 99.65% |

Top-10 most-blocked instruments (IS): LICHSGFIN, DLF, ABREL,
APOLLOTYRE, LT, ADANIENT, SBIN, AXISBANK, SIEMENS, HINDALCO. The
overlap with eod_b's most-blocked list is high (SBIN, DLF, LT,
ABREL, HINDALCO, AXISBANK, SIEMENS appear on both lists) — these
are large caps that pass both strategies' filters daily and lose
ranking ties.

---

## 2. `close > n_day_ma` is essentially a no-op

`filter_marginals.parquet`:

| Clause | conditional_fail_rate |
|---|---:|
| `clause_scanner_pass` | **69.45%** |
| `clause_ds_gt_thr` | 41.64% |
| `clause_close_ge_ndhigh` | 31.27% |
| `clause_close_gt_open` | 15.30% |
| `clause_close_gt_ma` | **0.13%** |

When `close ≥ n_day_high` already passes, `close > n_day_ma` rejects
0.13% of those rows. The MA window (10-day) is shorter than the
high window (3-day) **wait — it is the other way: MA=10, high=3,
yes**. Among the rows that already cleared a 3-day high, 99.87%
also clear the 10-day MA. The MA is downstream-redundant given the
high.

This is materially different from eod_b (15.58% conditional_fail_rate
on the same clause name, but with eod_b's MA=10 / high=3 setup
behaving differently because eod_b also has the regime gate +
direction-score interplay).

---

## 3. exit_reason propagates correctly here

Q4 (sim, by exit_reason):

| Period | exit_reason | trades | hold_days_mean | hold_days_max |
|---|---|---:|---:|---:|
| IS | trailing_stop | 1,173 | 53.3 | 464 |
| IS | anomalous_drop | 11 | 472 | 3,785 |
| IS | end_of_data | 3 | 2,175 | 5,285 |
| OOS | trailing_stop | 106 | 31.3 | 221 |
| OOS | end_of_data | 10 | 105 | 349 |

`anomalous_drop` is a rare-but-real catastrophe path: 11 trades over
15 years, mean hold 472 days, max 3,785 days (one position held for
~10 years before getting flagged). p50 hold for `anomalous_drop` is
44 days but the tail is dominated by long-shaft falls.

The sanity-check (Phase 3b C5) confirms simulator and audit
exit_reason taxonomies match — sub-set check passes cleanly.

---

## 4. OOS performance — open-position bias is real

Q8 — pnl by exit_reason:

| Period | exit_reason | trades | mean pnl% | median | hit rate | total net pnl |
|---|---|---:|---:|---:|---:|---:|
| IS | trailing_stop | 1,173 | 4.76% | -2.15% | 43.22% | ₹171.9M |
| IS | end_of_data | 3 | 6.60% | 7.32% | 66.67% | ₹2.1M |
| IS | anomalous_drop | 11 | -8.27% | -13.92% | 27.27% | ₹-1.2M |
| OOS | trailing_stop | 106 | -0.18% | -6.82% | 31.13% | ₹-8.4M |
| OOS | end_of_data | 10 | 11.28% | 13.61% | 80.00% | ₹13.2M |

OOS trailing_stop is **negative** (-0.18% mean, hit rate 31%, ₹-8.4M
total). That is real OOS degradation. The headline OOS net pnl
(₹4.8M total) is positive **only** because of `end_of_data` —
positions that haven't yet hit their TSL and are marked at the last
bar. Some of these will become anomalous_drops if the strategy keeps
running.

Effective OOS realised pnl on closed trades is ₹-8.4M. Revisit OOS
reporting before claiming OOS edge.

---

## 5. Year-over-year volume + hit rate

| Year | trades | hit rate | mean pnl% | total net pnl |
|---|---:|---:|---:|---:|
| 2010 | 94 | 34% | -0.6% | ₹-722k |
| 2017 | 54 | **66.7%** | 9.6% | ₹6.2M |
| 2018 | 80 | 27.5% | **-2.4%** | ₹-3.3M |
| 2020 | 69 | 56.5% | **17.8%** | ₹21.9M |
| 2021 | 120 | 47.5% | 6.3% | ₹22.6M |
| 2022 | 121 | 44.6% | 5.3% | ₹28.3M |
| 2023 | 67 | 53.7% | 14.0% | ₹57.9M |
| 2024 | 108 | 40.7% | 2.9% | ₹29.4M |
| 2025 (OOS) | 101 | 33.7% | 0.6% | ₹1.5M |
| 2026 (OOS) | 15 | 46.7% | 2.1% | ₹3.3M |

2025 hit-rate (33.7%) is the lowest non-2018 year. Mean pnl 0.6%
is recovery-territory. Year is incomplete in 2026 partial.

---

## 6. Direction-score distribution

Audit entries (all_clauses_pass), 0.1-wide buckets:

| Period | DS=0.5 | 0.6 | 0.7 | 0.8 | 0.9 |
|---|---:|---:|---:|---:|---:|
| IS | 24.3% | **40.0%** | 25.4% | 9.4% | 1.0% |
| OOS | 13.9% | 25.0% | **37.1%** | 13.9% | 6.0% |

OOS shifts mass strongly into the 0.7+ region (57% in DS≥0.7 vs 35.8%
in IS). Same direction as eod_b but more pronounced. Universe quality
shift in 2025 is real.

---

## 7. Day-of-week

Roughly proportional to trading-calendar density. Thursday
disproportionately represented in OOS sim (58 of 116 trades = 50%
in OOS, vs 25% in IS). Likely a small-sample artifact (only 116 OOS
trades total).

---

## 8. Open issues for Phase 4

- `[H]` Drop the `close > n_day_ma` clause. Conditional fail rate
  0.13% — almost zero binding power once the n-day-high clause
  passes. Removing it should give nearly identical behaviour with
  one fewer parameter to tune. Worth a regression test.
- `[H]` Threshold for `close ≥ n_day_high`: this is doing 31% of
  the work after scanner-pass. Sweep window length (currently 3
  days) to see if 5d / 7d are more selective or less.
- `[H]` OOS open-position bias: rerun champion with the holdout cut
  shifted earlier (e.g. boundary 2024-01-01) to see what fraction
  of those OOS open trades eventually closed at trailing_stop /
  anomalous_drop.
- `[Q]` 11 IS `anomalous_drop` trades: are they all the same
  small-cap-event-day type, or is there a regime where the drop
  detector misfires? Spot-check one in `trade_log_audit.parquet`
  with hold_days >> 100.
- `[H]` Position-cap loosening: same as eod_b. The strategy wants
  ~133× more positions in IS, ~286× more in OOS. The marginal
  blocked trade is interesting — need pnl-by-rank analysis.
- `[Q]` Q9 shows 2017 hit rate 66.7% with only 54 trades — was that
  a regime where the strategy fired rarely but accurately? Or a
  small-sample artifact?

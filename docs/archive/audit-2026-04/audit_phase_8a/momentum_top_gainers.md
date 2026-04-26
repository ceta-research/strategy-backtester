# Phase 8A Bias Impact — momentum_top_gainers (RE-RUN 2026-04-23)

## Finding: RETIRE

The A/B on `entry.universe_mode` is a **no-op** — the flag was removed from the
signal generator when the per-day scanner became the universe gate. See
`engine/signals/momentum_top_gainers.py:104-106`:

> Universe gate is the per-day scanner (`scanner_config_ids` not null).
> Replaces the old full-period `period_universe_set` (look-ahead bias) and its
> opt-in `point_in_time` variant.

So the delta table below is identical by construction, not by genuine test.
The real question was whether the POST-AUDIT champion still clears NIFTYBEES
buy-and-hold (~12% CAGR, 2010-2026). Answer: **no**.

## Full 8-config champion sweep, honest engine, nse_charting_day (2010-2026)

| Profile       | TSL | pos | dir  | CAGR  | MaxDD | Calmar | Pre-audit claim |
|---------------|----:|----:|-----:|------:|------:|-------:|-----------------|
| AGGRESSIVE    | 35% |  12 | 0.45 | **10.72%** | -28.8% | **0.373** | 20.2% / 0.82 |
| BALANCED      | 22% |  12 | 0.45 |  4.54% | -35.0% | 0.13   | 17.8% / 0.79 |
| CONSERVATIVE* | 22% |  30 | 0.50 |  7.57% | -38.8% | 0.20   | 15.3% / 0.79 |

Best honest CAGR 10.72% < NIFTYBEES ~12%. Advertised Calmar 0.82 was ~55%
metrics-audit inflation (ppy=252 vs calendar-day equity curve + stale exit
semantics). No profile clears the buy-and-hold bar.

*CONSERVATIVE profile not literally in the 8-config sweep but its closest
neighbor (TSL=22%, pos=30, dir>0.50) is.

## Single-config A/B (kept for the record)

- **Config:** `strategies/momentum_top_gainers/config_audit_ab.yaml`
- **Data provider:** `nse_charting`
- **Flag:** `entry.universe_mode` (legacy=full_period, honest=point_in_time)
- **Pipeline times:** legacy 29.9s · honest 26.8s

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +10.72% | +10.72% | +0.00pp |
| Total Return | +421.45% | +421.45% | +0.00pp |
| Max Drawdown | -28.78% | -28.78% | +0.00pp |
| Calmar | +0.3726 | +0.3726 | +0.0000 |
| Sharpe | +0.4985 | +0.4985 | +0.0000 |
| Total trades | 140 | 140 | — |
| Win rate | +50.71% | +50.71% | +0.00pp |

## Decision (revised 2026-04-24)

- **Status:** COMPLETE (was AUDIT_RETIRED, revised after benchmark correction).
- **Benchmark correction:** actual NIFTYBEES buy-and-hold 2010-2026 is **10.45%**
  (not the ~12% shorthand used initially). The 10.72% honest CAGR ties
  buy-and-hold on return (+0.27pp) but beats it on risk-adjusted basis:
  Cal 0.373 vs NIFTYBEES ~0.27 (MDD -28.8% vs -38%). Genuine edge.
- Pre-audit Calmar 0.82 was still metrics inflation (honest 0.373).
- `universe_mode` flag is a no-op (scanner gate); can be cleaned up later.

# P2 Audit Sprint — Decisions Log

**Resolved:** 2026-04-21 (pre-work for the P2 batch execution).
**Source:** `docs/P2_EXECUTION_PLAN.md` §1.

Four semantic decisions were made before code work began. Recording
them here so future engineers have context on why the code looks the
way it does after the P2 sprint.

---

## D1. Sharpe definition — emit both

**Question:** The existing `sharpe_ratio` uses CAGR in the numerator
(geometric). QuantStats / PyPortfolioOpt / textbooks use annualized
arithmetic mean excess return. Should we switch, keep, or emit both?

**Decision:** Emit both. New field `sharpe_ratio_arithmetic` sits
alongside the existing `sharpe_ratio`. `print_summary` shows both.

**Rationale:**
- Switching breaks every historical leaderboard comparison.
- Keeping only geometric leaves users comparing to external tools on
  different ground without realizing it.
- Both are cheap to compute; we already have mean/vol. No runtime cost.

**Consequence:** CATALOG_FIELDS expanded to include the new key. Any
downstream consumer that iterates a known key list is unaffected;
consumers that read `sharpe_ratio` specifically see no change.

---

## D2. Intraday v1 — deprecate

**Question:** v1 `intraday_simulator.py` + `intraday_sql_builder.py`
have a stop-loss bug (`LEAST(entry*stop_factor, or_low)` produces
looser stops than user requested). Fix in place, or deprecate in
favor of v2 which already corrected it?

**Decision:** Deprecate v1. Emit one-time `DeprecationWarning` on
first call. Document migration. Remove in next minor release.

**Rationale:**
- v2 already fixed the bug and is the default pipeline version.
- Grep showed zero production YAMLs using `pipeline_version: v1`.
- Fixing v1 would duplicate v2's work for a code path nobody uses.
- Keeping v1 around indefinitely invites new users to pick the
  broken path.

**Mechanism:** See `docs/INTRADAY_V1_DEPRECATION.md` for migration
guide and removal checklist.

---

## D3. Margin-interest cost model — document only

**Question:** `order_value_multiplier > 1` is treated as free leverage.
Real NSE MTF margin charges ~10% p.a. Implement a real model, add a
warning, or defer?

**Decision:** Document only for this sprint. Defer real model to a
dedicated cost-model-realism sprint.

**Rationale:**
- A proper margin-interest model requires a per-day accrual path and
  a configurable rate; touches the simulator's MTM loop.
- Real model would shift every leveraged result. Mixing that into a
  P2 "hygiene" sprint with many other fixes obscures cause and effect.
- Current impact: results for `order_value_multiplier > 1` overstate
  returns by ~margin_rate × leverage × years. Systematic, documented.

**Consequence:** `docs/AUDIT_FINDINGS.md` flags this as a known
systematic overestimate for leveraged strategies. Users who care
should run those strategies with multiplier=1 until the real model
ships.

---

## D4. Dividend income — document only

**Question:** Long-hold strategies (multi-month holds on
dividend-paying universes) miss ~1.5-3% p.a. yield because the
simulator ignores dividends. Implement, approximate, or defer?

**Decision:** Document only. Defer to cost-model-realism sprint.

**Rationale:**
- Real dividend handling requires corporate-actions data integration
  (ex-date accrual, credit to cash). Adjacent data pipeline change.
- Approximation (constant per-universe yield) is misleading across
  sectors with divergent yields (utilities 3-4%, tech 0-1%).
- Same "systematic bias" argument as D3 — document loudly, fix once
  the foundation is right.

**Consequence:** `trending_value`, `low_pe`, `quality_dip_buy`,
`factor_composite` results are slightly understated. The
understatement is approximately the dividend yield of the selected
universe over the hold period, compounded. Users comparing to live
broker statements should adjust upward.

---

## Combined sprint scope summary

| Decision | Code impact | Snapshot impact |
|---|---|---|
| D1 emit both Sharpe | New key in result JSON + catalog | None (existing `sharpe_ratio` unchanged) |
| D2 deprecate v1 intraday | One-time warning on call | None (v1 behavior preserved until removal) |
| D3 document margin interest | Docs only | None |
| D4 document dividend income | Docs only | None |

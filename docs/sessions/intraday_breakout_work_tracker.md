# Intraday Breakout — Work Tracker

**Started:** 2026-04-29
**Machine:** swas@80.241.215.48 (prod, 251GB RAM, EPYC)
**Workspace:** /home/swas/backtester/

## Environment Setup

- [ ] Create workspace on prod
- [ ] Copy essential pipeline files
- [ ] Create local parquet data loader
- [ ] Validate: run R0 baseline on prod, compare to CR API result
- [ ] Add slippage to pipeline

## Round 0: Baseline Validation

- [x] R0 on CR API (3 months): 51.3% win rate, 2:1 RR — PASS
- [x] R0 full (5 years, CR API): CAGR 12.88%, MDD -2.32%, Calmar 5.556
- [ ] R0 on prod: should match CR API result exactly
- [ ] Add slippage, re-run R0

## Round 1: Entry/Exit Sweep

- [ ] target_pct: [0.75, 1.0, 1.5, 2.0, 2.5]
- [ ] stop_pct: [0.5, 0.75, 1.0, 1.5]
- [ ] max_entry_bar: [60, 120, 180, 240]
- [ ] max_positions: [3, 5, 8, 10]

## Round 2: Fine Grid (around R1 winners)

## Round 3: Robustness (IS/OOS, yearly)

## Round 4: Variants

- [ ] Rolling 15-30 min intraday high
- [ ] Trailing stop on minute bars
- [ ] Universe size (50 vs 100)
- [ ] Ranking effectiveness

## Approaches to Test Later

- [ ] No daily filter (pure intraday on universe)
- [ ] Regime-only (no breakout filter)
- [ ] Gap-up filter
- [ ] Volume confirmation

## Key Metrics

| Config | CAGR | MDD | Sharpe | Calmar | Trades | Notes |
|---|---|---|---|---|---|---|
| R0 baseline (CR API) | 12.88% | -2.32% | 1.231 | 5.556 | 2408 | No slippage |

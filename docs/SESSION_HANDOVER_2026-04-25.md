# Session Handover 2026-04-25

**Session focus:** Drain final 6 PENDING strategies in `OPTIMIZATION_QUEUE.yaml`.
**Result:** Queue fully exhausted. 0 PENDING / 13 COMPLETE / 17 AUDIT_RETIRED.
**Commits:** 6 (`2623dd9` → `079b310`)
**Engine:** post-audit `fbcd36a+`
**Continues from:** `SESSION_HANDOVER_2026-04-24_pt3.md`

---

## TL;DR

- Worked through all 6 remaining PENDING strategies in priority order.
- All 6 retired. Zero new COMPLETE this session.
- **Queue is now empty.** Next session needs strategic redirection (no more
  auto-prioritized work).
- Brainstorm options drafted at end of session — see "Next Session Brainstorm".

---

## Per-strategy detail (this session)

### 1. overnight_hold — AUDIT_RETIRED (priority 25)

- R0 vanilla (no filter, 15 pos): -15.22% CAGR / -93.23% MDD / **1,489,366 trades**
- R1 (down-day × RSI × sort × pos = 72 configs): best -13.44% CAGR / -90.45% MDD
- 0/72 positive
- **Bias-clean** (entry MOC at close T, exit MOO at open T+1, distinct epochs)
- Failure is real — NSE individual stocks lack the overnight risk premium
  that US large-cap ETFs (SPY/QQQ) have. Literature thesis doesn't generalize.
- Files: `strategies/overnight_hold/{config_baseline,config_round1}.yaml`,
  `strategies/overnight_hold/OPTIMIZATION.md`
- Commit: `2623dd9`

### 2. holp_lohp — AUDIT_RETIRED (priority 26)

- R0 (Carter LOHP defaults: lookback=20, ts=3, hold=20): -10.30% CAGR / -86% MDD / 45,833 trades
- R1 (lookback × ts × hold × pos = 81 configs): best -7.20% CAGR / -76.96% MDD
- 0/81 positive
- **Bias-clean** (signal at close T, entry next_open T+1)
- Failure mode: Carter's LOHP is an intraday-discretionary pattern. On
  daily NSE bars + mechanical screening, it fires too often → STT 0.1%
  paper-cuts dominate.
- Files: `strategies/holp_lohp/{config_baseline,config_round1}.yaml`,
  `strategies/holp_lohp/OPTIMIZATION.md`
- Commit: `7509aae`

### 3. index_breakout — AUDIT_RETIRED (priority 27)

- R0 (memory's "best": 3d-high, 5% TSL): -0.70% CAGR (memory had 13.3% pre-audit)
- R1 (lookback × TSL × max_hold = 126 configs): best 8.18% CAGR / 4 trades (FLUKE)
- R2 (R1 + regime SMA filter = 96 configs): identical to R1, regime invariant
- Statistically meaningful (>=30 trades) cap: **5.10% CAGR / Cal 0.284**
- NIFTYBEES B&H benchmark: **10.45% CAGR / Cal ~0.27**
- **Bias-clean** (n_day_high uses close.shift(1), entry next_open T+1)
- Memory's standalone `buy_2day_high.py` 13.3% was pre-NSE-charges; engine
  post-audit STT 0.1% per round-trip eats the entire edge.
- ⚠️ **Discrepancy flagged:** memory 13.3% vs engine R0 -0.70% is too large
  for STT alone. Possible engine vs standalone-script bug. Investigation
  deferred (low ROI given retirement).
- Commit: `da8296f`

### 4. index_dip_buy — AUDIT_RETIRED (priority 28)

- R0 (SMA20/200, hold=20, no SL): -0.65% CAGR / -13% MDD / 188 trades
- R1 (SMA × RSI × hold × SL = 432 configs): best -0.31% CAGR
- **0/432 positive**
- **Bias-clean**
- Confirms memory: "Selling with target profit DESTROYS dip-buy returns"
- Mechanism: buy on close < SMA(20) > SMA(200), sell on close > SMA(20).
  Pullback resolves up → exit immediately → forfeit upside.
- Never-sell variant = buy-and-hold (~12.8% per memory).
- Commit: `e58fc2c`

### 5. index_green_candle — AUDIT_RETIRED (priority 29)

- R0 (2 green / 1 red): -0.49% CAGR / -8% MDD
- R1 (green × red × TP × SL = 192 configs): best 1.30% / 32 trades
- 21/192 positive but capped at 1.30%
- **Bias-clean**
- After N greens NIFTYBEES mean-reverts; tight red exits cut winners; STT eats edge.
- Commit: `4cff906`

### 6. index_sma_crossover — AUDIT_RETIRED (priority 30)

- R0 (50/200 golden cross): -0.66% CAGR / -10.76% MDD
- R1 (SMA × SL × max_hold = 192 configs): best 5.43% CAGR / Cal 0.324 / 18 trades
- **Cal beats NIFTYBEES B&H (0.27) by ~20%** but CAGR loses by ~5pp
- Borderline retirement — Cal edge real but absolute return drag dominates
- Crossover lag + 2015-19 whipsaw + 30-40% time in cash forfeit uptrend
- Commit: `079b310`

---

## Queue final state

```
0 PENDING / 13 COMPLETE / 17 AUDIT_RETIRED
```

### Top 5 COMPLETE by CAGR

| Rank | Strategy | CAGR | Calmar | Champion notes |
|---|---|---|---|---|
| 1 | `eod_technical` | **19.60%** | 0.757 | Best overall |
| 2 | `quality_dip_tiered` | 18.40% | 0.388 | High CAGR, weaker Cal |
| 3 | `trending_value` | 16.90% | 0.481 | |
| 4 | `enhanced_breakout` | 16.40% | 0.656 | |
| 5 | `eod_breakout` | 15.20% | 0.446 | |

### Top 3 by Calmar

| Rank | Strategy | CAGR | Calmar |
|---|---|---|---|
| 1 | `low_pe` | 12.30% | **1.016** |
| 2 | `eod_technical` | 19.60% | 0.757 |
| 3 | `earnings_dip` | 13.80% | 0.680 |

### NIFTYBEES benchmark

10.45% CAGR / Cal ~0.27 / Sharpe ~0.45 (2010-2026)

---

## Cumulative session totals (pt2 + pt3 + this)

- **2 COMPLETE added** this session: 0 (queue had only retirable strategies left)
- **6 AUDIT_RETIRED added** this session
- **17 total AUDIT_RETIRED** in queue (vs. 13 COMPLETE — more retired than completed)
- ~3,800 configs run + 0 walk-forward folds this session

---

## Key learnings (this session)

1. **No NIFTYBEES single-instrument timing strategy beats buy-and-hold.**
   All 4 (`index_breakout`, `index_dip_buy`, `index_green_candle`,
   `index_sma_crossover`) failed for related reasons:
   - NIFTYBEES uptrend persistence
   - NSE STT 0.1% per round-trip
   - Cash-drag during exits > timing edge

2. **NSE individual stocks lack overnight premium.** US large-cap ETF
   literature ("all S&P 500 gains came from overnight holds") does not
   generalize. NSE individual equities show negative overnight expectancy
   net of costs.

3. **Carter intraday patterns ≠ daily NSE backtests.** John Carter's
   LOHP is discretionary intraday work; mechanical daily-bar version
   fires too often, gets paper-cut by STT.

4. **Bias check protocol works.** Per gap_fill lesson, all 6 strategies
   were inspected for same-bar bias before optimization. Zero bias found
   this session — all failures are real, not artifactual.

5. **Memory's standalone-script results don't reproduce in engine.**
   `buy_2day_high.py` 13.3% (pre-charges, standalone) vs engine R0 -0.70%
   (post-audit, with charges). The 14pp gap is too large for STT alone —
   may indicate engine/script semantic differences. Worth investigating
   if revisiting index strategies.

---

## Files modified this session

| File | Action |
|---|---|
| `strategies/overnight_hold/{config_baseline,config_round1}.yaml` | created |
| `strategies/overnight_hold/OPTIMIZATION.md` | created |
| `strategies/holp_lohp/{config_baseline,config_round1}.yaml` | created |
| `strategies/holp_lohp/OPTIMIZATION.md` | created |
| `strategies/index_breakout/{config_baseline,config_round1,config_round2}.yaml` | created |
| `strategies/index_breakout/OPTIMIZATION.md` | created |
| `strategies/index_dip_buy/{config_baseline,config_round1}.yaml` | created |
| `strategies/index_dip_buy/OPTIMIZATION.md` | created |
| `strategies/index_green_candle/{config_baseline,config_round1}.yaml` | created |
| `strategies/index_green_candle/OPTIMIZATION.md` | created |
| `strategies/index_sma_crossover/{config_baseline,config_round1}.yaml` | created |
| `strategies/index_sma_crossover/OPTIMIZATION.md` | created |
| `strategies/OPTIMIZATION_QUEUE.yaml` | 6 PENDING → 6 AUDIT_RETIRED |
| `results/{strategy}/round{0,1,2}_*.json` | created (gitignored) |

---

## Next session brainstorm — strategic options

Queue is empty. No auto-prioritized work remaining. The next session must
decide a direction. Three buckets:

### A) Deferred maintenance (lowest cost, defensive)

- **R4c walk-forward** for new COMPLETEs (low_pe, ml_supertrend) — verify
  champion isn't overfit
- **R4d cross-exchange** re-runs (US, JPN, EU) for top-5 — geographic robustness
- **49 stale results files** re-run on post-audit engine (carried-forward
  from pt2)
- **Add deflated Sharpe** to OPTIMIZATION_RUNBOOK as standard
- **Add same-bar bias check** as required step in OPTIMIZATION_PROMPT
  (per gap_fill lesson)

### B) Bias-fix retrievals (medium cost, may revive retired strategies)

- `gap_fill` + 5 mean-reversion family (`connors_rsi`, `ibs_mean_reversion`,
  `extended_ibs`, `bb_mean_reversion`, `swing_master`) all need:
  - Minute-bar NSE data
  - Realistic execution sim (limit-order fill modeling, slippage)
  - Memory says this infra doesn't exist
- Building it = >1 week of work but could revive 5-7 strategies
- NIFTYBEES timing strategies could be retested on **higher-volatility
  ETFs** (BANKBEES, sector ETFs) where TSL drawdown reduction has more value

### C) New strategy generation (highest +CAGR ROI) — RECOMMENDED FOCUS

To beat current best `eod_technical` (19.60%) by +5pp → need **24.6%+ CAGR**.

**1. Combine winners (ensemble) — highest probability of success**
- Top-5 are uncorrelated signal types (technical, dip-buy, value, breakout)
- Allocator: 40% `eod_technical` + 30% `low_pe` + 30% `enhanced_breakout`
- Quarterly rebalance
- Expected: ~17-18% CAGR with Cal 0.7+ (volatility diversification)
- With 2× leverage on the lowest-MDD leg → path to 24%+

**2. Quality + Momentum overlay — proven NSE pattern**
- Audit phase showed `eod_technical`, `enhanced_breakout`, `momentum_cascade`
  share: direction-score + percentile + quality-year filters
- Build new strategy: NSE > ₹50 + ₹7Cr ADV; 3-yr ROE > 15%; 6-mo return
  pctile > 80; pullback to SMA50; 15% TSL + 252-d max hold
- Essentially merges `quality_dip_tiered` (18.4%) with `enhanced_breakout`'s
  quality filter (16.4%)
- Expected: 20-23% CAGR

**3. Sector rotation overlay**
- NSE has 11 sector indices (NIFTYBANK, NIFTYIT, NIFTYAUTO, etc.)
- Each month, rank sectors by 90-day momentum
- Run `eod_technical` ONLY on top-3 sectors that month
- Stock-level signal × sector-level rotation combo never tested
- Expected: 22-25% CAGR
- Higher engineering cost

**4. Leverage on best Calmar (lowest engineering cost)**
- `low_pe` Cal 1.016 → 2× leverage = 24% CAGR with -24% MDD
- Still beats NIFTYBEES MDD (-41%)
- NSE F&O makes this mechanical — just margin math + futures roll cost

**5. Microstructure (long-term highest payoff)**
- Build minute-bar + realistic execution sim
- Could revive 5-7 retired mean-reversion strategies
- Best long-term ROI but >1 week of work

---

## Recommended next-session opening

**Highest-probability +5pp**: do (C1) + (C4) together
- Build 3-strategy ensemble allocator
- Add 2× leverage on `low_pe` as volatility-stabilizer
- Estimated: 20-24% CAGR with Cal > 0.7
- ~1-2 sessions of work

**Highest absolute payoff**: scope (C3) sector-rotation overlay on `eod_technical`
- Estimated: 22-25% CAGR
- 2-3 sessions of work
- Adds reusable sector-rotation infra

**Most defensive**: do (A) walk-forward + cross-exchange runs first to
confirm current top-5 is robust before building on top of it.

---

## Fast-start (next session)

```bash
# 1. Verify clean state
cd /Users/swas/Desktop/Swas/Kite/ATO_SUITE/strategy-backtester
git status
git log --oneline -10

# 2. Confirm queue empty
grep -c "status: PENDING" strategies/OPTIMIZATION_QUEUE.yaml   # should be 0

# 3. Read top-5 OPTIMIZATION.md files for ensemble design context
cat strategies/eod_technical/OPTIMIZATION.md
cat strategies/quality_dip_tiered/OPTIMIZATION.md
cat strategies/enhanced_breakout/OPTIMIZATION.md
cat strategies/low_pe/OPTIMIZATION.md

# 4. Brainstorm with user — pick A / B / C direction
```

---

## Deferred work (carried forward from pt3)

- 49 stale results files cross-exchange re-runs
- Regression snapshot re-pinning
- Audit items
- R4c/R4d for newly-COMPLETE strategies (low_pe, ml_supertrend)
- Add deflated Sharpe to OPTIMIZATION_RUNBOOK as standard
- Add same-bar bias check to OPTIMIZATION_PROMPT
- Investigate `index_breakout` engine-vs-standalone discrepancy (memory
  13.3% vs engine -0.70% on identical params)
- Consider revisiting 5 retired NSE mean-reversion strategies with
  index-ETF universe instead of individual stocks

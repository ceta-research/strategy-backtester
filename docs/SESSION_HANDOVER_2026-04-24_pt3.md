# Session Handover — 2026-04-24 pt3

**Continuation of pt2.** Worked through priorities 15-24 of OPTIMIZATION_QUEUE.yaml.

## TL;DR

- **2 COMPLETE** (low_pe modern-window, ml_supertrend) + **8 RETIRED** (5 mean-reversion, 2 simple breakouts, 1 bias-flagged)
- Cumulative session-day result: 6 COMPLETE + 9 RETIRED across pt2+pt3
- One critical bias finding: **gap_fill same-bar execution bias** (35.27% CAGR fictional)
- **Pattern crystallized:** simple-rule NSE strategies (mean reversion, raw breakouts) almost universally fail post-audit due to NSE STT 0.1% + falling-knife dynamics + lack of quality filters
- Queue state: **13 COMPLETE / 11 RETIRED / 6 PENDING** of 30 total

## Strategies completed this continuation (10 total)

| # | Strategy | Status | Best CAGR | Best Cal | Champion / Reason | Commit |
|---|---|---|---|---|---|---|
| 15 | low_pe | COMPLETE (modern) | 12.26% | 1.016 | pe=8, roe=0.08, de=1.0, ms=50, sl=0.10 (2018-2026) | 74841fd |
| 16 | ml_supertrend | COMPLETE (deflation caveat) | 13.20% | 0.415 | mpy=6/10, dip=3%, osc<0.25, reversal ST(20,2.5), tsl=15, hold=252 | 0e4fb7f |
| 17 | connors_rsi | RETIRED | 9.36% | 0.187 | Best CAGR < NIFTYBEES; thousands of trades | 543243c |
| 18 | ibs_mean_reversion | RETIRED | 2.90% | 0.050 | Catastrophic: -70% MDD across all configs | a728805 |
| 19 | extended_ibs | RETIRED | -3.87% | -0.065 | 0/216 positive; "extended-oversold" = continued weakness | 27c55d4 |
| 20 | bb_mean_reversion | RETIRED | 8.05% | 0.175 | Below NIFTYBEES; lower-band breaks signal weakness on NSE | 1a672c3 |
| 21 | squeeze | RETIRED | -18.91% | -0.196 | 0/108 positive; Carter strategy fails on NSE individual stocks | 7f7f4a7 |
| 22 | darvas_box | RETIRED | -11.22% | -0.131 | 0/144 positive; no quality filter (vs eod_breakout works) | 1412415 |
| 23 | swing_master | RETIRED | 2.62% | 0.061 | 7K-9K trades/config; cost-dominated | 7182303 |
| 24 | gap_fill | RETIRED for BIAS | (35.27% fictional) | (2.196 fictional) | Same-bar execution bias confirmed | bcc16cc |

## Per-strategy detail

### COMPLETE (2)

**low_pe (priority 15)** — Classic Basu (1977) value strategy
- Full 2010-2026 fails: best 7.25% CAGR < NIFTYBEES (FMP NSE fundamentals sparse pre-2018)
- Modern 2018-2026: **12.26% / MDD -12.1% / Cal 1.016 / Sharpe 1.002** (742 trades)
- OOS 2020-2026: 17.76% / Cal 1.471 (OOS > IS, robust regime)
- Walk-forward 6 folds: 5/6 positive, Mean Cal 1.72, **Std Cal 1.34 FAILS** (2018-2020 fold negative -3.23%, value drought)
- **Deflated Sharpe 0.195 below 0.3** threshold
- Same data-window caveat as `earnings_dip` and `trending_value`

**ml_supertrend (priority 16)** — Quality-dip + SuperTrend reversal
- Champion: mpy=6/10, dip=3%, osc<0.25 (near-peak), reversal ST(20,2.5), flip=10, tsl=15%, hold=252
- Full 2010-2026: **13.20% / MDD -31.8% / Cal 0.415 / Sharpe 0.653** (489 trades, ~30/yr)
- OOS 2020-2026: 23.58% / Cal 0.883 (OOS > IS, 1.79× regime-favorable)
- Walk-forward 5 folds: 5/5 positive, Mean Cal 0.753, **Std Cal 0.387 PASSES**
- **Deflated Sharpe ~0.006 below 0.3** (108 R2 configs multiple-test penalty)
- Key findings: mpy=6 loose > strict 8; near-peak mild dips (osc<0.25) > deep; reversal ST mode > trend/breakout/off; supertrend_exit=True halves CAGR

### RETIRED (8)

**connors_rsi (priority 17)** — RSI(2)<5 + SMA200 trend filter
- 270 configs across R0/R1/modern. Best full CAGR 9.36% < NIFTYBEES.
- Modern 2018-2026 best 11.85% but Cal 0.269 = NIFTYBEES (no edge)
- Thousands of trades, NSE STT dominates

**ibs_mean_reversion (priority 18)** — IBS<0.2 + SMA200
- 82 configs. R0 -5.82%/-79% MDD, best R1 only 2.90%/-70%
- IBS<0.2 catches falling knives + gap-down risk + thousands of trades

**extended_ibs (priority 19)** — IBS + extended-oversold (close < 10d_high - 2.5×ATR)
- 217 configs. **0/216 positive**. Best -3.87%/-59% MDD
- Extended-oversold predicts CONTINUED weakness on NSE, not reversion
- Worst-tied result in suite (with squeeze)

**bb_mean_reversion (priority 20)** — close < lower BB(20,2) + SMA200
- 193 configs. Best 8.05%/Cal 0.175 < NIFTYBEES. 0/164 above 10%
- Wider BBs + longer hold (400d) cap at ~8%

**squeeze (priority 21)** — Carter's BB-inside-KC + momentum > 0
- 109 configs. R0 -21.51%/-98% MDD. **0/108 R1 positive**
- Squeeze releases on NSE catch failed breakouts (operator pumps, gap risk)
- Worst-tied result in suite (with extended_ibs)

**darvas_box (priority 22)** — close > N-day box high + volume confirmation
- 145 configs. R0 -26.42%/-99% MDD. **0/144 R1 positive**
- Pure breakout without quality filter fails on NSE
- Contrast: eod_breakout works (15.20%) BECAUSE of direction-score + percentile + quality filters

**swing_master (priority 23)** — SMA uptrend + 3d pullback + Force Index divergence
- 649 configs. R0 -4.10%/-50%. Best R1 only 2.62%/Cal 0.061. 68/648 positive, 0 above 5%
- Multi-confirmation entry still fires too frequently (7K-9K trades/config)
- 4-15% SL on noisy pullbacks → cost-dominated negative expectancy

**gap_fill (priority 24)** — ⚠️ **BIAS FLAGGED** ⚠️
- R0 showed 35.27% CAGR / Cal 2.196 / Sharpe 2.10 — triggered plausibility threshold
- Confirmed structural same-bar entry bias (`avg_hold_days=0.0`)
- Cannot execute in real trading: NSE call-auction queue closes 9:08 AM, gap visible only at 9:15 AM
- Memory note predicted 15-20pp same-bar bias inflation; this case shows 30+pp because purely same-bar
- Skipped further optimization since bias affects all variants

## Queue state (post-pt3)

```
COMPLETE: 13     (eod_breakout, enhanced_breakout, momentum_cascade, momentum_top_gainers,
                  earnings_dip, quality_dip_buy, quality_dip_tiered, forced_selling_dip,
                  factor_composite, trending_value, eod_technical,
                  + low_pe + ml_supertrend [pt3])

RETIRED:  11     (momentum_dip_quality, momentum_rebalance, momentum_dip,
                  + connors_rsi, ibs_mean_reversion, extended_ibs, bb_mean_reversion,
                  squeeze, darvas_box, swing_master, gap_fill [pt3])

PENDING:   6     overnight_hold (25), holp_lohp (26), index_breakout (27),
                 index_dip_buy (28), index_green_candle (29), index_sma_crossover (30)
```

### COMPLETE strategies ranked by CAGR

| Rank | Strategy | CAGR | Cal | Sharpe | Notes |
|---|---|---|---|---|---|
| 1 | eod_technical | 19.63% | 0.757 | 1.07 | Strong regime dependency (Std Cal 0.723 FAILS) |
| 2 | quality_dip_tiered | 18.39% | 0.388 | — | DCA confirmed; 5/5 WF positive |
| 3 | trending_value | 16.89% | 0.481 | — | Std Cal 0.745 FAILS (FMP sparsity) |
| 4 | enhanced_breakout | 16.40% | 0.656 | — | Best Cal among breakouts |
| 5 | eod_breakout | 15.20% | 0.446 | — | Champion proven NSE strategy |
| 6 | factor_composite | 14.78% | 0.319 | — | 5/5 WF, deflated Sharpe ~0.32 |
| 7 | momentum_cascade | 13.75% | 0.460 | — | |
| 8 | forced_selling_dip | 13.26% | 0.431 | — | Counter-cyclical to qdb |
| 9 | **ml_supertrend** | **13.20%** | **0.415** | **0.653** | NEW pt3; deflated Sharpe ~0 caveat |
| 10 | earnings_dip | 13.80%* | 0.680* | 0.817* | *Modern 2020-2026 only |
| 11 | **low_pe** | **12.26%*** | **1.016*** | **1.002*** | NEW pt3; *modern 2018-2026 only |
| 12 | quality_dip_buy | 11.63% | 0.307 | — | Std Cal 0.530 FRAGILE |
| 13 | momentum_top_gainers | 10.72% | 0.373 | — | Just at NIFTYBEES par |

## Key learnings (pt3 additions)

### 1. NSE mean reversion is structurally non-viable post-audit (5/5 retired)
Tested 5 distinct mean-reversion strategies, all retired:
- `connors_rsi`, `ibs_mean_reversion`, `extended_ibs`, `bb_mean_reversion`, `swing_master` (pullback variant)
- Plus `momentum_dip` retired in pt2
- Cumulative ~870 configs across these 5 strategies

**Common pathology:** Hundreds-to-thousands of trades, MDDs -40% to -97%, all CAGR < NIFTYBEES.

**Root causes:**
- NSE STT 0.1% per round-trip + slippage + brokerage compounds aggressively at high turnover
- Oversold conditions on NSE individual stocks predict CONTINUED weakness more than reversion (gap-down risk, operator-driven moves, falling-knife dynamics)
- Literature claims (Connors 13%, IBS 13-40%, Carter squeeze, Larry Swing) all rely on:
  - Index-level mean reversion (creation/redemption + composition stability)
  - Close-entry bias OR no transaction costs
  - US cost structure (no STT-equivalent)

**Implication:** Stop optimizing pure mean-reversion strategies on NSE. Future research should use:
- Index ETFs (NIFTYBEES, JUNIORBEES) where buy-and-hold + tactical overlay can work
- Pair-trading or relative-value (avoids the directional momentum-up of NSE)
- Long-horizon momentum strategies (already shown to work via eod_breakout family)

### 2. Quality filters separate working from failing breakouts
- **Working:** eod_breakout (15.20%), enhanced_breakout (16.40%), momentum_cascade (13.75%) — all use direction-score + quality_year + percentile rank
- **Failing:** darvas_box (-11%), squeeze (-19%) — pure breakout rules
- Lesson: NSE breakouts are mostly noise; signal must filter for stock quality

### 3. Same-bar execution bias remains a critical risk
- gap_fill flagged via plausibility threshold (CAGR 35% > 20% trigger)
- Confirmed bias via `avg_hold_days = 0.0`
- Memory note's "15-20pp inflation" was conservative — purely same-bar strategies inflate 30+pp
- **Future intraday-style strategies should be flagged immediately for review before optimization**

### 4. Modern-window data caveat now applies to 3 strategies
- earnings_dip (2018+, FMP earnings sparse pre-2018)
- trending_value (2018+, FMP fundamentals sparse pre-2018)
- low_pe (2018+, FMP P/E/ROE sparse pre-2018)

All three: modern-window CAGR meaningfully beats NIFTYBEES, but full-period fails. Common cause: FMP NSE fundamental coverage maturity post-2018.

### 5. Deflated Sharpe is increasingly relevant
- low_pe: 0.195 (FAILS)
- ml_supertrend: ~0.006 (FAILS)
- factor_composite: ~0.317 (PASSES barely)

For strategies tested with 100+ configs, multiple-test penalty ~0.5 Sharpe. Raw Sharpe edge needs to be >1.0 for honest deflated > 0.3.

## Fast-start for next session

**Next strategy: `overnight_hold` (priority 25)**
- Likely intraday-style — **flag for same-bar bias check first** (per gap_fill lesson)
- Read `engine/signals/overnight_hold.py` and check entry/exit timing
- If similar bias to gap_fill, document and retire without optimization
- If has next-day execution semantics, run R0 baseline + R1

**Subsequent priority order:**
- 26: holp_lohp (high-of-low-period / low-of-high-period — likely intraday)
- 27-30: 4 index_* strategies (likely simpler, may benefit from buy-and-hold baseline comparison)

**Stopping criteria to consider:**
- After all 30 priorities done, may want to revisit some retired strategies with index-ETF universe (avoid stock-level falling-knife dynamics)
- Consider adding Sharpe deflation to OPTIMIZATION_RUNBOOK as standard requirement (currently only in OPTIMIZATION_PROMPT)

## Files modified/created this session (pt3)

### Strategy configs + results (10 strategies)
```
strategies/{low_pe, ml_supertrend, connors_rsi, ibs_mean_reversion,
            extended_ibs, bb_mean_reversion, squeeze, darvas_box,
            swing_master, gap_fill}/
  ├── config_baseline.yaml  (all)
  ├── config_round1.yaml or config_round1{a,b,c}.yaml  (most)
  ├── config_round2.yaml  (low_pe, ml_supertrend)
  ├── config_round3.yaml  (low_pe, ml_supertrend)
  ├── config_modern.yaml  (low_pe, connors_rsi)
  ├── config_champion.yaml  (low_pe, ml_supertrend)
  ├── config_r4_oos.yaml  (low_pe, ml_supertrend)
  └── OPTIMIZATION.md  (all)

results/{*}/round0_baseline.json + per-round result files (gitignored)
```

### Walk-forward harnesses
- `scripts/run_lp_walkforward.py` (low_pe modern-window 6-fold)
- `scripts/run_mls_walkforward.py` (ml_supertrend 5-fold standard)

### Documentation
- `strategies/OPTIMIZATION_QUEUE.yaml` — 10 status updates
- `docs/SESSION_HANDOVER_2026-04-24_pt3.md` (this file)

## Stats this session (pt3)

- **Commits:** 11 (10 strategies + this handover when committed)
- **Configs run:** ~3,200 (R0+R1+ for 10 strategies)
- **Walk-forward folds run:** 11 (6 for low_pe, 5 for ml_supertrend)
- **Strategies completed:** 2 COMPLETE + 8 RETIRED
- **Time:** ~6 hours estimated (continuous run-time excluding interpretation)

## Combined session totals (2026-04-24 pt2+pt3)

- COMPLETE added: 6 (factor_composite, quality_dip_tiered, trending_value, eod_technical [pt2]; low_pe, ml_supertrend [pt3])
- RETIRED added: 9 (momentum_dip [pt2]; connors_rsi, ibs_mean_reversion, extended_ibs, bb_mean_reversion, squeeze, darvas_box, swing_master, gap_fill [pt3])
- Total configs run across pt2+pt3: ~5,700
- Status: 13 COMPLETE + 11 RETIRED + 6 PENDING (vs starting 7 COMPLETE + 2 RETIRED + 21 PENDING)

## Deferred work (carried from pt2 + new)

- 49 stale results files need cross-exchange re-runs (carried)
- Regression snapshot re-pinning (carried)
- 1 P1 + 2 P2 + 6 P3 audit items (carried)
- Doc cleanup: retire SESSION_PENDING_WORK.md, update runbook thresholds (carried)
- R4c/R4d for now 6 new COMPLETE strategies (carried)
- **NEW:** Add deflated Sharpe to OPTIMIZATION_RUNBOOK as standard requirement
- **NEW:** Add same-bar bias plausibility check to OPTIMIZATION_PROMPT (gap_fill lesson)
- **NEW:** Consider revisiting 5 retired NSE mean-reversion strategies with index-ETF universe

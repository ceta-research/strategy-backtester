# Intraday Breakout — STATUS & Tracking

**Created:** 2026-04-29
**Status:** ⚠️ AUDIT FAILED 2026-05-01 — STRATEGY DEAD AS BUILT
**Parent strategy:** eod_breakout (IR hysteresis, Calmar 1.350)
**Prod workspace:** `swas@80.241.215.48:/home/swas/backtester/`
**Prod runner:** `intraday_breakout_prod.py`

---

## ⚠️ AUDIT RESULT (2026-05-01)

The pre-audit results below (24% CAGR / Calmar 29.8 / etc.) were **artifacts of two bugs**:

1. **Entry price for gap-ups** — code filled at `prior_high` for stocks that opened above `prior_high`. You can't physically buy below the open.
2. **Same-bar entry+exit** — exit loop started at `range(entry_idx, ...)` so the entry bar's high/low could trigger target/stop on the same minute as entry.

**After fixing both** (target=1.0 / stop=0.5 / max_pos=5 / entry=15 bars / gap-up only):

| Metric | PRE-FIX | POST-FIX | Delta |
|---|---:|---:|---:|
| CAGR (0bps) | +24.14% | **−2.18%** | −26.3pp |
| MDD (0bps) | −0.81% | **−10.47%** | −9.7pp |
| Calmar (0bps) | 29.84 | **−0.21** | −30.0 |
| Win rate (0bps) | 58% | **36%** | −22pp |
| CAGR (3bps) | +19.81% | **−6.45%** | −26.3pp |
| MDD (3bps) | −0.86% | **−23.64%** | −22.8pp |

Decision gate (CAGR > 5% at 0 slip) failed. **All sweep results below — including the R2 "champion" target=1.2/stop=0.25 and the leverage projections — are invalidated.**

Audit artifacts:
- `tuning/audit_baseline.log` (full run output)
- `tuning/audit_baseline_result.json` (numbers)
- `tuning/run_audit_baseline.py` (reproducer)
- Pre-audit code preserved on prod: `intraday_breakout_prod.py.pre_audit`
- Pre-audit log files preserved on prod: `*.log.pre_audit`

**The remaining 6 audit stages (3–8) were not run** because Stage 1+2 already conclusively killed the strategy.

---

## ⚠️ PURE ORB EXTENSION (2026-05-01) — also dead

Tested whether a *cleaner* intraday breakout (no daily data, no cross-day-mismatch bug surface) had any edge. Setup:

- Universe = top 50 by trailing 30-day minute turnover, monthly rebalance
- Setup = 15-min opening range high
- Entry = first post-OR bar with high > OR-high, fill at `max(OR_high, bar_open) * (1+slip)`
- Exit = target 1% / stop 0.5% / EOD 15:25, exit loop from `entry_idx + 1`
- No regime, no daily data, single-day positions
- Period 2022–2025

**Result: NO EDGE in any year.** Loses money every year, including strongest bull year (2024 = −17.2%):

| Year | Trades | PnL %initial | WR |
|---|---:|---:|---:|
| 2022 | 211 | −4.3% | 31% |
| 2023 | 1,215 | −5.4% | 37% |
| 2024 | 1,224 | **−17.2%** | 33% |
| 2025 | 1,225 | −11.1% | 35% |
| Overall (0bps) | 3,875 | CAGR=−11.3%, MDD=−38.9%, Calmar=−0.29 |
| Overall (3bps) | 3,875 | CAGR=−21.8%, MDD=−62.6% |

Stops hit ~2x more than targets (2,344 vs 1,133) — same structural asymmetry as the gap-up version. **2024 (strongest bull) was the worst year**, which is the smoking gun: trending markets generate more failed breakouts (mean-reversion overwhelms continuation), not fewer.

Artifacts:
- `tuning/run_pure_orb_phase_a.py` — reproducer
- `tuning/pure_orb_phase_a.{log,json}` — output

**Conclusion across both audits:** any intraday strategy whose entry rule is "buy after price crosses level X" appears to have no inherent edge on NSE minute data 2022–2025, regardless of how X is defined (prior-day high, opening-range high). The "alpha" reported in the original session was bug-driven. Next direction (pair trading / mean-reversion) tracked separately.

---

## Phase B — Momentum-filtered universe (in progress, 2026-05-01)

**Hypothesis:** the failure mode of pure ORB may be universe selection, not entry mechanism. Filtering to stocks in a confirmed daily uptrend ("bull-run only") might capture continuation behavior that random-universe breakouts miss. Directly motivated by Jegadeesh-Titman momentum effect (well-replicated since 1993).

**Setup:**
- Universe: top 15 by trailing 20-day return, weekly rebalance, with filters (>10d SMA, >50d SMA, 5d return > 0 — the "not slowing down" anti-filter)
- Same intraday execution as Pure ORB (15-min OR, breakout entry, target/stop/EOD)
- Wider target/stop (1.5% / 0.7%) to give momentum room to continue
- Period 2022-2025, 0bps + 3bps slippage
- All bug fixes carried over

**Decision gate:** CAGR > 5% at 0bps → momentum filter is materially helping. Else: confirms entry-mechanism is dead regardless of universe.

Artifacts: `tuning/run_phase_b_momentum_filter.py`, `tuning/phase_b_momentum.{log,json}`

**Result: hypothesis CONFIRMED — momentum filter helps materially, but absolute edge is still marginal.**

| Year | Pure ORB (Phase A) | Phase B (mom filter) | Δ |
|---|---:|---:|---:|
| 2022 | −4.3% | +4.3% | +8.6pp |
| 2023 | −5.4% | **+26.6%** | +32pp |
| 2024 | −17.2% | −2.2% | +15pp |
| 2025 | −11.1% | +5.3% | +16pp |
| **Overall 0bps** | **CAGR −11.3% / MDD −38.9%** | **CAGR +7.6% / MDD −13.5%** | +19pp |
| Overall 3bps | −21.8% | −2.8% | +19pp |

Filter consistently lifts every year by 8-32pp. Sharpe 1.33, Calmar 0.56 at 0bps. WR unchanged (~37%); the filter doesn't change *frequency* of wins, it changes the *expected value* per trade — winners on uptrending stocks run further before mean-reverting back to stop.

**Limitations:**
- Slippage destroys it: −2.8% CAGR at 3bps. Real-world execution would need limit orders or tighter spreads.
- 2023 dominates (+26.6%). Other years are +4-5%. Edge has temporal concentration risk.
- 2024 (strongest bull year) is still a slight loss — momentum filter helps but doesn't fully fix the "everything-up bull market = noisy breakouts" problem.

**Phase B verdict (REVISED 2026-05-01 after survivorship check):** the +7.6% CAGR was almost entirely survivorship bias.

### Survivorship verification (Nifty 50 / Nifty 100 re-test)

NSE daily dataset is survivorship-only (zero stocks present in 2022 are missing from 2026 — all delisted companies are absent). Re-ran Phase B with universe restricted to current Nifty 50 and Nifty 100 members (large-caps almost never delist):

| Variant | Slip | CAGR | MDD | WR |
|---|---:|---:|---:|---:|
| Phase B (broad, biased) | 0bps | +7.6% | −13.5% | 37.4% |
| Phase B (broad, biased) | 3bps | −2.8% | −25.9% | 36.0% |
| **Nifty 50 (clean)** | **0bps** | **−7.5%** | **−28.3%** | **40.0%** |
| **Nifty 50 (clean)** | **3bps** | **−17.4%** | **−53.9%** | **36.7%** |
| **Nifty 100 (clean)** | **0bps** | **−9.9%** | **−35.2%** | **38.1%** |
| **Nifty 100 (clean)** | **3bps** | **−20.0%** | **−59.5%** | **35.4%** |

Survivorship inflation was 15-17pp — far larger than expected for an intraday strategy. Mechanism: the momentum filter (top by 20-day return) systematically over-selects survivors who happen to have strong recent returns. Stocks that were momentum picks but later crashed and delisted are absent.

### Why large-caps fail harder (not just neutral)

Exit-type distribution shifts dramatically in clean universe:
- Biased Phase B: 34% target / 4% EOD close
- Nifty 50: **15% target / 41% EOD close**

Large-caps don't break out cleanly — they drift. Two-sided liquidity → mean reversion overwhelms momentum. WR actually goes UP (40% vs 37%) but outcome distribution shifts to many small losses, few big wins. Asymmetric stop bleeds equity.

### Drawdown shape (from biased Phase B, for reference)

Even if the +7.6% were real, the drawdown profile (one 14-month bleed from 2024-05 to 2025-07, depth −13.5%, 5-month consecutive losing streak) makes leverage unviable for the user's "10% / <3% MDD" target. With 5x prop leverage, MDD would be ~−67% → instant liquidation.

Artifacts:
- `tuning/run_phase_b_nifty.py`, `tuning/phase_b_nifty.{log,json}`
- `tuning/analyze_phase_b_drawdown.py`, `tuning/phase_b_drawdown_analysis.json`

---

## Final intraday-breakout verdict (2026-05-01)

Three honest tests across two days, all dead:
1. Original gap-up "champion" (+24% claim) → −2% after bug fixes
2. Pure ORB (no daily data, top-50 turnover) → −11% raw, every year negative
3. Momentum-filtered ORB → +7.6% biased, −7.5% to −9.9% in survivorship-clean Nifty 50/100

**Conclusion: intraday breakout entries on NSE 2022-2025 lack inherent edge regardless of: entry mechanism (prior-day vs opening range), universe selection (broad turnover vs momentum-filtered vs Nifty 50/100), or execution detail (gap-up filter, market vs limit orders).** Mean reversion appears to dominate the minute-bar dynamics.

Pivot direction: pair trading / stat-arb (structurally less affected by survivorship) and other mean-reversion setups.

## Data Coverage

| Source | Date range | Top 50 coverage | Notes |
|---|---|---|---|
| FMP minute | 2022-2026 | 33-50/50 (by year) | Primary source |
| NSE minute | 2015-2022, 2026 | 17-20/50 | Sparse, 2023-2025 MISSING |

**Backtest window: 2022-01-01 to 2025-12-31** (4 years). FMP is the only
viable minute source for 2023-2025.

Timestamps: LOCAL time labeled UTC (NSE 09:15-15:30 stored as "UTC").

**CRITICAL:** NSE daily and FMP minute use different split-adjustment bases.
Prior-day highs for entry MUST be derived from minute data, not daily.
Daily data used ONLY for signal generation (relative comparisons are safe).

---

## Architecture

Hybrid daily + minute:
1. **Monthly universe**: top 50 NSE stocks by avg daily turnover (>50Cr)
2. **Daily signal**: close >= 3d high, close > 10d MA, close > open, IR hysteresis bull
3. **Intraday entry**: next day, buy when minute bar high > signal day's high (from minute data)
4. **Intraday exit**: fixed target / fixed stop / EOD close (15:25)
5. **All positions close same day** — no overnight risk

---

## R0: Baseline Results (2022-2025, split-fixed)

| Metric | No slippage | 3bps | 5bps |
|---|---:|---:|---:|
| CAGR | 16.28% | 8.38% | 3.80% |
| MDD | -5.75% | -7.69% | -12.49% |
| Calmar | 2.834 | 1.089 | 0.304 |
| Trades | 2,280 | 2,276 | 2,294 |
| Win rate | 42% | 40% | 39% |

Config: target=1.5%, stop=0.75%, max_entry_bar=120, max_positions=5.

**Note:** Pre-split-fix numbers (22-35% CAGR) were inflated by phantom
breakouts from price-basis mismatch between daily and minute data.

---

## R1: Parameter Sweep (split-fixed, 0 slippage)

### R1a: Target (stop=0.75, entry=120) → winner: **1.00%**

| Target | CAGR | MDD | Calmar | WR |
|---:|---:|---:|---:|---:|
| 0.50% | 9.15% | -1.75% | 5.237 | 69% |
| **1.00%** | **9.91%** | **-2.33%** | **4.247** | **51%** |
| 2.00% | 8.10% | -3.34% | 2.424 | 45% |
| 3.00% | 8.37% | -6.12% | 1.367 | 43% |

### R1b: Stop (target=1.0, entry=120) → winner: **0.50%**

| Stop | CAGR | MDD | Calmar | WR |
|---:|---:|---:|---:|---:|
| 0.25% | 8.50% | -2.10% | 4.042 | 31% |
| **0.50%** | **9.90%** | **-2.28%** | **4.339** | **44%** |
| 0.75% | 9.91% | -2.33% | 4.247 | 51% |
| 1.50% | 9.10% | -4.40% | 2.069 | 58% |

### R1c: Entry window (target=1.0, stop=0.5) → winner: **15 bars (09:30)**

| Entry | CAGR | MDD | Calmar |
|---:|---:|---:|---:|
| **15 bars** | **11.76%** | **-1.66%** | **7.064** |
| 30 bars | 11.19% | -1.71% | 6.558 |
| 60 bars | 10.67% | -1.81% | 5.883 |
| 120 bars | 9.90% | -2.28% | 4.339 |
| 375 bars | 8.26% | -2.47% | 3.347 |

### R1d: Positions (target=1.0, stop=0.5, entry=15) → winner: **5**

| Positions | CAGR | MDD | Calmar |
|---:|---:|---:|---:|
| 1 | 10.47% | -10.78% | 0.972 |
| 3 | 11.65% | -4.54% | 2.566 |
| **5** | **11.76%** | **-1.66%** | **7.064** |
| 8 | 9.06% | -1.49% | 6.080 |
| 10 | 7.68% | -1.40% | 5.482 |

### R1e: Slippage sensitivity (best config)

| Slippage | CAGR | MDD | Calmar |
|---:|---:|---:|---:|
| **0 bps** | **11.76%** | **-1.66%** | **7.064** |
| 1 bps | 8.45% | -1.87% | 4.521 |
| 2 bps | 6.06% | -2.25% | 2.700 |
| 3 bps | 3.54% | -3.58% | 0.989 |
| 5 bps | -1.59% | -14.24% | -0.111 |

**Breakeven slippage: ~3 bps/side.**

### R1 Best Config

**target=1.0%, stop=0.5%, entry=15 bars, positions=5**

| Slippage | CAGR | MDD | Calmar | Assessment |
|---:|---:|---:|---:|---|
| 0 bps | 11.76% | -1.66% | 7.064 | Theoretical max |
| 1 bps | 8.45% | -1.87% | 4.521 | Achievable with algo |
| 2 bps | 6.06% | -2.25% | 2.700 | Achievable with limit orders |
| 3 bps | 3.54% | -3.58% | 0.989 | Breakeven |

**Verdict:** Thin edge at all-entries level. See gap-up variant below.

---

## GAP-UP VARIANT (breakthrough finding)

Loss analysis revealed that non-gap entries (stocks that DON'T open above
prior-day high) are NET NEGATIVE (-100K total P&L, 35% WR). All the edge
comes from gap-up entries (stocks that open above prior-day high at 09:15).

### Gap-up only results

| Slippage | CAGR | MDD | Sharpe | Calmar | Trades | WR |
|---:|---:|---:|---:|---:|---:|---:|
| 0 bps | **24.14%** | **-0.81%** | 4.674 | **29.839** | 1,425 | **58%** |
| 1 bps | 22.72% | -0.83% | 4.396 | 27.460 | 1,432 | 58% |
| 2 bps | 21.17% | -0.74% | 3.974 | 28.619 | 1,431 | 57% |
| 3 bps | 19.81% | -0.86% | 3.641 | 22.929 | 1,426 | 57% |
| 5 bps | **16.49%** | **-0.90%** | 2.837 | **18.282** | 1,430 | 55% |

**Profitable at ANY realistic slippage.** Even 5bps gives 16.49% CAGR.

### Comparison to all-entries

| | All entries | Gap-up only |
|---|---:|---:|
| CAGR (0 slip) | 11.76% | **24.14%** (+12.4pp) |
| MDD | -1.66% | **-0.81%** (+0.85pp) |
| Calmar | 7.064 | **29.839** (4.2x) |
| Win rate | 45% | **58%** (+13pp) |

### With Zerodha MIS leverage (free, no extra charges)

| Slippage | 1x | 3x | 5x | 5x MDD |
|---:|---:|---:|---:|---:|
| 0 bps | 24% | ~72% | ~121% | ~-4.1% |
| 2 bps | 21% | ~63% | ~106% | ~-3.7% |
| 5 bps | 16% | ~49% | ~82% | ~-4.5% |

### Yearly (0 slippage)

| Year | Trades | Trades/wk | WR | Return |
|---:|---:|---:|---:|---:|
| 2022 | 32 | <1 | 56% | +1.9% |
| 2023 | 471 | 9 | 60% | +35.1% |
| 2024 | 518 | 9 | 55% | +32.3% |
| 2025 | 404 | 7 | 60% | +30.4% |

### The algorithm

```
EVERY EVENING (after market close):
  1. Universe: top 50 NSE stocks by avg daily turnover (monthly rebalance)
  2. For each stock, check daily signal:
     - close >= 3-day rolling high (breakout)
     - close > 10-day MA (trend)
     - close > open (bullish candle)
     - internal regime bullish (>40% of universe above 50d SMA,
       hysteresis: stays bull until <35%)
  3. Stocks passing ALL → "eligible for tomorrow"

NEXT MORNING 09:15:
  4. For each eligible stock: did it GAP UP?
     (first bar open > yesterday's high from minute data)
     - YES → BUY at open (market order)
     - NO  → SKIP
  5. Max 5 positions, each = margin / 5

DURING DAY (09:15 - 15:25):
  6. Watch each position:
     - Price hits +1.0% → SELL (target)
     - Price hits -0.5% → SELL (stop)
     - 15:25 → SELL all (EOD close, no overnight)
```

---

## Next Steps (resume here)

1. **Tune gap-up variant** — sweep target/stop around current best
   (target 0.75-1.5%, stop 0.25-0.75%) with gap-up filter
2. **Minimum gap size** — does requiring a larger gap (e.g., >10bps)
   improve win rate further?
3. **Volume at open** — does high first-bar volume predict better trades?
4. **Symbol filtering** — blacklist worst performers (PNB, SUNPHARMA etc.)
5. **Trailing stop** — instead of fixed target, trail winners
6. **Yearly IS/OOS** — train on 2023-2024, test on 2025
7. **Leverage simulation** — actually model 3x/5x with margin calls
8. **Live paper trading** — forward test with Kite API

---

## Slippage Analysis (from minute bar data)

Measured on top-20 NSE stocks, 2024:
- Median 1-min bar range (all day): 7.9 bps
- Median 1-min bar range (first 15 min): **17.0 bps** (wider at open)
- Large-caps (HDFCBANK, RELIANCE): 6-7 bps
- Mid-caps (IRFC, RVNL): 10-11 bps

Limit order fill analysis (7,903 signal-instrument pairs):
- 42.3% fill at exact limit price (0 slippage)
- 25.6% gap-up (fill at open, median 34 bps worse)
- 32.1% no fill (stock never reaches limit)

**Limit orders don't help** — gap-up stocks are the strongest breakouts.
Skipping them loses the best trades (confirmed: limit-only variants
produce negative returns).

---

## Optimization Methodology

Adapted from EOD OPTIMIZATION_PROMPT.md. Each round narrows the search space.

### R0: Baseline validation (COMPLETE)
### R1: Coarse sweep (COMPLETE)
### R2: Fine grid (PENDING)
### R3: Robustness — IS/OOS, yearly stability
### R4: Variants & Extensions

---

## Ideas Backlog

### Entry improvements
- Volume heuristic: require entry bar volume > 1.5x avg
- Gap-up filter: only trade if stock gaps up from previous close
- VWAP confirmation at entry
- Rolling 15-30 min intraday high (instead of prior-day high)

### Exit improvements
- Trailing stop on minute bars
- Partial profit booking (exit 50% at target, trail rest)
- VWAP exit
- Time-based scaling: widen target in morning, tighten in afternoon

### Position management
- Leverage (Zerodha MIS: 5x on equity intraday)
- Risk-based sizing
- Sector correlation check

### Universe
- Nifty 50 only (tightest spreads)
- Index ETFs: NIFTYBEES, BANKBEES
- Futures: NIFTY/BANKNIFTY (lower costs, higher leverage)

### Other
- Shorting: sell breakdown below prior-day low
- Regime-only mode (no breakout filter)
- Intraday regime from minute data

---

## Execution Environment

**Prod machine:** `swas@80.241.215.48`
- CPU: AMD EPYC, RAM: 251GB, Python 3.10, Polars 1.39

**Data paths:**
- Daily: `/opt/insydia/data/data_source=nse/charting/granularity=day/`
- Minute (FMP): `/opt/insydia/data/data_source=fmp/tick_data/stock/granularity=1min/exchange=NSE/`

**Performance:** ~50s per config on prod (load ~15s + simulate ~35s).

---

## Commits

| Commit | What |
|---|---|
| `c187935` | Initial intraday breakout pipeline |
| `17c62c3` | Fix entry price: use signal day high |
| `d50aa2b` | R0 results, prod runner, STATUS doc |
| `269ecda` | Fix split-adjustment mismatch (minute-derived highs) |
| `3ec06a9` | R1 sweep complete, STATUS updated |
| _(next)_ | Gap-up variant: 24% CAGR, Calmar 29.8 |

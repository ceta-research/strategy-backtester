# Intraday Breakout — STATUS & Tracking

**Created:** 2026-04-29
**Status:** R0 COMPLETE, R1 PENDING
**Parent strategy:** eod_breakout (IR hysteresis, Calmar 1.350)
**Prod workspace:** `swas@80.241.215.48:/home/swas/backtester/`
**Prod runner:** `intraday_breakout_prod.py`

---

## Data Coverage

| Source | Date range | Top 50 coverage | Notes |
|---|---|---|---|
| FMP minute | 2022-2026 | 33-50/50 (by year) | Primary source |
| NSE minute | 2015-2022, 2026 | 17-20/50 | Sparse, 2023-2025 MISSING |

**Backtest window: 2022-01-01 to 2025-12-31** (4 years). FMP is the only
viable minute source for 2023-2025.

Timestamps: LOCAL time labeled UTC (NSE 09:15-15:30 stored as "UTC").

---

## Architecture

Hybrid daily + minute:
1. **Monthly universe**: top 50 NSE stocks by avg daily turnover (>50Cr)
2. **Daily signal**: close >= 3d high, close > 10d MA, close > open, IR hysteresis bull
3. **Intraday entry**: next day, buy when minute bar high > signal day's high
4. **Intraday exit**: fixed target / fixed stop / EOD close (15:25)
5. **All positions close same day** — no overnight risk

---

## R0: Baseline Results (2022-2025)

| Metric | No slippage | 5bps slippage |
|---|---:|---:|
| CAGR | 22.38% | 8.19% |
| MDD | -2.21% | -8.68% |
| Sharpe | 2.558 | 0.275 |
| Calmar | 10.114 | 0.943 |
| Trades | 2,543 | 2,543 |
| Win rate | 48% | 44% |

**Config:** target=1.5%, stop=0.75%, max_entry_bar=120 (first 2 hours),
max_positions=5, eod_exit=15:25.

**Yearly breakdown (no slippage):**

| Year | Trades | Return |
|---:|---:|---:|
| 2022 | 82 | -0.3% |
| 2023 | 848 | +40.7% |
| 2024 | 860 | +37.6% |
| 2025 | 753 | +16.2% |

**Trade characteristics:**
- 91% of entries in first hour (09:15-10:15)
- Exit distribution: 32% target, 44% stop, 24% EOD close
- EOD close trades avg +0.23% (positive — trend continuation)
- Avg 4.1 positions/day, trades 62% of days
- Order value: 200K-450K per position (equal weight)

**Key concern:** Slippage sensitivity. 5bps/side cuts CAGR from 22% to 8%.
For top-50 large-caps, realistic slippage is 2-3bps. True CAGR likely 14-18%.

---

## Optimization Methodology

Adapted from EOD OPTIMIZATION_PROMPT.md. Each round narrows the search space.

### R0: Baseline validation (COMPLETE)
- Single config, validate pipeline end-to-end
- Verify all positions close same day
- Verify charges are correct (intraday STT 0.025%)
- Verify entry/exit logic is honest (no look-ahead)

### R1: Coarse sweep (NEXT)
Sweep ONE parameter at a time while holding others at R0 baseline.
This identifies which parameters matter before doing Cartesian product.

**R1a: Target/stop ratio**
- target_pct: [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
- stop_pct: hold at 0.75
- Goal: find optimal target size

**R1b: Stop size**
- stop_pct: [0.25, 0.5, 0.75, 1.0, 1.5]
- target_pct: hold at R1a winner
- Goal: find optimal stop size

**R1c: Entry window**
- max_entry_bar: [30, 60, 120, 180, 240, 375]
- target/stop: hold at R1a/R1b winners
- Goal: is early entry better?

**R1d: Max positions**
- max_positions: [3, 5, 8, 10, 15]
- Goal: can we deploy more capital?

**R1e: Slippage sensitivity**
- slippage_bps: [0, 1, 2, 3, 5, 10]
- At R1a-R1d winner config
- Goal: find the breakeven slippage

### R2: Fine grid
- Narrow sweep around R1 winners (e.g., if R1a says target=2.0,
  sweep 1.5/1.75/2.0/2.25/2.5)
- Cartesian product of top 2-3 values per param

### R3: Robustness
- IS/OOS split: 2022-2024 train, 2025 test
- Per-year stability: does each year show positive returns?
- Monte Carlo: shuffle trade order, check Sharpe stability

### R4: Variants & Extensions
- [ ] Rolling 15-30 min intraday high (instead of prior-day high)
- [ ] Volume confirmation at entry (bar volume > 1.5x avg)
- [ ] VWAP-based entry (enter at VWAP instead of exact breakout)
- [ ] Trailing stop on minute bars (instead of fixed target)
- [ ] Universe size: 50 vs 100 vs 200
- [ ] Ranking: top_gainer momentum vs volume-weighted
- [ ] Shorting: sell breakdown below prior-day low
- [ ] Index trading: NIFTYBEES/NIFTY50 instead of individual stocks
- [ ] Leverage: 2x-5x MIS margin
- [ ] Daily filter modes: regime-only, no filter

---

## Ideas Backlog

### Entry improvements
- Use VWAP of first 5 bars as entry confirmation (not just breakout)
- Volume heuristic: require entry bar volume > 1.5x 20-day avg bar volume
- Gap-up filter: only trade if stock gaps up from previous close
- Time-weighted entry: weight earlier entries higher
- Limit order at prior_high instead of market order (removes slippage but risks non-fill)

### Exit improvements
- Trailing stop on minute bars (tighten as profit grows)
- Partial profit booking (exit 50% at target, trail rest)
- VWAP exit: exit when price crosses below intraday VWAP
- Time-based scaling: widen target in morning, tighten in afternoon

### Position management
- Allocate more than 5 positions (intraday can handle 10-15)
- Use leverage (Zerodha MIS: 5x on equity intraday)
- Risk-based sizing: smaller positions on wider stops
- Correlation check: avoid 5 positions in same sector

### Universe
- Nifty 50 only (most liquid, tightest spreads)
- Sector rotation: trade leading sectors
- Index ETFs: NIFTYBEES, BANKBEES
- Futures: NIFTY/BANKNIFTY futures (lower costs, higher leverage)

### Regime / signal
- Intraday regime: market breadth computed from minute data
- Gap analysis: are gap-up breakouts stronger?
- Pre-market data: use pre-market auction price for signal

---

## Execution Environment

**Prod machine:** `swas@80.241.215.48`
- CPU: AMD EPYC
- RAM: 251GB (59GB free)
- Disk: 104GB free
- Python 3.10, Polars 1.39, PyYAML

**Data paths:**
- Daily: `/opt/insydia/data/data_source=nse/charting/granularity=day/`
- Minute (FMP): `/opt/insydia/data/data_source=fmp/tick_data/stock/granularity=1min/exchange=NSE/`

**Performance:** ~65s per config (load 13s + simulate 50s). 80-config sweep ~90 min.

**Workflow:**
1. Edit config locally
2. scp to prod: `scp config.yaml swas@80.241.215.48:/home/swas/backtester/`
3. Run: `ssh prod "cd /home/swas/backtester && python3 intraday_breakout_prod.py config.yaml --output result.json"`
4. Copy results back: `scp swas@80.241.215.48:/home/swas/backtester/result.json .`

---

## Commits

| Commit | What |
|---|---|
| `c187935` | Initial intraday breakout pipeline |
| `17c62c3` | Fix entry price: use signal day high |

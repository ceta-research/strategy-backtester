# Intraday Breakout — STATUS

**Created:** 2026-04-29
**Status:** BUILDING
**Parent strategy:** eod_breakout (IR hysteresis, Calmar 1.350)

## Concept

Day-trading version of the eod_breakout strategy. Same breakout + regime
logic for stock selection, but entries/exits happen intraday on minute bars.

## Architecture

Hybrid daily + minute approach:
1. **Monthly universe**: top 50-100 NSE stocks by market cap + liquidity
2. **Daily signal**: breakout (close >= N-day high, close > MA, bullish candle) + internal regime with hysteresis (enter > 0.4, exit < 0.35)
3. **Intraday entry**: buy when price breaks above prior day's high
4. **Intraday exit**: target/stop/trailing/time-stop/EOD close

## Data

- Daily OHLCV: nse_charting (same as EOD strategy)
- Minute OHLCV: fmp.stock_prices_minute (timestamps = LOCAL time labeled UTC)
- Date range: 2020-01-01 to 2025-12-31

## Entry Types to Test

- [x] Prior-day-high breakout (Round 1)
- [ ] Rolling 15-30 min intraday high breakout
- [ ] Opening range breakout (first N bars)
- [ ] Gap-up filter variant

## Daily Filter Modes to Test

- [x] Hard gate (daily breakout + regime must pass)
- [ ] No daily filter (pure intraday on universe)
- [ ] Regime-only (no breakout filter)

## Optimization Rounds

- [ ] R0: Baseline — single config, validate signal + execution
- [ ] R1: Entry sweep — max_entry_bar, target/stop combinations
- [ ] R2: Fine grid — narrow around R1 winners
- [ ] R3: Robustness — IS/OOS split, yearly stability
- [ ] R4: Variants — rolling high, ranking, position sizing

## Results

_(to be populated)_

## Key Differences from EOD

| Aspect | EOD | Intraday |
|---|---|---|
| Hold period | Days-weeks | Minutes-hours |
| Universe | ~500 stocks (price > 99) | Top 50-100 large-cap |
| Entry | Next-day open | Minute-bar breakout |
| Exit | TSL 8% + regime flip | Target/stop/time/EOD |
| Costs | ~0.35% RT delivery | ~0.08% RT intraday |

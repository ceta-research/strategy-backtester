# Live Trading Integration Plan: Momentum Dip-Buy Strategy

## Strategy Spec (Champion, Calmar 1.01 on NSE)

| Parameter | Value |
|-----------|-------|
| Quality gate | 2yr consecutive positive returns |
| Momentum filter | Top 30% by 63d trailing return |
| Entry signal | 5% dip from 63d rolling peak |
| Fundamental filter | ROE>15%, PE<25, D/E<1.0 |
| Execution | Signal at close[i], execute at open[i+1] (MOC) |
| Position sizing | Equal weight: account_value / 10 |
| Max positions | 10 |
| Exit: TSL | 10% trailing stop (activates after peak recovery) |
| Exit: Max hold | 504 trading days |
| Regime filter | SMA200 (benchmark above SMA = bull) |
| Charges | NSE delivery (STT 0.1%, stamp, GST) + 5 bps slippage |

---

## Architecture Decision: Standalone Signal Generator

**Problem:** ATO_Simulator's order_generation_step is built for breakout entries (`close >= n_day_high`). Our dip-buy strategy needs the opposite (`close <= peak * 0.95`). Modifying core pipeline risks breaking existing strategies.

**Decision:** Build a standalone EOD signal generator that runs post-market, produces a shortlist, and feeds into the existing strategy_integration flow.

**Why:**
- Reuses exact backtested logic from `quality_dip_buy_lib.py` (validated, no bugs)
- No changes to core ATO_Simulator pipeline
- Can run independently and be audited against backtest predictions
- Easier to paper trade first

---

## System Design

```
Post-Market (15:35 IST)                Pre-Market (09:14 IST)
┌──────────────────────┐                ┌──────────────────────┐
│ 1. Fetch EOD data    │                │ 5. Read shortlist    │
│    (NSE charting)    │                │    from state file   │
│                      │                │                      │
│ 2. Update state:     │                │ 6. Check positions   │
│    - quality universe│                │    vs max_positions  │
│    - momentum rank   │                │                      │
│    - open positions  │                │ 7. Place AMO orders  │
│    - TSL tracking    │                │    (BUY entries)     │
│                      │                │                      │
│ 3. Generate signals: │                │ 8. Place exit orders │
│    - New entries     │                │    (SELL for TSL/    │
│    - Exit triggers   │                │     max-hold exits)  │
│                      │                └──────────────────────┘
│ 4. Write shortlist   │
│    + exit orders     │
│    to state file     │
└──────────────────────┘
```

### State File: `~/.ato/dip_buy_state.json`

```json
{
  "last_updated": "2026-03-26T15:35:00+05:30",
  "regime": "bull",
  "quality_universe": ["INFY", "TCS", "HDFCBANK", ...],
  "momentum_universe": ["TATAELXSI", "BHARTIARTL", ...],
  "combined_universe": ["INFY", "TCS", ...],
  "pending_entries": [
    {
      "symbol": "INFY",
      "signal_date": "2026-03-26",
      "peak_price": 1850.0,
      "dip_price": 1757.5,
      "current_close": 1740.0,
      "order_value": 1000000,
      "quantity": 571
    }
  ],
  "pending_exits": [
    {
      "symbol": "HDFCBANK",
      "reason": "tsl",
      "trail_high": 1650.0,
      "tsl_trigger": 1485.0,
      "current_close": 1480.0
    }
  ],
  "positions": [
    {
      "symbol": "TCS",
      "entry_date": "2025-11-15",
      "entry_price": 3800.0,
      "quantity": 263,
      "peak_since_entry": 4200.0,
      "reached_peak": true,
      "days_held": 95
    }
  ]
}
```

---

## Implementation Plan

### Phase 1: EOD Signal Generator (new script)

**File:** `scripts/live_signal_generator.py`

Responsibilities:
1. Fetch today's EOD data from NSE (via CR API or Kite historical)
2. Recompute quality universe (every 63 days or on schedule)
3. Recompute momentum universe (every 63 days)
4. Intersect quality + momentum + fundamental filters
5. Check dip conditions against 63d rolling peaks
6. For existing positions: check TSL, max-hold, regime
7. Write state file with pending entries/exits

Key difference from backtest: uses live data, not historical replay.

```python
# Core logic (reuses quality_dip_buy_lib.py functions):
# 1. Quality: compute_quality_universe() with live price history
# 2. Momentum: compute_momentum_universe() with live price history
# 3. Dip detection: compare today's close vs 63d peak
# 4. Fundamentals: fetch_fundamentals() from FMP (45-day lag)
# 5. Regime: benchmark SMA200 check
# 6. Position management: TSL tracking, max-hold counting
```

### Phase 2: Order Placement Integration

**Modify:** `ATO/ATO_UserUtil/src/ATO_UserUtil/strategy_integration/`

Add `dip_buy_order_placement.py`:
1. Read state file
2. For each pending_entry:
   - Check available margin
   - Check position count < max_positions
   - Place AMO BUY order at market (or limit at yesterday's close)
3. For each pending_exit:
   - Place AMO SELL order at market
4. Log all orders to audit trail

### Phase 3: Position Tracking

**Modify:** Post-session script to:
1. Check which AMO orders were executed
2. Update state file with new positions / closed positions
3. Run signal generator for next day
4. Record daily P&L snapshot

### Phase 4: Risk Limits

| Limit | Value | Action |
|-------|-------|--------|
| Max positions | 10 | Skip new entries |
| Max per symbol | 1 | Skip if already held |
| Max sector | 3 per sector | Skip if sector full |
| Drawdown kill switch | -30% from peak | Close all, pause 30 days |
| Regime filter | Benchmark < SMA200 | No new entries (hold existing) |
| Min order value | Rs 50,000 | Skip small orders |
| Max order value | Rs 2,000,000 | Cap per position |

### Phase 5: Paper Trading Validation

Before going live:
1. Run signal generator daily for 30 days
2. Compare generated signals against what backtest would have produced
3. Verify entry/exit prices match expectations
4. Check position sizing is correct
5. Audit charges calculation
6. Confirm no look-ahead (signals use yesterday's close, not today's)

---

## Key Files

| File | Action | Purpose |
|------|--------|---------|
| `scripts/live_signal_generator.py` | CREATE | EOD signal generation |
| `scripts/quality_dip_buy_lib.py` | REUSE | Core filters (quality, momentum, dip) |
| `scripts/quality_dip_buy_fundamental.py` | REUSE | Fundamental filters |
| `~/.ato/dip_buy_state.json` | CREATE | Live state tracking |
| `ATO_UserUtil/.../dip_buy_order_placement.py` | CREATE | AMO order placement |
| `TS_Scripts/.../pre_session_script.py` | MODIFY | Add dip-buy to pre-session |
| `TS_Scripts/.../post_session_script.py` | MODIFY | Add position reconciliation |

## Execution Timeline

| Phase | Duration | Prereq |
|-------|----------|--------|
| 1. Signal generator | 2-3 days | None |
| 2. Order placement | 1-2 days | Phase 1 |
| 3. Position tracking | 1-2 days | Phase 2 |
| 4. Risk limits | 1 day | Phase 3 |
| 5. Paper trading | 30 days | Phase 4 |
| **Go live** | After paper trading validates | All phases |

## Open Questions

1. **Capital allocation:** How much of total account to allocate? (suggest 30-50% initially)
2. **Order type:** AMO market vs AMO limit at previous close?
3. **Partial fills:** How to handle? (suggest: accept partial, adjust position size)
4. **Corporate actions:** Bonus/split handling for peak tracking?
5. **Data source for live:** Kite historical API vs CR API vs both?

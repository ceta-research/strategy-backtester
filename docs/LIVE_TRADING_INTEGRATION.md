# Live Trading Integration Plan

**Strategy:** `eod_technical` (current champion, post-audit engine `fbcd36a+`)
**Backtested performance (2010-2026, NIFTYBEES universe):** 19.63% CAGR / Cal 0.757 / Sharpe 1.07
**Caveat:** Pre-2019 standalone CAGR is 8.62%. Performance is regime-dependent
on the 2019+ NSE mid-cap bull. Forward expectation: 10-13% CAGR if regime reverts.

> Earlier draft of this doc covered standalone `momentum_dip_buy` (Cal 1.01)
> from pre-engine work. That champion no longer exists post-audit (Layer 4
> P0 fix invalidated the standalone numbers). Current champion is the
> engine-pipeline `eod_technical` — see
> [`strategies/eod_technical/OPTIMIZATION.md`](../strategies/eod_technical/OPTIMIZATION.md)
> for full optimization history.

---

## Strategy spec (champion config)

Source: [`strategies/eod_technical/config_champion.yaml`](../strategies/eod_technical/config_champion.yaml)

| Parameter | Value | Notes |
|---|---|---|
| Universe | NSE, price > 50, 125d avg turnover > 7Cr | Liquidity gate |
| Entry: MA filter | `close > 3-day MA` | Recent uptrend |
| Entry: Breakout | `close >= 5-day high` | Short breakout |
| Entry: Direction score | `>= 0.54` of stocks above 3d MA | Market breadth gate |
| Sort/rank | `top_gainer`, 30-day window | Pick best of multiple breakouts |
| Exit: TSL | 10% trailing stop | Activates from entry |
| Exit: Min hold | 3 days | Avoid same-day flips |
| Position sizing | Equal weight | `account_value / 15` |
| Max positions | 15 | Concentrated portfolio |
| Max per symbol | 1 | No pyramiding |
| Execution | Signal at close T, BUY at next_open T+1 | MOC, no same-bar bias |
| Charges | NSE delivery (STT 0.1%, stamp, GST) + 5 bps slippage | Per `engine/charges.py` |

---

## Architecture

The engine path (`engine/signals/eod_technical.py` →
`engine/order_generator.py` → `engine/simulator.py`) produces orders with
explicit `entry_epoch` / `exit_epoch`. For live trading we don't need a
re-implementation — we need a thin daily wrapper that:

1. Pulls the same engine signal generator,
2. Runs it on data through yesterday's close,
3. Persists the resulting shortlist for tomorrow's open,
4. Reconciles fills + open-position state.

```
Post-Market (15:35 IST)                Pre-Market (09:14 IST)
┌──────────────────────────┐           ┌──────────────────────────┐
│ 1. Fetch EOD T data      │           │ 5. Read state file       │
│    (NSE charting / Kite) │           │                          │
│                          │           │ 6. Pre-trade checks      │
│ 2. Run eod_technical     │           │    - margin              │
│    signal gen on data    │           │    - position cap (15)   │
│    through close T       │           │    - sector cap          │
│                          │           │                          │
│ 3. Apply universe +      │           │ 7. Place AMO orders      │
│    breadth + breakout    │           │    - BUY entries         │
│    filters → shortlist   │           │    - SELL TSL/timed exits│
│                          │           │                          │
│ 4. Persist state:        │           │ 8. Audit log             │
│    shortlist + exits     │           └──────────────────────────┘
└──────────────────────────┘
```

### State file: `~/.ato/eod_technical_state.json`

```json
{
  "last_updated": "2026-04-25T15:35:00+05:30",
  "engine_commit": "fbcd36a",
  "champion_config": "strategies/eod_technical/config_champion.yaml",
  "regime": "bull",
  "breadth_score": 0.61,
  "pending_entries": [
    {
      "symbol": "TATAELXSI",
      "signal_date": "2026-04-25",
      "trigger_close": 7245.0,
      "5d_high": 7240.0,
      "rank": 1,
      "order_value": 666666,
      "quantity": 92
    }
  ],
  "pending_exits": [
    {
      "symbol": "BHARTIARTL",
      "reason": "trailing_stop",
      "trail_high": 1620.0,
      "tsl_trigger": 1458.0,
      "current_close": 1455.5
    }
  ],
  "positions": [
    {
      "symbol": "INFY",
      "entry_date": "2026-03-12",
      "entry_price": 1480.0,
      "quantity": 450,
      "peak_since_entry": 1612.0,
      "days_held": 32
    }
  ]
}
```

---

## Implementation phases

### Phase 1 — Daily signal runner

**File:** `scripts/live_eod_technical.py`

1. Fetch EOD data for current NSE universe (price > 50, liquidity-filtered).
2. Invoke the engine signal generator the same way `run.py` does, but
   with `end_epoch = today_close` and a single-day output window.
3. Translate engine orders for `entry_epoch == tomorrow_open` into a
   shortlist; translate `exit_epoch == tomorrow_open` into pending exits.
4. Update peak-since-entry / TSL triggers for open positions.
5. Write state file.

**Key invariant:** the live runner must use the SAME signal generator
the backtest uses (`engine/signals/eod_technical.py`). Any
re-implementation drifts and silently invalidates backtest expectations.

### Phase 2 — Order placement integration

**Module:** `ATO/ATO_UserUtil/strategy_integration/eod_technical_orders.py`

1. Read state file.
2. For each pending entry: pre-trade checks → place AMO BUY at market
   (or AMO limit at signal close + 0.5%).
3. For each pending exit: place AMO SELL at market.
4. Audit-log all placements with broker order IDs.

### Phase 3 — Post-session reconciliation

1. Pull executed AMO fills from broker.
2. Update state file with new positions / closed positions / partial fills.
3. Record daily P&L snapshot.
4. Trigger Phase 1 for next day.

### Phase 4 — Risk limits

| Limit | Value | Action on breach |
|---|---|---|
| Max positions | 15 | Skip new entries |
| Max per symbol | 1 | Skip if already held |
| Max sector | 4 per sector | Skip if sector full (NEW — backtest doesn't enforce) |
| Min order value | ₹50,000 | Skip small orders |
| Max order value | ₹2,000,000 | Cap per position |
| Drawdown kill switch | -25% from peak | Close all, pause 30 days |
| Breadth gate | `direction_score < 0.40` | No new entries (hold existing) |
| Engine drift check | `git rev-parse HEAD != fbcd36a` | Block new entries until verified |

### Phase 5 — Paper trading validation (30 days)

Before going live:

1. Run signal runner daily for 30 trading days.
2. **Reconciliation test:** for each day, also run the engine backtest
   over the same window — compare generated signals one-for-one. Any
   mismatch is a bug.
3. Verify entry/exit prices match backtest (within slippage band).
4. Check position sizing.
5. Audit charges calculation against broker contract notes.
6. Confirm no look-ahead — signals at close T, executions next-day open.

---

## Key files

| File | Action | Purpose |
|---|---|---|
| `scripts/live_eod_technical.py` | CREATE | Daily signal runner |
| `engine/signals/eod_technical.py` | REUSE (no changes) | Signal logic — must match backtest |
| `~/.ato/eod_technical_state.json` | CREATE | Live state |
| `ATO_UserUtil/.../eod_technical_orders.py` | CREATE | AMO placement |
| `TS_Scripts/.../pre_session_script.py` | MODIFY | Wire eod_technical into pre-session |
| `TS_Scripts/.../post_session_script.py` | MODIFY | Wire reconciliation into post-session |

---

## Open questions

1. **Capital allocation** — how much of total account? Suggest 30-50%
   initially given 2026-04-24 handover note that pre-2019 CAGR was 8.62%.
2. **Order type** — AMO market vs AMO limit at signal close? Limit
   matches backtest more honestly but risks unfilled in fast markets.
3. **Partial fills** — backtest assumes full fill at next_open. Live
   should accept partial and adjust position size.
4. **Corporate actions** — bonus/split handling for `peak_since_entry`
   tracking. NSE adjusts close prices, but state file needs explicit
   corp-action ingestion to avoid spurious TSL trips.
5. **Data source for live** — Kite historical API for execution-aligned
   prices, or CR API (matches backtest)? Suggest both: Kite for
   reconciliation, CR for signal regeneration.
6. **Champion drift** — `eod_technical` Std Cal 0.723 FAILS the WF
   fragility gate. Need to decide: paper-trade as-is, or swap to a more
   robust champion (e.g. `forced_selling_dip`, Std Cal 0.218 PASSES) for
   live deployment?

---

## Cross-refs

- Champion optimization detail: [`strategies/eod_technical/OPTIMIZATION.md`](../strategies/eod_technical/OPTIMIZATION.md)
- Top 5 alternatives + Calmar leaderboard: [`STATUS.md`](STATUS.md)
- Engine baseline + protected files: [`STATUS.md`](STATUS.md)
- Why pre-2019 vs post-2019 differs: [`sessions/2026-04-24_pt2_handover.md`](sessions/2026-04-24_pt2_handover.md) §3 "Regime dependency is pervasive on NSE"

# Live Trading Integration Plan

**Last updated:** 2026-04-28
**Engine baseline:** `fbcd36a` (post-audit)
**Recommended deployment:** **Ensemble** — `eod_breakout` (regime+holdout)
+ `eod_technical`, inverse-vol weighted, quarterly rebalanced.

> Earlier drafts of this doc covered `eod_technical` solo, and before that
> a pre-engine `momentum_dip_buy`. The current recommendation supersedes
> both. Solo deployment paths are retained below as Option B / Option C
> in case operational simplicity matters more than the ensemble's Sharpe lift.

---

## Why an ensemble (TL;DR)

| Variant | CAGR | MDD | Calmar | Sharpe | 2025 |
|---|---:|---:|---:|---:|---:|
| `eod_technical` solo | 19.63% | -25.95% | 0.757 | 1.067 | +2.69% |
| `eod_breakout` regime+holdout solo | 17.68% | -26.75% | 0.661 | 1.183 | +18.67% |
| **🏆 Ensemble (eod_b 56% / eod_t 44%, inverse-vol qtly)** | **18.79%** | **-23.81%** | **0.789** | **1.281** | (mix) |

Ensemble Sharpe **lifts above both solo legs** (1.067 / 1.183 → 1.281). MDD
is shallower than either solo. CAGR sits between the two — close enough to
the higher-CAGR leg that the diversification cost is small. Daily-return
correlation between the two legs is 0.59, low enough for diversification to
work but high enough that sizing disagreements are manageable.

**Source:** `strategies/ensembles/eod_eodt_invvol_quarterly_full/config.yaml`,
result `results/ensembles/eod_eodt_invvol_quarterly_full.json`. See
[`ENSEMBLE_GUIDE.md`](ENSEMBLE_GUIDE.md) for the math.

---

## Deployment options

### Option A — Ensemble (recommended)

| | |
|---|---|
| Capital split | 56% to `eod_breakout`, 44% to `eod_technical` (current inverse-vol weights) |
| Rebalance | Quarterly: first NSE trading day of Jan/Apr/Jul/Oct |
| Regime gate | DAILY: NIFTYBEES vs SMA(100) — applies only to the `eod_breakout` sleeve |
| Pos cap | 15 per sleeve (30 total max) |
| Sector cap | 4 per sector across both sleeves combined (NEW — neither backtest enforces) |
| Friction overhead | ~25 bps/yr from quarterly rebalance trades (see Friction model below) |

### Option B — `eod_breakout` regime+holdout solo

For operational simplicity (single sleeve, no rebalance plumbing). Sharpe
1.183 alone is the second-highest in the suite. Trade-off: gives up the
ensemble Sharpe lift (~+0.10) and absorbs eod_breakout's full MDD profile
(-26.75%).

### Option C — `eod_technical` solo

Highest single-leg CAGR (19.63%). Worst Sharpe of the three (1.067).
Pre-2019 CAGR is 8.62% — performance is regime-dependent on the 2019+ NSE
mid-cap bull. Forward expectation 10-13% CAGR if regime reverts. Use only
if operational complexity must be minimized AND the Sharpe gap is
acceptable.

---

## Strategy specs

### `eod_breakout` (regime+holdout champion)

Source: [`strategies/eod_breakout/config_champion.yaml`](../strategies/eod_breakout/config_champion.yaml)

| Parameter | Value | Notes |
|---|---|---|
| Universe | NSE, price > 99, 125d avg turnover > 7Cr | Liquidity gate (price ↑ from 50) |
| Entry: MA filter | `close > 10-day MA` | Trend confirmation |
| Entry: Breakout | `close >= 3-day high` | Faster breakout (vs ndh=7 pre-promo) |
| Entry: Direction score | `>= 0.40` of stocks above 5d MA | Looser breadth, longer MA |
| Entry: Bullish bar | `close > open` | Avoids reversal day entries |
| **Regime gate** | NIFTYBEES > SMA(100) | **No new entries when off.** |
| **Force-exit on flip** | True | Sell positions at next open when regime flips bear |
| Exit: TSL | 8% trailing stop | Tighter than eod_technical |
| Exit: Min hold | 7 days | Resists same-week whipsaw |
| Sort/rank | top_gainer, 180d window | Rank by 180d gain |
| Max positions | 15 | |

### `eod_technical` (champion, no regime gate)

Source: [`strategies/eod_technical/config_champion.yaml`](../strategies/eod_technical/config_champion.yaml).
Regime+holdout was tested 2026-04-28 and rejected (negative result on every
metric — see `strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md`).
**Solo deployment uses no regime gate.**

| Parameter | Value | Notes |
|---|---|---|
| Universe | NSE, price > 50, 125d avg turnover > 7Cr | Liquidity gate |
| Entry: MA filter | `close > 3-day MA` | Recent uptrend |
| Entry: Breakout | `close >= 5-day high` | Short breakout |
| Entry: Direction score | `>= 0.54` of stocks above 3d MA | Standard breadth gate |
| Sort/rank | top_gainer, 30d window | |
| Exit: TSL | 10% trailing stop | |
| Exit: Min hold | 3 days | |
| Max positions | 15 | |

Execution model for both: **signal at close T, BUY at next_open T+1 (MOC).
No same-bar bias.** Charges per `engine/charges.py` (NSE delivery: STT 0.1%,
stamp, GST) plus 5 bps slippage.

---

## Daily operations (Option A — ensemble)

```
Post-Market 15:35 IST                  Pre-Market 09:14 IST
┌──────────────────────────────────┐   ┌────────────────────────────────────┐
│ 1. Fetch EOD T data              │   │ 6. Read state file                 │
│    (NSE charting)                │   │                                    │
│                                  │   │ 7. Pre-trade checks                │
│ 2. Compute regime: NIFTYBEES vs  │   │    - margin per sleeve             │
│    SMA(100). Cache prev value.   │   │    - position cap (15/sleeve)      │
│                                  │   │    - sector cap (4 across both)    │
│ 3. Run eod_breakout signal gen.  │   │                                    │
│    If regime=bear: NO new        │   │ 8. Place AMO orders                │
│    entries; if regime FLIPPED    │   │    - eod_b BUY entries (or none)   │
│    bull→bear today: FORCE-EXIT   │   │    - eod_t BUY entries             │
│    all eod_b positions next open │   │    - eod_b/eod_t SELL exits        │
│                                  │   │    - rebalance trades (qtly only)  │
│ 4. Run eod_technical signal gen. │   │                                    │
│    No regime gate.               │   │ 9. Audit log                       │
│                                  │   └────────────────────────────────────┘
│ 5. Persist state per sleeve:     │
│    - eod_b shortlist + exits     │
│    - eod_t shortlist + exits     │
│    - regime (bull/bear)          │
│    - rebalance pending? (qtly)   │
└──────────────────────────────────┘
```

### State file: `~/.ato/ensemble_state.json`

```json
{
  "last_updated": "2026-04-25T15:35:00+05:30",
  "engine_commit": "fbcd36a",
  "champion_configs": {
    "eod_breakout":  "strategies/eod_breakout/config_champion.yaml",
    "eod_technical": "strategies/eod_technical/config_champion.yaml"
  },
  "ensemble_config":  "strategies/ensembles/eod_eodt_invvol_quarterly_full/config.yaml",
  "regime": {
    "instrument": "NSE:NIFTYBEES",
    "sma_period": 100,
    "today": "bull",
    "yesterday": "bull",
    "force_exit_pending": false
  },
  "weights_target":     {"eod_breakout": 0.56, "eod_technical": 0.44},
  "weights_actual":     {"eod_breakout": 0.553, "eod_technical": 0.447},
  "next_rebalance":     "2026-07-01",
  "sleeves": {
    "eod_breakout":  {"positions": [...], "pending_entries": [...], "pending_exits": [...]},
    "eod_technical": {"positions": [...], "pending_entries": [...], "pending_exits": [...]}
  }
}
```

---

## Quarterly rebalance procedure

Rebalance fires on the first NSE trading day of Jan/Apr/Jul/Oct. The
ensemble runner uses inverse-vol weights computed from each leg's
trailing-period vol; in production we use the SAME computation against
fresh data.

Steps:

1. **Compute target weights.** For each sleeve, compute trailing 1-year
   daily-return vol from sleeve's equity curve (or sleeve NAV history).
   `w_i ∝ 1 / vol_i`, normalized to sum 1.
2. **Compute current actual weights** from sleeve NAVs.
3. **Compute trades.** For each sleeve: `target_NAV - actual_NAV`. Positive
   → buy entries from sleeve's shortlist (or top up existing positions
   pro-rata). Negative → sell positions oldest-first until sleeve NAV
   reaches target.
4. **Place all rebalance trades as a single AMO batch** at next-day open.
5. **Update state file** with new weights and `next_rebalance` date.

**Tolerance band:** if `|w_actual - w_target| < 0.05` for both sleeves,
SKIP the rebalance entirely (no trades). This avoids friction for small
drifts that the next regime/exit event will erase anyway.

---

## Friction model

The backtest's ensemble Sharpe (1.281) assumes daily zero-cost rebalancing
implicit in averaging two equity curves. Real-world frictions:

| Source | Per occurrence | Frequency | Annual cost |
|---|---|---|---|
| Quarterly rebalance trades | ~10-30 bps on rebalanced amount | 4/yr | 5-10 bps/yr |
| Regime force-exit batch | ~15-25 bps slippage on whole eod_b sleeve | ~2/yr historically | 10-15 bps/yr |
| Signal-day entries (already in backtest) | ~5 bps | ~1/day across both | already modeled |
| Exit slippage (already in backtest) | ~5 bps | per exit | already modeled |
| **Total UNMODELED overhead** | | | **~15-25 bps/yr** |

So the live forward expectation is **~25 bps below the backtest CAGR** —
~18.54% instead of 18.79%. Sharpe degrades by maybe 0.02-0.04. Still
clearly above either solo. Still clearly above NIFTYBEES buy-and-hold
(10.45% CAGR / Sharpe 0.45).

---

## Risk limits

| Limit | Value | Action on breach |
|---|---|---|
| Max positions per sleeve | 15 | Skip new entries in that sleeve |
| Max positions total | 30 | Hard ceiling |
| Max per symbol (across sleeves) | 1 | Skip if held in either |
| Max sector | 4 per sector across BOTH sleeves combined | Skip if sector full |
| Min order value | ₹50,000 | Skip small orders |
| Max order value | ₹2,000,000 | Cap per position |
| Sleeve drawdown kill switch | -30% from sleeve peak | Pause that sleeve, alert |
| Total drawdown kill switch | -25% from portfolio peak | Close all, pause 30 days |
| Breadth gate (eod_b sleeve) | NIFTYBEES < SMA(100) | No new entries (force-exit if flipped today) |
| Engine drift check | `git rev-parse HEAD != fbcd36a` | Block new entries until verified |

---

## Implementation phases

### Phase 1 — Daily signal runners (per sleeve)

**Files:**
- `scripts/live_eod_breakout.py`
- `scripts/live_eod_technical.py`
- `scripts/live_ensemble_runner.py` — orchestrator that calls both, computes
  regime, decides force-exit, writes the unified state file.

Each per-sleeve runner invokes the engine signal generator the same way
`run.py` does, with `end_epoch = today_close`. It produces shortlists for
tomorrow's open and exit candidates.

**Key invariant:** live runners use the SAME signal generator the backtest
uses (`engine/signals/eod_breakout.py`, `engine/signals/eod_technical.py`).
Any re-implementation drifts and silently invalidates backtest expectations.

### Phase 2 — Order placement

**Module:** `ATO/ATO_UserUtil/strategy_integration/ensemble_orders.py`

1. Read ensemble state file.
2. Apply pre-trade checks per sleeve.
3. Place AMO BUY/SELL orders in a SINGLE BATCH (so the broker accepts them
   atomically and reconciliation isn't fragmented).
4. Audit-log all placements with broker order IDs and target/actual sleeve.

### Phase 3 — Reconciliation

1. Pull executed AMO fills from broker.
2. Tag each fill to its sleeve via the `entry_config_ids`-style tag in the
   audit log.
3. Update sleeve NAVs and state file.
4. Record daily P&L snapshot per sleeve and total.

### Phase 4 — Quarterly rebalance hook

`scripts/live_ensemble_runner.py` checks `next_rebalance` against today's
date. On the rebalance day, after Phase 1 produces normal entries/exits,
it adds the rebalance trade list to the AMO batch.

### Phase 5 — Paper trading validation (45 days, longer than solo)

The ensemble window must include at least one rebalance and ideally one
regime flip. 45 days covers ~one quarterly rebalance plus typical regime
flip frequency.

1. Run ensemble runner daily for 45 trading days.
2. **Reconciliation test:** for each day, also run the ensemble backtest
   over the same window — compare per-sleeve generated signals one-for-one.
3. Verify the regime gate fires correctly on at least one flip
   (synthetically force one if no natural flip occurred).
4. Verify the quarterly rebalance produces sensible trade lists.
5. Audit charges per sleeve against broker contract notes.

---

## Key files

| File | Action | Purpose |
|---|---|---|
| `scripts/live_ensemble_runner.py` | CREATE | Orchestrator |
| `scripts/live_eod_breakout.py` | CREATE | Per-sleeve daily signal runner |
| `scripts/live_eod_technical.py` | CREATE | Per-sleeve daily signal runner |
| `engine/signals/eod_breakout.py` | REUSE (no changes) | Signal logic |
| `engine/signals/eod_technical.py` | REUSE (no changes) | Signal logic |
| `lib/ensemble_curve.py` | REUSE | Same inverse-vol weight computation |
| `~/.ato/ensemble_state.json` | CREATE | Live state |
| `ATO_UserUtil/.../ensemble_orders.py` | CREATE | AMO placement |
| `TS_Scripts/.../pre_session_script.py` | MODIFY | Wire ensemble runner |
| `TS_Scripts/.../post_session_script.py` | MODIFY | Wire reconciliation |

---

## Open questions

1. **Capital allocation** — total account share for the ensemble?
   Suggested 50-70% given backtest robustness; 30-50% if mid-cap bull
   regime persistence is in doubt.
2. **Rebalance order type** — AMO market is the cleanest match to the
   backtest's MOC convention. Limits risk unfilled but bracket the
   slippage.
3. **Partial fills on rebalance** — handle pro-rata: if a sleeve is
   underfilled by 30%, defer the remainder to the next day rather than
   force-fill at a worse price.
4. **Corporate actions** — bonus/split handling for `peak_since_entry` and
   for sleeve NAV computation. NSE adjusts close prices, but state file
   needs explicit corp-action ingestion.
5. **Data source for live** — Kite historical API for execution-aligned
   prices, or CR API (matches backtest)? Use both: Kite for reconciliation,
   CR for signal regeneration.
6. **Sleeve NAV bootstrap** — first-day initialization splits capital per
   target weights. Subsequent days NAVs evolve from fills + market moves.
7. **Engine drift on a refit** — when one sleeve's champion is updated
   (e.g. eod_b post-2026-04-27 promotion), the ensemble weights shift.
   Decide: rebalance on the next quarterly date with new weights, or
   immediate rebalance? Quarterly is cheaper, immediate is more accurate.

---

## Cross-refs

- Ensemble math: [`ENSEMBLE_GUIDE.md`](ENSEMBLE_GUIDE.md)
- Champion configs:
  - [`strategies/eod_breakout/OPTIMIZATION.md`](../strategies/eod_breakout/OPTIMIZATION.md)
  - [`strategies/eod_technical/OPTIMIZATION.md`](../strategies/eod_technical/OPTIMIZATION.md)
- Engine baseline + protected files: [`STATUS.md`](STATUS.md)
- Why eod_technical did NOT get regime+holdout (negative result):
  [`strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md`](../strategies/eod_technical/REGIME_AND_HOLDOUT_2026-04-28.md)
- Pre-2019 vs post-2019 regime context:
  [`sessions/2026-04-24_pt2_handover.md`](sessions/2026-04-24_pt2_handover.md) §3

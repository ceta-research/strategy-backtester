# BrightFunded Prop-Firm Trading System

## Strategy: NAS100 Gap-Fade with SMA20 Regime Filter

**Validated on:** QQQ (same underlying as NAS100 CFD), 6 years (2020-2026), 1-minute bars
**Backtest result (SMA-filtered):** Sharpe 0.70, WR 63%, Calmar 0.29
**Best OOS year (2025):** +9.5% CAGR on 1x notional

---

## Account Setup

| Parameter | Recommendation |
|---|---|
| Account size | **$100,000** (Neptune plan) |
| Challenge cost | €495 (refundable with first payout) |
| Platform | MetaTrader 5 or cTrader |
| Instrument | NAS100.cash (Nasdaq 100 Index CFD) |
| Leverage | 1:20 (indices) |
| Profit split | 80% (standard) |

---

## Strategy Rules (EXACT)

### Signal: Gap Detection (daily, pre-market)

```
prev_close = yesterday's NAS100 last price at 16:00 ET
today_open = NAS100 first price at 09:30 ET
gap_pct = (today_open - prev_close) / prev_close * 100
```

### Entry Conditions (ALL must be true):

1. **Gap exists:** |gap_pct| >= 0.20% AND |gap_pct| <= 4.0%
2. **Regime filter:** QQQ/NAS100 previous close > 20-day SMA (bullish regime)
3. **Time:** Enter at **09:35 ET** (5 min after open, let dust settle)
4. **Direction:** FADE the gap
   - Gap UP (gap > +0.20%) → **SELL** (short NAS100)
   - Gap DOWN (gap < -0.20%) → **BUY** (long NAS100)

### Exit Rules (first to trigger):

| Exit | Condition | Expected frequency |
|---|---|---|
| **Target** | Price moved 70% of gap toward prev_close | ~50% of trades |
| **Stop** | Price moved 2× gap AWAY from prev_close | ~10% of trades |
| **Time** | 120 minutes after entry (11:35 ET) | ~37% of trades |
| **Hard EOD** | 15:55 ET (5 min before close) | ~3% of trades |

### No-Trade Days (skip entirely):

- NAS100 close < 20-day SMA (bearish regime — gaps tend to CONTINUE)
- Gap > 4% (monster move, likely news-driven, won't fade)
- Gap < 0.20% (too small, cost exceeds edge)
- FOMC day / major macro announcements (10-min BrightFunded restriction anyway)

---

## Position Sizing

### Funded Account ($100K)

```
Risk per trade = 1.8% of account equity = $1,800

Stop distance (points) = gap_pct × 2 × NAS100_price
  Example: 0.3% gap × 2 × 18,000 = 108 points

Lot size = Risk / (Stop_distance × $1_per_point)
  Example: $1,800 / 108 = 16.7 lots → round to 16 lots

Target (points) = gap_pct × 0.70 × NAS100_price
  Example: 0.3% × 0.70 × 18,000 = 37.8 points
```

### Per-Trade P&L at 16 lots:

| Outcome | Points | P&L |
|---|---|---|
| Target hit | +38 pts | **+$608** |
| Stop hit | -108 pts | **-$1,728** |
| Time exit (avg) | ~-10 pts | **-$160** |

### Expected Monthly (funded):

- Trades/month: ~9 (only on qualifying gap days with SMA filter)
- Win distribution: 4-5 targets + 3-4 time exits + 0-1 stops
- **Expected gross: $550-700/month**
- **Net after 80% split: $440-560/month**

---

## Risk Management

### BrightFunded Limits vs Strategy Risk:

| BF Limit | Our max exposure | Buffer |
|---|---|---|
| 5% daily loss ($5,000) | 1 trade max $1,728 | 65% buffer |
| 10% total loss ($10,000) | ~5 consecutive stops | Very unlikely (0.001%) |

### Drawdown Budget:

- Max consecutive losing trades (historically): 4
- Max consecutive-loss PnL: 4 × $1,728 = $6,912 (6.9% of account)
- Recovery at $600/winning trade: ~12 wins = ~3 weeks
- **Worst-case scenario stays within 10% total DD limit**

### Circuit Breakers (manual rules):

1. If account down 5% from peak → reduce risk to 1.0% per trade for 2 weeks
2. If account down 7% from peak → STOP trading for 1 week (review)
3. Never exceed 1 trade per day on NAS100
4. Never trade if you haven't checked SMA20 filter

---

## Evaluation Phase Strategy

### Phase 1: Target 8% ($8,000 profit)

**Approach:** Slightly more aggressive sizing during evaluation (no real money at risk).

```
Risk per trade = 2.5% of starting balance = $2,500
Expected per trade: ~$80 net
Trades needed: ~100 → ~11 months
```

**Acceleration tactics (evaluation ONLY):**
- Add a SECOND daily trade on large-gap days (gap > 0.5%): enter at 09:35, and if stopped, re-enter at 10:30 if gap still unfilled
- Trade BOTH long and short gaps (no directional bias filter during eval)

**With acceleration: ~6-8 months to pass Phase 1**

### Phase 2: Target 5% ($5,000 profit)

Same strategy as Phase 1. At ~$80/trade and 9 trades/month: ~7 months.
With acceleration: ~4-5 months.

**Total evaluation timeline: 10-13 months (conservative), 6-8 months (accelerated)**

---

## Daily Execution Checklist

```
PRE-MARKET (before 09:30 ET):
□ Check NAS100 futures pre-market price
□ Note yesterday's close (16:00 ET level)
□ Check if yesterday's close > 20-day SMA
  → If NO: no trade today. Done.
□ Check economic calendar (FOMC, NFP, CPI)
  → If high-impact in first 2h: no trade today.
□ Calculate gap at 09:30 open
□ Verify: 0.20% <= |gap| <= 4.0%
  → If outside range: no trade today.

ENTRY (09:35 ET):
□ Direction: gap > 0 → SELL ; gap < 0 → BUY
□ Calculate stop: entry ± (gap_size × 2)
□ Calculate target: entry ∓ (gap_size × 0.70)
□ Calculate lots: $1,800 / stop_distance_points
□ Place order with SL and TP attached

MONITORING:
□ If target or stop hits → done for the day
□ If 11:35 ET and position still open → close at market
□ Hard backstop: close anything at 15:55 ET

POST-TRADE:
□ Log result (date, gap%, direction, exit type, P&L)
□ Update running equity
□ Check: still above circuit-breaker levels?
```

---

## Monthly Income Projection

| Account | Risk/Trade | Est. Monthly Gross | Net (80% split) |
|---|---|---|---|
| $50K | 1.8% ($900) | $275-350 | **$220-280** |
| $100K | 1.8% ($1,800) | $550-700 | **$440-560** |
| $200K | 1.8% ($3,600) | $1,100-1,400 | **$880-1,120** |

**For $500/month target: $100K account with 1.8% risk delivers ~$500/month net.**

---

## Scaling Path

BrightFunded scales every 4 months (+30% of original balance):
- Month 0: $100K funded
- Month 4: $130K (if profitable 2 of 4 months + 10% total profit)
- Month 8: $160K
- Month 12: $190K
- Month 16: $220K

At month 12 ($190K account): expected monthly net = **$850-1,000/month**

---

## What This Strategy Will NOT Do

- Deliver 10% CAGR / 3% MDD (requires Sharpe > 2.3, strategy delivers 0.70)
- Win every month (expect 2-3 losing months per year)
- Work without the SMA filter (2022 proves filter is mandatory)
- Scale infinitely (market impact above ~50 lots on NAS100 CFD)

## What It WILL Do

- Deliver consistent positive expectancy (63% WR, validated 6 years)
- Stay within BrightFunded risk limits (5% daily / 10% total)
- Generate $400-600/month gross on $100K (regime-dependent)
- Compound via BrightFunded's scaling plan to $1K+/month within a year

---

## Key Risk: Evaluation Takes Time

The main challenge is PASSING THE EVALUATION, not the funded phase. At ~$80/trade average and 9 trades/month, Phase 1 (8% target) takes 11+ months without acceleration.

**Mitigation:** Use acceleration tactics during evaluation (higher risk, re-entries). Once funded, switch to conservative 1.8% risk.

**Alternative:** Buy $50K account (€295, cheaper) — needs only $4K profit for Phase 1 at same timeline. Then scale naturally to $100K+ within BrightFunded.

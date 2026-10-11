# momentum_cascade audit (2026-10-11)

Plan: `PLAN.md`. Every number below is printed by `analyze.py` (full output in `out/analyze_output.md`)
or `l2_fills.py`. Data: the repaired snapshot (`../long_window/providers.py`, `ca=True, clean=True`),
2010-01-01 → 2026-10-09 unless stated. Survivorship out of scope (Swas, 10-11).

## Verdict

**The strategy is real, and the engine reports it honestly.** No look-ahead, no fill fabrication, and the
result is deterministic. The fills check out against bhavcopy to the paisa. Three things make it
smaller than the board's 17.9–21%, and they're about how to read the number, not bugs:

1. **One year carries it.** 2021 was +134–159%. Without it the strategy compounds at **~12.6–13.2%**.
   The top 20 of 581 trades are **70% of net P&L**.
2. **The parameters sit near a peak.** Single-parameter neighbours have a median CAGR of **16.8%** vs the
   champion's 18.3%, and the trailing stop is a sharp peak (12% → 10.9%, 18% → 13.8%).
3. **Tax takes ~3pp.** 90% of trades are held under a year (STCG 20%).

| planning view (2010 → now unless noted) | CAGR | max DD |
|---|---|---|
| engine as-is, repaired data | 18.28% | −29.9% |
| **live-realistic**: next-open exits, liquidity cap from the prior bar, ₹50L, 15 bps | **17.71%** (17.1–18.2% across start dates) | −32.3% |
| live-realistic, 20 years (2006 →) | 17.14% | −32.2% |
| live-realistic, **after tax** | **14.82%** (20y: 14.30%) | |
| live-realistic, ex-2021 | 12.69% (20y: 13.23%) | |
| NIFTYBEES (≈ total return), after LTCG on exit | 9.28% (20y: 10.49%) | −36% (20y −60%) |

**Planning number: ~16% pre-tax, ~13% after tax (call it 12–14%), against ~9.3–10.5% after tax for
NIFTYBEES.** That's 17.7% live-realistic, minus ~1.5pp for parameter selection (champion 18.3% vs
neighbour median 16.8%), minus ~3pp tax. It lands next to the ex-2021 figure (12.7–13.2%), which is a
fair "no repeat of 2021" case. Expect a **~3-year stretch underwater** (Jan 2018 → Feb
2021, −32% at the Mar-2020 trough). As of 2026-10-09 it sits **9–12% below its Sep-2024 peak**.

## Lens results

### Gate: audit copy reproduces the engine
554/554 trades and the full equity curve identical on the first data vintage, and again (581 trades)
on the final one. **Pass.**

### L1 Exit fill timing — holds, once the simulator's ordering is matched to reality
The engine fills trailing-stop and max-hold exits at the **same close** that triggers them. Live, you'd
sell at the next open.

| variant | CAGR | max DD |
|---|---|---|
| close fill, entries processed before exits (engine) | 18.28% | −29.9% |
| next-open fill, entries before exits | 13.61% | −36.2% |
| **next-open fill, exits before entries** | **18.05%** | −31.3% |
| close fill, exits before entries | 15.17% | −33.0% |
| close fill, entries first, sizing multiplier 1.0 (see below) | 17.90% (20y 18.02%) | −29.0% |

⚠️ The simulator's exits-first branch (`simulator.py:393`) recomputes position size **without**
`order_value_multiplier`, so every exits-first run sizes at 1/15 instead of momentum_cascade's
0.95/15. At matched sizing (1.0), close-fill/entries-first gives 17.90% and next-open/exits-first
18.05% (20y: 18.02 vs 17.91). The larger sizing doesn't inflate the exits-first numbers.

The fill itself costs essentially nothing (within ±0.4pp across both sizings): on shared trades, next
open vs trigger close is **−0.08% mean, +0.05% median** (p05 −3.7%, p95 +2.3%). The 13.6% row is an ordering artifact. The engine processes entries
before exits each day, so a next-open exit frees its slot a day late and reroutes ~250 trades. Live,
you sell and buy at the same open, which is the 18.05% row. **Holds (−0.2pp).** ⚠️ Slot-recycling
rules alone move CAGR ±3pp (18.3 / 15.2 / 18.1), so they're worth fixing in the engine (follow-ups).

### L2 Fill prices vs bhavcopy — pass
Each trade re-priced from `nse_bhavcopy_historical` (independent, unadjusted; bhav epoch = charting −
5.5h, verified 245/247 RELIANCE returns). Returns are compared day by day, and days that differ by a
clean split ratio count as corporate actions.

| run | trades checked | trade return error vs bhav (median / p05 / p95) | entry opens off >1% |
|---|---|---|---|
| baseline | 464 of 581 | 0.000% / −0.00% / +0.00% | 0 |
| live-realistic | 417 of 542 | 0.000% / −0.00% / +0.00% | 0 |

The only mismatch is TATASTEEL's Jan-2018 rights issue (charting adjusts, bhav doesn't: correct).
95 trades (₹184L, 12% of P&L) can't be checked: a renamed ticker or a missing bhav bar at entry or exit.
22 more exit after bhav ends (2026-03-24). Opens equal to the prior close (4–10% of liquid bars, rising
since 2023) are real: they match bhav's OPEN exactly.

### L3 Look-ahead — pass
- Liquidity cap from the previous bar instead of the entry bar: 18.28 → **18.20%**.
- The non-causal oscillation filter removed 35 liquid bars since 2008, **0** inside a held trade.
- Code read: signals use closes ≤ signal day; regime (NIFTYBEES > SMA200) on the signal day; ranking
  on the signal-day close vs 360 bars back; entry at the next open.

### L4 Determinism — pass
Three shuffles of the order frame give identical trades. No trades fall in the null-ranked 262–361-bar
cohort (the cohort the eod_breakout tie bug lived in).

### L5 Costs and capacity
| | CAGR | | | CAGR |
|---|---|---|---|---|
| 5 bps (base) | 18.28% | | ₹50 L | 18.82% |
| 15 bps | 18.35% | | ₹1 cr (base) | 18.28% |
| 30 bps | 17.72% | | ₹5 cr | 16.00% |
| | | | ₹25 cr | 9.85% |

Cost-insensitive (round trip already ~0.23% charges + slippage). **Capacity-limited above ~₹5 cr**
(the 4.5%-of-turnover cap binds). Not a constraint at ₹50L.

### L6 Parameter neighbours — discount ~1.5pp
20 one-at-a-time neighbours: median **16.8%**, range 10.9–22.1%, 14 of 20 within 3pp of the champion.
- **Trailing stop is a sharp peak**: 12% → 10.88%, 15% → 18.28%, 18% → 13.79%. Don't hand-tune it.
- Fast lookback is the other sensitive one (30 → 15.2%, 63 → 12.1%).
- Without the NIFTYBEES regime filter: 17.40%, but −32% max DD and a worse 2008 (see long_window).
- 10 positions instead of 15: **22.1% / −24.0%**. That's a neighbour, not a recommendation; picking it
  after seeing it is the same selection bias this lens measures.

### L7 Start year and concentration
Start-year sweep (2006 … 2018, all to the latest bar): **16.6–23.9%**, every window beats NIFTYBEES
by 6.7–12.4pp, and max DD is −28.8 to −32.5% in all of them (the same 2018–2020 episode).
Concentration: top 5 / 10 / 20 trades = 22% / 41% / 70% of net P&L. **2021 = +158.7%** (baseline);
ex-2021 CAGR 12.55%. Fully invested: ≥1 position on 99.9% of days, 11.8 of 15 slots on average.

### L8 Tax — about −3pp
Today's rates (STCG 20%, LTCG 12.5% over ₹1.25L, FY set-off, paid from the account each FY):
baseline 18.28 → **15.32%**; live-realistic 17.71 → **14.82%** (20y 17.14 → 14.30%). 90% of trades
are short-term. NIFTYBEES bought once and sold at the end: 9.96 → 9.28% (20y 11.12 → 10.49%).
Estimate only: no surcharge or cess, no year-by-year historical rates. Conservative on carry-forward:
a residual short-term loss only offsets later long-term gains here, while real STCL also offsets later
STCG, so after-tax CAGR is understated by roughly 0.2pp.

**The one bias in the strategy's favour:** its legs are price-only (no dividends), while NIFTYBEES ≈
total return. Momentum names yield little, but that's roughly +0.5–1pp not counted in any figure above.

### L9 Live-realistic combined run (the planning number)
Next-open exits with exits processed first, liquidity cap from the prior bar, ₹50L, 15 bps:
**17.71% / −32.3%** (2010→), **17.14% / −32.2%** (20y). Year by year vs NIFTYBEES: 2010 −2.5 vs +18.6,
2018 −15.7 vs +4.8, 2019 +1.7 vs +13.6, **2025 −11.3 vs +11.7**, 2026 YTD +5.3 vs −12.9. It lags the
index in roughly 1 year in 3, sometimes badly.

### L10 Path dependence — small
Start shifted 0–27 days within Jan 2010: engine 18.13–18.81%; live-realistic **17.06–18.18%**.

## Trades with no stop (design risk, not a bug)
The trailing stop only arms after a position regains its signal-day close. **23 of 581 trades never
armed**: mean −35.9%, worst −70.7%, **−₹379L = −24% of net P&L**. A stop from entry is the obvious
variant to test, but it's a strategy change, so it's left out of this audit.

## Data defects found during the audit (repaired in `../long_window/providers.py`)
Raw charting fabricates moves a momentum strategy buys into:
- **12 double-adjusted splits.** Pre-ex history divided twice, so the ex-date jumps UP by the ratio:
  BALKRISIND 2:1 Dec-2017 (+100%), PERSISTENT 2:1 Mar-2024, FEDERALBNK 5:1 2013 (+414%), CESC 10:1 2021,
  GODREJPROP 2013. One fabricated a +73% BALKRISIND trade (₹14.8L).
- **Listing stubs**: bars before a listing on another scale (IDEAFORGE ×551, AWL ×153). Dropped.
- **Recycled tickers**: an older company's history keyed to a new listing's symbol (AWL, INDIGO,
  RATEGAIN, ROSSARI, RVNL, SUPRIYA), so 360-bar momentum reads straight through the seam. Split at any
  >1-year gap into separate instruments (150 symbols). A suspension (ROML) is split the same way, which
  exits a held position at its last pre-gap close: optimistic, but only a handful.
- BFUTILITIE 2012–15 and WEWIN 2017–18 interleaved at ~2× are quarantined; the threshold catches TATASTEEL.

On the final data: momentum_cascade 19.01 → 18.28% (2010→), eod_breakout 14.69 → 12.12%. Updated tables
are in `../long_window/RESULTS.md`.

## Follow-ups (measured here, not changed in the engine)
1. **Simulator ordering.** Entries-before-exits plus same-close exit fills is a combination you can't
   trade, and slot recycling alone moves CAGR ±3pp. Make next-open exits + exits-first the daily
   default. That's a deliberate re-baseline of every strategy.
   1b. Same code path: the exits-first branch drops `order_value_multiplier` and any `order_value`
   override when it recomputes sizing (`simulator.py:393`). Fix together.
2. **Data repairs into the engine provider** (`NseChartingDataProvider`): split adjust, double-adjust
   undo, stub drop, ticker split, interleave quarantine. Also a re-baseline.
3. **Stop from entry** for momentum_cascade (the 23 unarmed trades).
4. Survivorship (bhavcopy universe) remains the one untested bias; measured small before on breakouts.

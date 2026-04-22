# eod_breakout — regime filter port + parameter sweep (2026-04-22)

**Context:** after retiring `momentum_dip_quality` and blocking the two
momentum strategies, eod_breakout is the remaining ATO_Simulator-aligned
candidate for the "20-30% CAGR" target. Ran on full NSE via nse_charting
(2010-2026, 2454 instruments).

## Published champion baseline (from config_champion.yaml)

- ndh=7, ndm=5, ds={3, 0.54}, tsl=8, pos=15
- Documented: 13.3% CAGR, -25.7% MDD, Calmar 0.516, Sharpe 0.93
- **Reproduced today: 15.2% CAGR, -34.1% MDD, Calmar 0.45, Sharpe 0.70**
- Drift: CAGR +1.9pp but MDD -8.4pp worse. Likely post-audit charge schedule
  + determinism fixes shifted trade timing. Not investigated here.

## Parameter sweep results (72 configs, 2 passes)

### Sweep 1 — ndh × ndm × tsl × pos × ds (48 configs)

Best CAGR: **ndh=5, ndm=5, ds={3, 0.54}, tsl=18, pos=20 → 19.1% CAGR, -37.6% MDD, Calmar 0.51**

Best Calmar: **ndh=5, ndm=5, ds={5, 0.60}, tsl=12, pos=20 → 19.0% CAGR, -29.9% MDD, Calmar 0.636**

Key findings:
- ndh=3 underperforms ndh=5 across the board
- ds={5, 0.60} slightly beats ds={3, 0.54} on risk-adj
- tsl=12 ≥ tsl=15 for Calmar, tsl=18 wins raw CAGR
- pos=20 > pos=25 (concentration beats diversification here)

### Sweep 2 — longer breakout windows (24 configs)

Probed ndh ∈ {7, 10, 15, 20} × ndm ∈ {5, 10} × ds.score ∈ {0.60, 0.65, 0.70}.
No config exceeded sweep-1 winner. **ndh=5 is the local maximum.**

## Regime filter port (additive, default disabled)

Added `regime_instrument`, `regime_sma_period`, `force_exit_on_regime_flip`
to eod_breakout's entry config. Empty defaults preserve existing configs
byte-identical.

### Option (i): entries-only gate

- Sweep-1 winner + regime NIFTYBEES > SMA200 → **CAGR 14.5%, MDD -27.8%, Calmar 0.52**
- vs baseline: **-4.5pp CAGR**, +2.1pp MDD. Bad trade.
- Why: breakouts are already a "strength" signal; redundant with direction_score gate.

### Option (ii): entries + force-exit on regime flip

- Sweep-1 winner + regime + force-exit → **CAGR 14.8%, MDD -24.2%, Calmar 0.61**
- vs baseline: **-4.2pp CAGR**, **-5.7pp MDD** (drawdown improvement). Real tradeoff.
- vs option (i): +0.3pp CAGR AND -3.6pp MDD. (ii) strictly dominates (i).
- Calmar nearly matches the no-regime baseline (0.61 vs 0.64) with much
  better drawdown control.

## Champion candidates

| Variant | Params | CAGR | MDD | Calmar | Notes |
|---|---|---:|---:|---:|---|
| Max CAGR | ndh=5, ndm=5, ds=0.60, tsl=12, pos=20, no regime | 19.0% | -29.9% | 0.64 | Sweep in-sample peak |
| Max Calmar | same + regime (ii) NIFTYBEES SMA200 | 14.8% | -24.2% | 0.61 | Drawdown-controlled |
| Published (current) | ndh=7, ndm=5, ds=0.54, tsl=8, pos=15 | 15.2% | -34.1% | 0.45 | Drifted from 13.3% |

## Honesty caveats (do NOT treat 19% as production)

1. **Multiple-comparison bias.** 72 configs swept; "best CAGR" is an
   in-sample peak. Rough OOS shrinkage 15-25%. Expected OOS: ~14-16%.
2. **No walk-forward validation.** The published 13.3% was validated
   6/6 folds (avg Calmar 0.736). Our 19% has no such anchor.
3. **Charge schedule uplift.** Phase 3 revisit lowered NSE_EXCHANGE_RATE
   (0.0000345 → 0.0000297). ~0.5-1pp CAGR contribution from cheaper fees,
   not signal improvement.
4. **3 orders with >2000% returns flagged** by sanitize diagnostic but not
   dropped. If any contribute meaningfully to the 19%, it's data-quality
   noise, not alpha.

## Next steps (deferred)

- Walk-forward validation on both candidates (~15 min compute) before
  promoting either to published champion.
- Investigate the MDD -34% vs published -25.7% drift on the existing champion
  (likely audit-legitimate, but needs root-cause before the old config is retired).
- Clean up the 3 outlier trades flagged by sanitize (data quality).

## Key observation for other strategies

Regime filter option (i) **hurts breakouts** because the signal itself is
a strength filter; adding "market must also be strong" filters out the
best setups (post-pullback individual breakouts).

Regime option (ii) — entries gated + force-exit on flip — is the more
sensible pattern for trend-following strategies: it cuts drawdowns without
starving the entry channel.

Dip-buy / mean-reversion strategies (e.g. dip_quality) benefit from
option (i) because their signal is buying weakness, which genuinely needs
a regime gate to avoid catching falling knives.

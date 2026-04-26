# Phase 8A — Bias Impact Measurements

**Created:** 2026-04-21
**Scope:** A/B measurements for the three P1-flagged biased strategies.
**Harness:** `scripts/measure_bias_impact.py`
**Output format:** one `<strategy>.md` + `<strategy>.json` per run.

## Goal

Each of the three strategies gained a new opt-in flag in Phase 8A that
toggles between the legacy (biased) behavior and an honest fix:

| Strategy | Flag | Legacy (default) | Honest |
|----------|------|------------------|--------|
| `momentum_rebalance` | `entry.moc_signal_lag_days` | `0` (same-bar entry) | `1` (T-1 signal, T close execution) |
| `momentum_top_gainers` | `entry.universe_mode` | `"full_period"` | `"point_in_time"` |
| `momentum_dip_quality` | `entry.universe_mode` | `"full_period"` | `"point_in_time"` |

Default stays legacy so pre-audit results remain reproducible. Honest
mode is opt-in; users add the flag to their YAML explicitly. The
measurement harness flips the flag per-pass to produce a side-by-side
delta.

## Results: local parquet fixture (NSE, 30 blue-chips, 2020-2021)

| Strategy | Legacy CAGR | Honest CAGR | ΔCAGR | Category | Action |
|----------|------------:|------------:|------:|----------|--------|
| `momentum_rebalance` | **+12.13%** | **+6.59%** | **-5.54pp** | LOAD-BEARING | Retire or invert |
| `momentum_top_gainers` | +25.13% | +25.13% | +0.00pp | UNMEASURABLE on fixture | Re-run on full universe |
| `momentum_dip_quality` | +30.88% | +30.88% | +0.00pp | UNMEASURABLE on fixture | Re-run on full universe |

## Results: full NSE universe via nse_charting (2010-2026)

**momentum_dip_quality named champion — `config_nse_champion.yaml`:**

| Metric | Legacy (full_period) | Honest (point_in_time) | Δ |
|--------|---------------------:|-----------------------:|----:|
| CAGR | **+22.71%** | **+5.08%** | **−17.63pp** |
| Total Return | +2660% | +123% | −2537pp |
| Max Drawdown | −41.16% | −35.59% | +5.56pp |
| Calmar | 0.552 | 0.143 | −0.41 |
| Sharpe | 1.20 | 0.19 | −1.00 |
| Win rate | 70.71% | 62.67% | −8.04pp |
| Total trades | 297 | 225 | −72 |

2454 instruments fetched, 887 in period-average universe, avg 36 per
rebalance after quality + regime filters. Run time: 56.8s legacy +
62.4s honest on local compute.

**Verdict:** the champion's 17.2%/22.71% CAGR was almost entirely the
look-ahead bias. Honest CAGR (5.08%) is lower than a naive buy-and-hold
Nifty. This strategy is in the "retire or invert" zone.

Same-day Dip Quality figures published anywhere should be treated as
INVALID pending an honest re-optimization.

### Why the 0pp deltas are misleading

`momentum_top_gainers` and `momentum_dip_quality` carry a
FULL-PERIOD UNIVERSE bias — a stock liquid in 2020 gets included in
the 2015 universe. The bias shows up only when the universe has
cross-section variation in liquidity over time.

Our local parquet fixture (`~/ATO_DATA/tick_data`, 30 blue-chip NSE
stocks 2019-2021) has all stocks liquid throughout the window, so
`full_period` and `point_in_time` universes are identical — hence
the zero delta. This is a fixture-size artifact, not a bias-free
strategy.

**Re-run these on the full FMP universe** via
`--provider cr` (requires CR_API_KEY):

```bash
python scripts/measure_bias_impact.py momentum_top_gainers \
  strategies/momentum_top_gainers/config_champion.yaml --provider cr

python scripts/measure_bias_impact.py momentum_dip_quality \
  strategies/momentum_dip_quality/config_champion_r3.yaml --provider cr
```

Expected deltas (educated guess): 4-10pp CAGR reduction. The
magnitude depends on how much the period-average universe
differs from the point-in-time universe, which is largest when
the NSE F&O inclusion list changed frequently in the 2015-2020
period.

### `momentum_rebalance` — load-bearing bias confirmed

The same-bar entry bias costs **5.54pp CAGR** (46% relative) and
cuts Calmar by 63% on this small NSE fixture. Full results:

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +12.13% | +6.59% | -5.54pp |
| Total Return | +25.70% | +13.60% | -12.10pp |
| Max Drawdown | -9.04% | -13.14% | -4.10pp |
| Calmar | +1.34 | +0.50 | -0.84 |
| Sharpe | +1.41 | +0.55 | -0.87 |
| Win rate | +61.78% | +58.44% | -3.34pp |

This falls into the "retire or invert" decision-guide category.
Recommendations:

1. **Flip the default** to `moc_signal_lag_days=1` once any consumers
   of the strategy are migrated. Legacy opt-in stays available for
   exact reproducibility of published results.
2. **Do not publish the legacy numbers** as if they represent a
   live-tradable strategy. Any blog / video referencing momentum_rebalance
   CAGR must use the honest number.
3. **Optional inversion study:** the legacy strategy looks like it's
   capturing close-to-close mean reversion (top gainer *on T's close*
   falls slightly relative to next rebal), not true momentum. Invert
   the rank to see whether there's a genuine counter-trend edge.

## Decision guide (copied from harness reports)

- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to honest and re-run
  optimization once; move on.
- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).
- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.

## Next steps

1. **Run `--provider cr`** for the two universe-bias strategies to get
   real deltas. ~30-60 min per strategy over the full NSE universe.
2. **Update OPTIMIZATION_QUEUE.yaml** based on deltas:
   - If `momentum_rebalance` is retired, remove it from the queue.
   - If the universe strategies show >2pp delta, re-run their R2/R3
     rounds with `universe_mode: "point_in_time"`.
3. **Flip flag defaults** to honest mode once all consumers acknowledge
   the change and results are re-published.
4. **Update publishing order** to block any momentum_rebalance blog
   post until legacy-vs-honest is resolved.

## Caveats

- The local-parquet fixture is small (30 symbols, 2 years). Take
  absolute numbers as directional, not publication-ready.
- The point-in-time universe uses `date_epoch < rebalance_epoch`
  (strictly less-than) to prevent same-day look-ahead.
- Charges, slippage, and simulator config are identical across
  the legacy/honest passes — the only difference is the flag value.

## Files in this directory

- `README.md` (this file) — summary + decision guide.
- `momentum_rebalance.md` / `.json` — A/B measurement output.
- `momentum_top_gainers.md` / `.json` — A/B measurement output (fixture-limited).
- `momentum_dip_quality.md` / `.json` — A/B measurement output (fixture-limited).

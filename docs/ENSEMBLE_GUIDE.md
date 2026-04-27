# Ensemble Guide

Combine N strategy equity curves into a single portfolio.

**Quick start:**
```bash
python scripts/run_ensemble.py \
  --ensemble strategies/ensembles/eod_lowpe_5050/config.yaml \
  --output results/ensembles/eod_lowpe_5050.json
```

**Code:** `lib/ensemble_curve.py`, `scripts/run_ensemble.py`
**Tests:** `tests/test_ensemble_curve.py` (32 tests, run `python -m unittest tests.test_ensemble_curve`)

---

## When to use this

- You have ≥2 strategies that are individually profitable but with different mechanisms (momentum vs value, trend vs mean-reversion).
- Their daily-return correlation is below ~0.7. Lower is better; below 0.6 typically yields a Sharpe lift.
- You want lower drawdowns than any single strategy without giving up too much CAGR.

**Don't use this** for stacking parameter variants of the same strategy — they'll be highly correlated and the diversification benefit will be near-zero.

---

## Config schema

`strategies/ensembles/{name}/config.yaml`:

```yaml
ensemble:
  name: <ensemble_name>              # required, free text
  description: <free text>           # optional
  starting_capital: 10000000         # required, notional
  alignment: intersection            # only mode supported
  rebalance: none                    # none | monthly | quarterly | annual
  weighting: fixed                   # fixed | inverse_vol | risk_parity
  weight_lookback_days: null         # null = full window (in-sample), int = trailing N

  legs:                              # >= 2 legs required
    - name: <leg display name>
      weight: 0.5                    # required when weighting=fixed; ignored otherwise
      result_path: results/.../something.json   # path to a result JSON
      rank: 1                        # default 1 (top config in a sweep result)
      # OR: params_match: {pe_max: 10, ...}     # find detailed[i] matching these params
```

### Leg-source modes

| Mode | Status | When to use |
|---|---|---|
| `result_path` + `rank` | Phase 1 (shipped) | Pre-computed results; default `rank: 1` picks top config |
| `result_path` + `params_match` | Phase 1 (shipped) | Pin a specific config by params dict (e.g. for reproducibility) |
| `config_path` (rerun mode) | Phase 2+ (deferred) | Auto-rerun the underlying strategy if cache stale |

### Weighting modes

| Mode | What | Phase |
|---|---|---|
| `fixed` | Use `weight:` from each leg | 1 |
| `inverse_vol` | `w_i ∝ 1 / vol_i`. Equivalent to risk-parity in the 2-leg case. | 3 |
| `risk_parity` | Equal-risk-contribution; iterative ERC solver. NotImplementedError today. | 3.5 (deferred) |

### Rebalancing

| Mode | What |
|---|---|
| `none` | Set-and-forget. Winning leg's effective weight grows over time. |
| `monthly` | Reset to target weights at the first day of each month. |
| `quarterly` | Quarter boundaries (Jan/Apr/Jul/Oct). |
| `annual` | First day of each calendar year. |

Rebalancing is **frictionless** in this runner. Real-world friction estimates are auto-emitted as warnings in the output JSON (~70bps/yr monthly, ~25bps/yr quarterly, ~7bps/yr annual on NSE STT 0.1%).

---

## What the runner outputs

### stdout

1. **Header** with name, description, window, alignment, rebalance, weighting.
2. **Leg leaderboard**: name, weight, CAGR, MDD, Calmar, Sharpe per leg + ensemble.
3. **Drawdown attribution**: peak/trough dates and per-leg DD share.
4. **Correlation matrix**: pairwise daily-return correlations.
5. **Sharpe sensitivity** (2-leg only): w1 ∈ [0,1] sweep with peak vs inverse-vol comparison.

### Output JSON

```yaml
type: ensemble
ensemble:
  name, description, starting_capital, alignment, rebalance, weighting,
  weight_lookback_days
  legs: [{name, weight, config_weight, result_path, rank, params_match,
          leg_summary}]
summary:                       # 17 metrics from compute_metrics_from_curve
  cagr, max_drawdown, calmar_ratio, sharpe_ratio, ...
  worst_year, peak_value, final_value
drawdown_attribution:
  peak_date, trough_date, ensemble_drawdown, duration_days
  legs: [{name, nav_at_peak, nav_at_trough, leg_return, contribution_to_dd}]
diagnostics:
  correlation_matrix: {labels, matrix}
  sharpe_sensitivity: {grid, peak_weights, peak_sharpe,
                        inverse_vol_weights, inverse_vol_sharpe}
equity_curve: [{epoch, value}, ...]
warnings: [string, ...]
```

The output is shape-distinct from single-strategy results (no per-trade list) but uses identical field names where they map. Discriminate via `type: "ensemble"`.

---

## Math contract

Each leg's result JSON provides an equity curve `v_i[t]`. The runner:

1. **Aligns** all leg curves by epoch intersection. All legs must share `equity_curve_frequency` (default `DAILY_CALENDAR`).
2. **Treats each leg as a return stream** `v_i[t] / v_i[0]`. The underlying leg's `start_margin` is irrelevant; the ensemble rescales.
3. **Combines** via:
   ```
   ensemble_NAV[t] = starting_capital * Σ_i (weight_i * v_i[t] / v_i[0])
   ```
   With rebalancing, `weight_i` snaps back to target at each period boundary; between boundaries each leg compounds independently.

Drawdown attribution finds the peak-to-trough pair on `ensemble_NAV[]`, then decomposes:
```
contribution_i = (leg_NAV_i[trough] - leg_NAV_i[peak]) / ensemble_NAV[peak]
```
By construction, `Σ contribution_i = ensemble_drawdown`.

---

## Worked example: eod_breakout + low_pe (modern 2018-2026)

Configs in `strategies/ensembles/`:

| Config | Weighting | Rebalance | CAGR | MDD | Calmar | Sharpe |
|---|---|---|---:|---:|---:|---:|
| `eod_lowpe_5050` | fixed (0.5/0.5) | none | 19.44% | -21.34% | 0.911 | 1.380 |
| `eod_lowpe_5050_quarterly` | fixed (0.5/0.5) | quarterly | 18.65% | -17.19% | 1.085 | 1.399 |
| `eod_lowpe_invvol_quarterly` | inverse_vol (0.38/0.62) | quarterly | 17.18% | -14.60% | **1.177** | 1.365 |

Solo legs: eod_breakout 24.42% / -27.76% / Cal 0.880 / Sharpe 1.349. low_pe 12.26% / -12.08% / Cal 1.016 / Sharpe 1.002. Daily-return correlation: 0.532.

**Findings:**
- Set-and-forget biases optimistically: eod's effective weight drifts to ~70% by 2026, inflating both CAGR and MDD.
- Quarterly 50/50 is the honest "set the weights and rebalance" baseline.
- Inverse-vol gives the **best Calmar of any variant** (-14.60% MDD is 15% smaller than 50/50 quarterly), but slightly lower Sharpe because the higher-vol leg here also has the higher Sharpe.
- Sharpe sweep peak is at w1=0.60 (Sharpe 1.408), within 0.01 of 50/50 quarterly. The Sharpe surface is very flat near peak.
- Drawdown attribution shows the 2024-2025 drawdown was driven primarily by eod_breakout (50/50: -19.66 / -1.68; inv-vol qtr: -9.99 / -4.61).

---

## Known limitations (not bugs)

1. **`union_ffill` alignment not implemented.** Legs with non-overlapping windows force intersection truncation. If you ensemble a 2010-start leg with a 2018-start leg, the ensemble window is 2018+. Logged as a warning.

2. **`config_path` rerun mode not implemented.** Must use pre-computed `result_path` for now. Rerun mode would auto-invoke `run.py` on stale cache; deferred to keep Phase 1 focused.

3. **`risk_parity` weighting not implemented.** `inverse_vol` is the practical 2-leg approximation (identical when correlations equal). Iterative ERC solver is Phase 3.5.

4. **Weights are fixed for the entire backtest.** Per-rebalance adaptive weights (recompute inv-vol at each rebalance using a trailing window) is Phase 3.5.

5. **Frictionless rebalancing.** Real NSE STT (~0.1% per round-trip × leg-to-leg turnover) adds ~25bps/yr at quarterly. Subtract from realized CAGR for live deployment estimates.

6. **In-sample bias when `weight_lookback_days: null`.** Inverse-vol weights computed from the full curve "know" the entire backtest's variance. For honest forward estimates, set a trailing lookback and exclude the lookback period from evaluation.

7. **Compacted sweep results have empty `equity_curve`.** Loading such files raises a clear error; either re-run the source config or pick an uncompacted result.

8. **2-leg-only diagnostics.** `sharpe_sensitivity_2leg` only handles 2-leg ensembles. N-leg sensitivity would need either grid search (combinatorial) or a fixed-weight projection for N-2 legs.

---

## Files

```
lib/ensemble_curve.py                          # core math
scripts/run_ensemble.py                        # CLI runner
tests/test_ensemble_curve.py                   # 32 unit tests
strategies/ensembles/
  eod_lowpe_5050/config.yaml                   # set-and-forget 50/50
  eod_lowpe_5050_quarterly/config.yaml         # quarterly 50/50
  eod_lowpe_invvol_quarterly/config.yaml       # inverse-vol quarterly
results/ensembles/                             # output JSONs (gitignored)
docs/ENSEMBLE_GUIDE.md                         # this file
```

---

## Future work

- Phase 3.5: per-rebalance adaptive weighting; iterative ERC for risk_parity
- Phase 7: rerun mode (`config_path` legs) with cache invalidation on config mtime
- Phase 8: friction modeling per leg (rebalance trades cost something)
- Phase 9: union_ffill alignment for differently-windowed legs
- Phase 10: N-leg sharpe_sensitivity via Monte Carlo or constrained projection

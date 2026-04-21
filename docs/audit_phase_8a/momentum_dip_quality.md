# Phase 8A Bias Impact — momentum_dip_quality

- **Config:** `strategies/momentum_dip_quality/config_phase_8a_local.yaml`
- **Data provider:** `parquet`
- **Flag:** `entry.universe_mode` (legacy=full_period, honest=point_in_time)
- **Pipeline times:** legacy 0.1s · honest 0.2s

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +30.88% | +30.88% | +0.00pp |
| Total Return | +71.24% | +71.24% | +0.00pp |
| Max Drawdown | -8.44% | -8.44% | +0.00pp |
| Calmar | +3.6576 | +3.6576 | +0.0000 |
| Sharpe | +2.1867 | +2.1867 | +0.0000 |
| Total trades | 74 | 74 | — |
| Win rate | +77.03% | +77.03% | +0.00pp |

## Decision guide

- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to `honest` and
  re-run optimization once; move on.
- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).
- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.

# Phase 8A Bias Impact — momentum_top_gainers

- **Config:** `strategies/momentum_top_gainers/config_phase_8a_local.yaml`
- **Data provider:** `parquet`
- **Flag:** `entry.universe_mode` (legacy=full_period, honest=point_in_time)
- **Pipeline times:** legacy 0.1s · honest 0.1s

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +25.13% | +25.13% | +0.00pp |
| Total Return | +56.52% | +56.52% | +0.00pp |
| Max Drawdown | -17.98% | -17.98% | +0.00pp |
| Calmar | +1.3979 | +1.3979 | +0.0000 |
| Sharpe | +1.4112 | +1.4112 | +0.0000 |
| Total trades | 87 | 87 | — |
| Win rate | +57.47% | +57.47% | +0.00pp |

## Decision guide

- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to `honest` and
  re-run optimization once; move on.
- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).
- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.

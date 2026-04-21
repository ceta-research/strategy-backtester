# Phase 8A Bias Impact — momentum_dip_quality

- **Config:** `strategies/momentum_dip_quality/config_nse_champion.yaml`
- **Data provider:** `nse_charting`
- **Flag:** `entry.universe_mode` (legacy=full_period, honest=point_in_time)
- **Pipeline times:** legacy 56.8s · honest 62.4s

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +22.71% | +5.08% | -17.63pp |
| Total Return | +2660.20% | +123.41% | -2536.79pp |
| Max Drawdown | -41.16% | -35.59% | +5.56pp |
| Calmar | +0.5519 | +0.1428 | -0.4090 |
| Sharpe | +1.1972 | +0.1940 | -1.0031 |
| Total trades | 297 | 225 | — |
| Win rate | +70.71% | +62.67% | -8.04pp |

## Decision guide

- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to `honest` and
  re-run optimization once; move on.
- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).
- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.

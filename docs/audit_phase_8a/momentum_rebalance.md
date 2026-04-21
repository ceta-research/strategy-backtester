# Phase 8A Bias Impact — momentum_rebalance

- **Config:** `strategies/momentum_rebalance/config_phase_8a_local.yaml`
- **Data provider:** `parquet`
- **Flag:** `entry.moc_signal_lag_days` (legacy=0, honest=1)
- **Pipeline times:** legacy 0.1s · honest 0.1s

| Metric | Legacy | Honest | Delta |
|--------|-------:|-------:|------:|
| CAGR | +12.13% | +6.59% | -5.54pp |
| Total Return | +25.70% | +13.60% | -12.10pp |
| Max Drawdown | -9.04% | -13.14% | -4.10pp |
| Calmar | +1.3412 | +0.5015 | -0.8397 |
| Sharpe | +1.4148 | +0.5492 | -0.8656 |
| Total trades | 157 | 154 | — |
| Win rate | +61.78% | +58.44% | -3.34pp |

## Decision guide

- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to `honest` and
  re-run optimization once; move on.
- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).
- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.

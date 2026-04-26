# Phase 8A Bias Impact — momentum_rebalance (RE-RUN 2026-04-23)

## Finding: RETIRE

Two runs on full NSE (nse.nse_charting_day, 2010-01-01 to 2026-03-17):

### 1. Flag-bias A/B on a representative config

Single config: mom=126d, rebal=21d, num_positions=10, regime=NIFTYBEES>SMA200.

| Metric       | Legacy (moc=0) | Honest (moc=1) | Delta    |
|--------------|---------------:|---------------:|---------:|
| CAGR         | +4.45%         | +4.38%         | -0.07pp  |
| Total Return | +102.60%       | +100.37%       | -2.23pp  |
| Max Drawdown | -20.35%        | -18.39%        | +1.97pp  |
| Calmar       | +0.2187        | +0.2383        | +0.0195  |
| Sharpe       | +0.2718        | +0.2634        | -0.0085  |
| Total trades | 981            | 984            | —        |
| Win rate     | +50.97%        | +50.51%        | -0.46pp  |

**Decision guide says `|ΔCAGR| < 2pp` → cosmetic.** The local-fixture finding
of -5.54pp ΔCAGR was an artifact of the 30-stock 2020-2021 parquet (tight
correlations inflate the bias magnitude). On full 16-year NSE, moc=0 vs 1 is
a rounding error. **Default flipped to `moc_signal_lag_days=1` anyway** since
same-bar entry is still mechanically wrong; the cost is 0.07pp CAGR.

### 2. Full 27-config sweep on honest engine

Sweep: lookback {63, 126, 252} × rebalance {21, 42, 63} × positions {5, 10, 20}.

| Ranked by         | Config                    | CAGR   | MaxDD   | Calmar |
|-------------------|---------------------------|-------:|--------:|-------:|
| Best CAGR         | mom=252, rebal=63, pos=20 | 7.0%   | -40.8%  | 0.17   |
| 2nd best CAGR     | mom=252, rebal=21, pos=20 | 5.9%   | -31.7%  | 0.19   |
| 3rd best CAGR     | mom=126, rebal=63, pos=20 | 5.3%   | -43.0%  | 0.12   |
| Best Calmar       | mom=126, rebal=21, pos=5  | 3.2%   | -12.6%  | 0.254  |
| 2nd best Calmar   | mom=252, rebal=21, pos=5  | 2.7%   | -11.7%  | 0.230  |

No config clears NIFTYBEES buy-and-hold (~12% CAGR, 2010-2026).

## Decision

- **Status:** AUDIT_RETIRED.
- **Reason:** Honest cross-sectional momentum (Jegadeesh-Titman 12-1 style)
  on NSE is structurally below buy-and-hold. Best CAGR across 27 configs is
  7.0%, best Calmar 0.254 at 3.2% CAGR.
- **Flag:** `moc_signal_lag_days=1` is the new default. The local-fixture
  5.5pp delta was an artifact; on full data the flag's effect is 0.07pp.
- **Next steps:** none. Can be revived later only if paired with a stronger
  universe filter or a different quality/momentum blend.

# eod_breakout Optimization

**Strategy:** N-day high breakout + direction score filter + TSL exit
**Signal file:** `engine/signals/eod_breakout.py`
**Current best:** 12.3% CAGR (NSE). ATO_Simulator reached 32.5% — gap likely due to ranking differences.

## Parameters

### Entry (strategy-specific)

| Param | Description | Plausible range | Baseline |
|-------|------------|----------------|----------|
| `n_day_ma` | Close must be above N-day MA | 2-20 | 3 |
| `n_day_high` | Close must be >= N-day rolling high | 2-20 | 2 |
| `direction_score` | Compound: `{n_day_ma, score}`. Fraction of stocks above their MA | score: 0.30-0.70, n_day_ma: 2-10 | `{n_day_ma: 3, score: 0.54}` |

Note: `direction_score` is a single compound config param (list of dicts in YAML). To sweep, provide multiple dicts:
```yaml
direction_score:
  - {n_day_ma: 3, score: 0.40}
  - {n_day_ma: 3, score: 0.50}
  - {n_day_ma: 3, score: 0.60}
```

### Exit (shared priors)

| Param | Description | Plausible range | Baseline |
|-------|------------|----------------|----------|
| `trailing_stop_pct` | TSL from max price since entry | 3-50% | 15 |
| `min_hold_time_days` | Min days before TSL activates | 0-10 | 0 |

### Simulation

| Param | Description | Plausible range | Baseline |
|-------|------------|----------------|----------|
| `max_positions` | Max concurrent positions | 5-40 | 20 |
| `sorting_type` | Order ranking method | top_gainer, top_performer | top_gainer |

**Total params to explore:** 8

## Optimization Log

### Round 0: Baseline

| Config | CAGR | MDD | Calmar | Trades | Notes |
|--------|------|-----|--------|--------|-------|
| _not yet run_ | | | | | |

**Baseline config:** `config_baseline.yaml`

### Round 1: Sensitivity Scan

| Param | Values swept | Shape | Classification | Best value | Notes |
|-------|-------------|-------|---------------|------------|-------|
| `n_day_ma` | _pending_ | | | | |
| `n_day_high` | _pending_ | | | | |
| `direction_score` (score) | _pending_ | | | | Fix n_day_ma=3, sweep score |
| `direction_score` (n_day_ma) | _pending_ | | | | Fix score=0.54, sweep n_day_ma |
| `trailing_stop_pct` | _pending_ | | | | |
| `min_hold_time_days` | _pending_ | | | | |
| `max_positions` | _pending_ | | | | |

### Round 2: Focused Search

_Pending Round 1 results. Will cross IMPORTANT params only._

### Round 3: Robustness

_Pending Round 2._

### Round 4: Validation

_Pending Round 3._

## Decisions

_Record key decisions and reasoning here._

# Re-test ORB (Opening Range Breakout) with corrected execution

## Background

The ORB strategy showed Calmar 5.36 (+45.2% CAGR, -8.4% MDD) on NSE in our leaderboard, but it has a **same-bar entry bias**: the signal fires when `close > or_high` and the entry is taken at that same bar's close price. This inflates returns because you can't know the close price at the time the breakout happens intrabar.

The strategy needs to be re-tested with proper execution: **enter at the NEXT bar's open** after the breakout signal fires.

## Current implementation (pipeline)

The ORB is currently implemented in the intraday pipeline:
- `engine/intraday_sql_builder.py` -- builds SQL to detect breakout bars from `fmp.stock_prices_minute`
- `engine/intraday_simulator_v2.py` -- Python exit logic with target/stop/trailing/EOD
- `strategies/orb/config.yaml` -- NSE config
- `strategies/orb_us/config.yaml` -- US config

Results are in `results/orb_sweep_2026-03-15.json` and `results/orb_sweep_2026-03-16.json`.

## What needs to happen

Port the ORB strategy to a **standalone script** (`scripts/orb_standalone.py`) following the pattern in `scripts/forced_selling_dip.py` and `scripts/quality_dip_buy_lib.py`. Fix the entry bias.

### Data sources

- **NSE native minute data**: `nse.nse_charting_minute` (2015-02 to 2022-10, then 2026-02 to 2026-03). 2,625 symbols. Columns: symbol, date_epoch, open, close, volume. Full day 09:15-15:30 IST (labeled UTC).
- **FMP minute data (NSE)**: `fmp.stock_prices_minute` with exchange='NSE' (2022-2026). 2,666 symbols.
- **FMP minute data (US)**: `fmp.stock_prices_minute` for NASDAQ/NYSE (2020-2026).
- Timestamps in both sources are LOCAL time labeled as UTC. Do NOT apply AT TIME ZONE.

### Strategy logic

1. For each trading session (day), for each qualifying stock:
   - Compute Opening Range: high/low of first N bars (OR window: 15 or 30 minutes from session open)
   - NSE session: 09:15-15:30. OR window of 15 = bars from 09:15 to 09:29
   - Compute OR range: `or_high = max(high or close)`, `or_low = min(low or close)` over OR window bars
   - Filter: OR range must be >= min_range_pct of or_low (e.g., 0.5%)
2. Signal: first bar AFTER OR window where `close > or_high` (long breakout)
3. **CORRECTED entry**: enter at the NEXT bar's open (not signal bar's close)
4. Exit conditions (check each bar after entry):
   - Target: entry_price * (1 + target_pct) -- exit at bar close or exact target if bar high exceeds it
   - Stop: entry_price * (1 - stop_pct) -- exit at bar close or exact stop if bar low breaches it
   - Trailing stop: track highest close since entry, exit if close drops trailing_pct below
   - EOD: force-close 30 minutes before session end (15:00 for NSE)
   - Max hold bars: force-exit after N bars from entry
5. Charges: use `engine/charges.py` for NSE intraday (STT, brokerage etc.) and US charges
6. Slippage: 5 bps minimum

### Sweep parameters

```python
from itertools import product

param_grid = list(product(
    [15, 30],          # or_window (minutes)
    [0.5, 1.0],        # min_range_pct (%)
    [1.0, 1.5, 2.0],   # target_pct (%)
    [0.5, 1.0],        # stop_pct (%)
    [0, 1.0],          # trailing_stop_pct (0=off)
    [5, 10],           # max_positions
))
```

### Liquidity filter

- Only trade stocks with avg daily turnover > 7 Cr (NSE) or reasonable volume (US)
- Price > 50 (NSE) or > 5 (US)

### Output

Use `SweepResult` from `lib/backtest_result.py`. Save as `result.json`. Print leaderboard.

### Run

```bash
# NSE (native minute data, 2015-2022)
python run_remote.py scripts/orb_standalone.py --timeout 600 --ram 8192

# US
python run_remote.py scripts/orb_standalone_us.py --timeout 600 --ram 8192
```

### Success criteria

Compare corrected Calmar to the biased result (5.36). The bias audit on other strategies showed ~15-20pp CAGR inflation from same-bar entry. If the corrected ORB still has Calmar > 1.0, it's a genuine edge worth pursuing.

### Key files to read first

- `engine/intraday_sql_builder.py` -- current ORB SQL (understand the breakout detection)
- `engine/intraday_simulator_v2.py` -- current exit logic (understand target/stop/trailing)
- `strategies/orb/config.yaml` -- current sweep params
- `scripts/forced_selling_dip.py` -- reference for standalone script pattern
- `scripts/quality_dip_buy_lib.py` -- reference for data fetching and simulator
- `docs/BACKTEST_GUIDE.md` -- output format spec
- `docs/archive/pre-engine-2026-03/NEXT_STRATEGIES_PLAN.md` -- see Strategy 3b (ORB + quality filter) for context

### Important notes

- NSE native minute data has a GAP from 2022-10 to 2026-02. Handle this gracefully.
- FMP minute data timestamps are local time labeled UTC. NSE 09:15 is stored as 09:15 "UTC".
- The existing pipeline ORB uses `fmp.stock_prices_minute` (2020-2026 only). Using NSE native gives us 2015-2022 (7 years more data covering 2018 correction + 2020 COVID crash).
- Use `--market nse` / `--market us` flag pattern with thin wrapper scripts for CR compute (run_remote.py can't pass script args).

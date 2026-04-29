# Opening Range Breakout (ORB) -- NSE

NSE intraday strategy. Computes the high/low of the first N minutes (opening range),
enters long when price closes above the OR high, exits at take-profit, stop-loss,
or end-of-day.

Pre-filter: liquid NSE stocks (volume >= 5M, price >= Rs 100, daily range >= 1%).

## v2 Features

Uses the v2 intraday engine with:
- Trailing stop (0% or 2%)
- Min hold bars (0 or 5)
- Bar hi/lo exit (close-only or intrabar triggers)
- Dynamic sizing (fixed or equal_weight)
- Walk-forward ranking (signal_strength or top_performer)

## Parameter Sweep

256 configurations (v2): 4 SQL combos x 64 sim combos.

SQL params (4 combos): 2 or_window x 2 max_entry_bar
Sim params (64 combos): 2 target x 1 stop x 1 hold x 2 trailing x 2 min_hold x 2 hilo x 2 sizing x 2 ranking

## v1 Results (2026-03-16, 48 configs)

Best configs from v1 sweep (2020-2026):
- CAGR: 31-45%
- Calmar: 3.5-5.4
- Max Drawdown: 6-9%
- Key finding: target_pct is the dominant driver (2% >> 1%)

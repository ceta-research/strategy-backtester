# Opening Range Breakout (ORB)

NSE intraday strategy. Computes the high/low of the first N minutes (opening range),
enters long when price closes above the OR high, exits at take-profit, stop-loss,
or end-of-day.

Pre-filter: liquid NSE stocks (volume >= 5M, price >= Rs 100, daily range >= 1%).

## Expected Performance

Best configs from nse_arena sweep (2020-2026):
- CAGR: 31-45%
- Calmar: 3.5-5.4
- Max Drawdown: 6-9%

## Parameter Sweep

48 configurations: 2 or_window x 2 max_entry_bar x 3 target_pct x 2 stop_pct x 2 max_hold_bars

Key parameters:
- `or_window`: Opening range window in minutes (15, 30)
- `max_entry_bar`: Latest bar for breakout entry (60, 120)
- `target_pct`: Take-profit threshold (1%, 1.5%, 2%)
- `stop_pct`: Stop-loss threshold (1%, 1.5%)
- `max_hold_bars`: Max bars to hold after entry (60, 120)

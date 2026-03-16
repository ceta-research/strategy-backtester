# EOD Technical Strategy

Momentum-based daily strategy using the full ATO_Simulator pipeline:

1. **Scanner**: Filters by price > threshold, rolling avg daily turnover, and n-day gain
2. **Entry**: Bullish candle (close > open) + close above n-day MA + n-day high + market direction score
3. **Exit**: Trailing stop-loss with configurable min hold time. Anomalous price drops exit at 80% of last good price.
4. **Ranking**: Walk-forward adaptive ranking (top_performer) using 180-day realized + unrealized P&L

Default config runs 4 combinations: 2 min_hold x 2 max_positions = 4 configs.

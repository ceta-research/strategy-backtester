# Opening Range Breakout (ORB) -- US Market

NASDAQ intraday strategy. Same logic as NSE ORB but tuned for US market:
- Lower volume threshold (1M shares vs 5M for NSE)
- Lower price floor ($10 vs Rs 100)
- Risk-free rate: 2% (US T-bill vs 6.5% India)

Pre-filter: liquid NASDAQ stocks (volume >= 1M, price >= $10, daily range >= 1%).

## Parameter Sweep

256 configurations: 2 or_window x 2 max_entry_bar x 2 target_pct x 1 stop_pct x 1 max_hold_bars
x 2 trailing_stop x 2 min_hold x 2 bar_hilo x 2 sizing x 2 ranking

Uses v2 engine: 4 SQL queries, 64 sim combos each.

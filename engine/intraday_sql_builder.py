"""SQL builders for intraday strategies.

Each strategy has a build_*_sql() function that returns a complete SQL query
to be executed on the CR compute engine. All signal logic lives in SQL CTEs.
"""


def _orb_shared_ctes(cfg: dict) -> str:
    """Shared CTEs for ORB queries: EOD filtering, bars, opening range, entry detection."""
    symbol_filter = cfg.get("symbol_filter", "symbol LIKE '%.NS'")
    exchange_filter = cfg.get("exchange_filter", "m.exchange = 'NSE'")

    return f"""
-- Step 1a: All EOD data with lagged volume/range (avoids look-ahead bias)
all_eod AS (
    SELECT
        symbol, date AS trade_date, open, close, high, low, volume,
        (close - open) / NULLIF(open, 0) AS oc_return,
        LAG(volume) OVER (PARTITION BY symbol ORDER BY date) AS prev_day_volume,
        LAG((high - low) / NULLIF(low, 0)) OVER (PARTITION BY symbol ORDER BY date) AS prev_day_range_pct
    FROM fmp.stock_eod
    WHERE {symbol_filter}
      AND date BETWEEN '{cfg["start_date"]}' AND '{cfg["end_date"]}'
      AND close > 0
),

-- Step 1b: Filter using PREVIOUS day's metrics (no look-ahead)
-- open > min_price is OK: open is known at market open
filtered_eod AS (
    SELECT symbol, trade_date, open, close, high, low, volume, oc_return
    FROM all_eod
    WHERE open > {cfg["min_price"]}
      AND prev_day_volume >= {cfg["min_volume"]}
      AND prev_day_range_pct >= {cfg["min_range_pct"]}
),

bench AS (
    SELECT trade_date, AVG(oc_return) AS bench_ret
    FROM filtered_eod
    GROUP BY trade_date
),

-- Step 2a: Minute bars (INNER JOIN prunes to filtered stocks only)
bars AS (
    SELECT
        m.symbol,
        to_timestamp(m.dateEpoch)::DATE AS trade_date,
        m.dateEpoch, m.open, m.high, m.low, m.close, m.volume,
        ROW_NUMBER() OVER (
            PARTITION BY m.symbol, to_timestamp(m.dateEpoch)::DATE
            ORDER BY m.dateEpoch
        ) AS bar_num
    FROM fmp.stock_prices_minute m
    INNER JOIN filtered_eod f
        ON m.symbol = f.symbol
        AND to_timestamp(m.dateEpoch)::DATE = f.trade_date
    WHERE {exchange_filter}
),

-- Opening range: high/low of first N bars
opening_range AS (
    SELECT
        symbol, trade_date,
        MAX(high) AS or_high,
        MIN(low) AS or_low,
        MAX(high) - MIN(low) AS or_range
    FROM bars
    WHERE bar_num <= {cfg["or_window"]}
    GROUP BY symbol, trade_date
    HAVING MAX(high) > MIN(low)
),

-- Entry: first bar closing above OR high (breakout)
-- Join filtered_eod to get EOD open for split-adjustment check
entry_candidates AS (
    SELECT
        b.symbol, b.trade_date, b.bar_num, b.close AS entry_price,
        o.or_high, o.or_low, o.or_range, f.open AS eod_open,
        ROW_NUMBER() OVER (PARTITION BY b.symbol, b.trade_date ORDER BY b.bar_num) AS rn
    FROM bars b
    JOIN opening_range o USING (symbol, trade_date)
    JOIN filtered_eod f USING (symbol, trade_date)
    WHERE b.bar_num > {cfg["or_window"]}
      AND b.bar_num <= {cfg["max_entry_bar"]}
      AND b.close > o.or_high
),

first_entry AS (
    SELECT symbol, trade_date, bar_num AS entry_bar, entry_price,
           or_high, or_low, or_range
    FROM entry_candidates
    WHERE rn = 1
      -- FMP minute data is NOT split-adjusted; EOD IS. Skip mismatches.
      AND entry_price BETWEEN eod_open * 0.8 AND eod_open * 1.2
)"""


def build_orb_sql(cfg: dict) -> str:
    """Build Opening Range Breakout SQL query from config dict.

    cfg keys: start_date, end_date, min_volume, min_price, min_range_pct,
              or_window, max_entry_bar, target_pct, stop_pct, max_hold_bars

    Returns: SQL string with CTEs + final SELECT producing columns:
        symbol, trade_date, entry_bar, entry_price, exit_price,
        exit_type, or_range_pct, signal_strength, bench_ret
    """
    target_factor = round(1.0 + cfg["target_pct"], 6)
    stop_factor = round(1.0 - cfg["stop_pct"], 6)

    shared = _orb_shared_ctes(cfg)

    return f"""
WITH
{shared},

-- Step 2c: Exit -- target, stop-loss, or below OR low
exit_candidates AS (
    SELECT
        b.symbol, b.trade_date, b.bar_num, b.close AS exit_price,
        ROW_NUMBER() OVER (PARTITION BY b.symbol, b.trade_date ORDER BY b.bar_num) AS rn
    FROM bars b
    JOIN first_entry e USING (symbol, trade_date)
    WHERE b.bar_num > e.entry_bar
      AND b.bar_num <= e.entry_bar + {cfg["max_hold_bars"]}
      AND (b.close >= e.entry_price * {target_factor}
           OR b.close <= LEAST(e.entry_price * {stop_factor}, e.or_low))
),

first_exit AS (
    SELECT symbol, trade_date, exit_price
    FROM exit_candidates WHERE rn = 1
),

eod_exit AS (
    SELECT symbol, trade_date,
           FIRST(close ORDER BY bar_num DESC) AS eod_price
    FROM bars GROUP BY symbol, trade_date
)

SELECT
    e.symbol, e.trade_date, e.entry_bar, e.entry_price,
    COALESCE(x.exit_price, eod.eod_price) AS exit_price,
    CASE WHEN x.exit_price IS NOT NULL THEN 'signal' ELSE 'eod' END AS exit_type,
    e.or_range / NULLIF(e.or_low, 0) AS or_range_pct,
    e.or_range / NULLIF(e.or_low, 0) AS signal_strength,
    b.bench_ret
FROM first_entry e
LEFT JOIN first_exit x USING (symbol, trade_date)
JOIN eod_exit eod USING (symbol, trade_date)
JOIN bench b USING (trade_date)
ORDER BY e.trade_date, e.or_range DESC
"""


def build_orb_signal_sql(cfg: dict) -> str:
    """Build Opening Range Breakout signal matrix SQL (v2).

    Returns all bars from entry onward for each entry signal, without exit logic.
    Exit logic moves to Python simulator (intraday_simulator_v2).

    cfg keys: start_date, end_date, min_volume, min_price, min_range_pct,
              or_window, max_entry_bar, symbol_filter, exchange_filter

    Returns: SQL string producing columns:
        symbol, trade_date, entry_bar, entry_price,
        or_high, or_low, or_range, signal_strength,
        bar_num, bar_open, bar_high, bar_low, bar_close,
        bench_ret
    """
    shared = _orb_shared_ctes(cfg)

    return f"""
WITH
{shared}

SELECT
    e.symbol, e.trade_date, e.entry_bar, e.entry_price,
    e.or_high, e.or_low, e.or_range,
    e.or_range / NULLIF(e.or_low, 0) AS signal_strength,
    b.bar_num, b.open AS bar_open, b.high AS bar_high, b.low AS bar_low, b.close AS bar_close,
    bench.bench_ret
FROM first_entry e
JOIN bars b ON b.symbol = e.symbol AND b.trade_date = e.trade_date
    AND b.bar_num >= e.entry_bar
JOIN bench ON bench.trade_date = e.trade_date
ORDER BY e.trade_date, e.symbol, b.bar_num
"""


def build_rvol_atr_sql(symbols: list, start_date: str, end_date: str,
                       symbol_filter: str = "symbol NOT LIKE '%.%'",
                       exchange_filter: str = "m.exchange = 'NASDAQ'") -> str:
    """Build a lightweight SQL query to compute RVOL and ATR for specific symbols.

    Run AFTER the signal matrix query, using the entry symbols discovered.
    Uses EOD data (small) + first-5-min volume from minute bars (scoped).

    Returns: SQL producing columns:
        symbol, trade_date, rvol, atr_14
    """
    symbol_list = ", ".join(f"'{s}'" for s in symbols)

    return f"""
WITH
eod AS (
    SELECT symbol, date AS trade_date, open, close, high, low, volume
    FROM fmp.stock_eod
    WHERE symbol IN ({symbol_list})
      AND date BETWEEN '{start_date}' AND '{end_date}'
      AND close > 0
),

-- ATR(14) from EOD data
eod_true_range AS (
    SELECT symbol, trade_date,
        GREATEST(high - low,
                 ABS(high - LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date)),
                 ABS(low - LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date))
        ) AS true_range
    FROM eod
),
atr_14 AS (
    SELECT symbol, trade_date,
        AVG(true_range) OVER (
            PARTITION BY symbol ORDER BY trade_date
            ROWS BETWEEN 13 PRECEDING AND CURRENT ROW
        ) AS atr_14
    FROM eod_true_range
),

-- RVOL: first 5-min volume vs 21-day trailing average
minute_bars AS (
    SELECT
        m.symbol,
        to_timestamp(m.dateEpoch)::DATE AS trade_date,
        m.volume,
        ROW_NUMBER() OVER (
            PARTITION BY m.symbol, to_timestamp(m.dateEpoch)::DATE
            ORDER BY m.dateEpoch
        ) AS bar_num
    FROM fmp.stock_prices_minute m
    WHERE m.symbol IN ({symbol_list})
      AND {exchange_filter}
      AND to_timestamp(m.dateEpoch)::DATE BETWEEN '{start_date}' AND '{end_date}'
),
first_5min_volume AS (
    SELECT symbol, trade_date, SUM(volume) AS vol_5min
    FROM minute_bars
    WHERE bar_num <= 5
    GROUP BY symbol, trade_date
),
rvol AS (
    SELECT symbol, trade_date,
        vol_5min / NULLIF(
            AVG(vol_5min) OVER (
                PARTITION BY symbol ORDER BY trade_date
                ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING
            ), 0
        ) AS rvol
    FROM first_5min_volume
)

SELECT
    a.symbol,
    a.trade_date,
    r.rvol,
    a.atr_14
FROM atr_14 a
LEFT JOIN rvol r ON r.symbol = a.symbol AND r.trade_date = a.trade_date
ORDER BY a.symbol, a.trade_date
"""

#!/usr/bin/env python3
"""ORB Standalone - Corrected Execution (next-bar open entry).

Opening Range Breakout with bias-corrected entry: signal fires when
bar close > OR high, but entry is at the NEXT bar's open price.

All exit conditions (target/stop/trailing for every sweep combo) are
precomputed in SQL, returning 1 compact row per signal (~20K rows)
instead of the full bar matrix (~5M+ rows).

NSE: Uses nse.nse_charting_minute (2015-02 to 2022-10). No high/low
columns -- uses max(open,close)/min(open,close) as proxies.

US: Uses fmp.stock_prices_minute (2020-2026) with actual high/low.

Usage:
    python run_remote.py scripts/orb_standalone.py --timeout 600 --ram 8192
    python run_remote.py scripts/orb_standalone_us.py --timeout 600 --ram 8192
"""

import sys
import os
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from lib.backtest_result import BacktestResult, SweepResult
from engine.charges import nse_intraday_charges, us_intraday_charges

# ── Constants ────────────────────────────────────────────────────────────────

SLIPPAGE = 0.0005  # 5 bps per side
EOD_BUFFER = 30    # Force-exit 30 bars before session end
MAX_ENTRY_BAR = 120  # Must break out within first 2 hours
EOD_CUTOFF = 345   # Bar 345 = 30 min before NSE close (375 - 30)

STRATEGY_NAME = "orb_corrected"

# Target/stop levels in the sweep (as multipliers)
TARGET_LEVELS = {"t100": 1.01, "t150": 1.015, "t200": 1.02}
STOP_LEVELS = {"s050": 0.995, "s100": 0.99}
TRAIL_FACTOR = 0.99  # trailing stop at 1% below running high

# Map sweep percent values to column prefixes
TARGET_COL = {1.0: "t100", 1.5: "t150", 2.0: "t200"}
STOP_COL_NO_TRAIL = {0.5: "s050", 1.0: "s100"}
STOP_COL_TRAIL = {0.5: "s050t", 1.0: "s100t"}


# ── SQL: Shared CTEs ─────────────────────────────────────────────────────────

def _nse_base_ctes(or_window, min_range_pct, start_date, end_date,
                   min_price, min_turnover):
    """Shared CTEs for NSE: daily filter, minute bars, opening range, signal."""
    mrp = min_range_pct / 100

    return f"""
daily_stats AS (
    SELECT
        symbol,
        CAST(to_timestamp(date_epoch) AS DATE) AS trade_date,
        open, close, volume,
        AVG(close * volume) OVER (
            PARTITION BY symbol ORDER BY date_epoch
            ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
        ) AS trail_turnover
    FROM nse.nse_charting_day
    WHERE CAST(to_timestamp(date_epoch) AS DATE)
        BETWEEN DATE '{start_date}' - INTERVAL '60 days' AND DATE '{end_date}'
      AND close > 0
),

filtered_days AS (
    SELECT symbol, trade_date
    FROM daily_stats
    WHERE open > {min_price}
      AND trail_turnover >= {min_turnover}
      AND trade_date BETWEEN '{start_date}' AND '{end_date}'
),

bars AS (
    SELECT
        m.symbol,
        CAST(to_timestamp(m.date_epoch) AS DATE) AS trade_date,
        m.date_epoch, m.open, m.close, m.volume,
        GREATEST(m.open, m.close) AS bar_high,
        LEAST(m.open, m.close) AS bar_low,
        ROW_NUMBER() OVER (
            PARTITION BY m.symbol, CAST(to_timestamp(m.date_epoch) AS DATE)
            ORDER BY m.date_epoch
        ) AS bar_num
    FROM nse.nse_charting_minute m
    INNER JOIN filtered_days f
        ON m.symbol = f.symbol
        AND CAST(to_timestamp(m.date_epoch) AS DATE) = f.trade_date
    WHERE m.open > 0 AND m.close > 0
      AND m.date_epoch >= EXTRACT(EPOCH FROM DATE '{start_date}')::BIGINT
      AND m.date_epoch <= EXTRACT(EPOCH FROM DATE '{end_date}')::BIGINT + 86400
),

opening_range AS (
    SELECT
        symbol, trade_date,
        MAX(bar_high) AS or_high,
        MIN(bar_low) AS or_low,
        MAX(bar_high) - MIN(bar_low) AS or_range
    FROM bars
    WHERE bar_num <= {or_window}
    GROUP BY symbol, trade_date
    HAVING MAX(bar_high) > MIN(bar_low)
       AND (MAX(bar_high) - MIN(bar_low)) / NULLIF(MIN(bar_low), 0) >= {mrp}
),

signal_candidates AS (
    SELECT
        b.symbol, b.trade_date, b.bar_num, b.close AS signal_price,
        o.or_high, o.or_low, o.or_range,
        ROW_NUMBER() OVER (
            PARTITION BY b.symbol, b.trade_date ORDER BY b.bar_num
        ) AS rn
    FROM bars b
    JOIN opening_range o USING (symbol, trade_date)
    WHERE b.bar_num > {or_window}
      AND b.bar_num <= {MAX_ENTRY_BAR}
      AND b.close > o.or_high
),

first_signal AS (
    SELECT symbol, trade_date, bar_num AS signal_bar, signal_price,
           or_high, or_low, or_range
    FROM signal_candidates
    WHERE rn = 1
)"""


def _us_base_ctes(or_window, min_range_pct, start_date, end_date,
                  min_price, min_volume):
    """Shared CTEs for US: daily filter, minute bars, opening range, signal."""
    mrp = min_range_pct / 100

    return f"""
all_eod AS (
    SELECT
        symbol, date AS trade_date, open, close, high, low, volume,
        LAG(volume) OVER (PARTITION BY symbol ORDER BY date) AS prev_vol
    FROM fmp.stock_eod
    WHERE symbol NOT LIKE '%.%'
      AND date BETWEEN DATE '{start_date}' - INTERVAL '5 days' AND DATE '{end_date}'
      AND close > 0
),

filtered_eod AS (
    SELECT symbol, trade_date, open AS eod_open
    FROM all_eod
    WHERE open > {min_price}
      AND prev_vol >= {min_volume}
      AND trade_date BETWEEN '{start_date}' AND '{end_date}'
),

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
    WHERE m.exchange IN ('NASDAQ', 'NYSE')
),

opening_range AS (
    SELECT
        symbol, trade_date,
        MAX(high) AS or_high,
        MIN(low) AS or_low,
        MAX(high) - MIN(low) AS or_range
    FROM bars
    WHERE bar_num <= {or_window}
    GROUP BY symbol, trade_date
    HAVING MAX(high) > MIN(low)
       AND (MAX(high) - MIN(low)) / NULLIF(MIN(low), 0) >= {mrp}
),

signal_candidates AS (
    SELECT
        b.symbol, b.trade_date, b.bar_num, b.close AS signal_price,
        o.or_high, o.or_low, o.or_range,
        f.eod_open,
        ROW_NUMBER() OVER (
            PARTITION BY b.symbol, b.trade_date ORDER BY b.bar_num
        ) AS rn
    FROM bars b
    JOIN opening_range o USING (symbol, trade_date)
    JOIN filtered_eod f USING (symbol, trade_date)
    WHERE b.bar_num > {or_window}
      AND b.bar_num <= {MAX_ENTRY_BAR}
      AND b.close > o.or_high
),

first_signal AS (
    SELECT symbol, trade_date, bar_num AS signal_bar, signal_price,
           or_high, or_low, or_range
    FROM signal_candidates
    WHERE rn = 1
      AND signal_price BETWEEN eod_open * 0.8 AND eod_open * 1.2
)"""


# ── SQL: Compact exit computation ────────────────────────────────────────────

def _exit_ctes_and_select(market):
    """CTEs for corrected entry + precomputed exits for all sweep combos.

    Returns 1 row per signal with entry_price, signal_strength, and
    exit bar/price for every (target, stop, trailing) combination.

    Columns: symbol, trade_date, entry_price, signal_strength,
             t100_bar, t100_px, t150_bar, t150_px, t200_bar, t200_px,
             s050_bar, s050_px, s100_bar, s100_px,
             s050t_bar, s050t_px, s100t_bar, s100t_px,
             eod_close
    """
    # Bar high/low column names differ between NSE (proxy) and US (actual)
    if market == "nse":
        bh = "GREATEST(b.open, b.close)"
        bl = "LEAST(b.open, b.close)"
    else:
        bh = "b.high"
        bl = "b.low"

    return f"""
,

-- Corrected entry: next bar's open after signal
corrected_entry AS (
    SELECT
        s.symbol, s.trade_date, s.signal_bar,
        nb.open AS entry_price,
        s.or_high, s.or_low,
        s.or_range / NULLIF(s.or_low, 0) AS signal_strength
    FROM first_signal s
    JOIN bars nb
        ON nb.symbol = s.symbol
        AND nb.trade_date = s.trade_date
        AND nb.bar_num = s.signal_bar + 1
    WHERE nb.open > 0
),

-- All post-entry bars with running max high (for trailing stop)
-- Entry bar (signal_bar+1) included for running_high tracking but
-- flagged as not exit-eligible (can't exit same bar you entered)
post_bars AS (
    SELECT
        e.symbol, e.trade_date, e.entry_price, e.signal_strength,
        b.bar_num,
        b.open AS bar_open,
        {bh} AS bar_high,
        {bl} AS bar_low,
        b.close AS bar_close,
        b.bar_num > e.signal_bar + 1 AS is_exit,
        MAX({bh}) OVER (
            PARTITION BY e.symbol, e.trade_date
            ORDER BY b.bar_num
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS run_high
    FROM corrected_entry e
    JOIN bars b
        ON b.symbol = e.symbol
        AND b.trade_date = e.trade_date
        AND b.bar_num >= e.signal_bar + 1
        AND b.bar_num <= {EOD_CUTOFF}
)

SELECT
    p.symbol,
    CAST(p.trade_date AS VARCHAR) AS trade_date,
    p.entry_price,
    p.signal_strength,

    -- Target 1.0%
    MIN(CASE WHEN is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t100"]}
        THEN bar_num END) AS t100_bar,
    ARG_MIN(GREATEST(bar_open, entry_price * {TARGET_LEVELS["t100"]}), bar_num)
        FILTER (WHERE is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t100"]})
        AS t100_px,

    -- Target 1.5%
    MIN(CASE WHEN is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t150"]}
        THEN bar_num END) AS t150_bar,
    ARG_MIN(GREATEST(bar_open, entry_price * {TARGET_LEVELS["t150"]}), bar_num)
        FILTER (WHERE is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t150"]})
        AS t150_px,

    -- Target 2.0%
    MIN(CASE WHEN is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t200"]}
        THEN bar_num END) AS t200_bar,
    ARG_MIN(GREATEST(bar_open, entry_price * {TARGET_LEVELS["t200"]}), bar_num)
        FILTER (WHERE is_exit AND bar_high >= entry_price * {TARGET_LEVELS["t200"]})
        AS t200_px,

    -- Stop 0.5% (no trailing)
    MIN(CASE WHEN is_exit AND bar_low <= entry_price * {STOP_LEVELS["s050"]}
        THEN bar_num END) AS s050_bar,
    ARG_MIN(LEAST(bar_open, entry_price * {STOP_LEVELS["s050"]}), bar_num)
        FILTER (WHERE is_exit AND bar_low <= entry_price * {STOP_LEVELS["s050"]})
        AS s050_px,

    -- Stop 1.0% (no trailing)
    MIN(CASE WHEN is_exit AND bar_low <= entry_price * {STOP_LEVELS["s100"]}
        THEN bar_num END) AS s100_bar,
    ARG_MIN(LEAST(bar_open, entry_price * {STOP_LEVELS["s100"]}), bar_num)
        FILTER (WHERE is_exit AND bar_low <= entry_price * {STOP_LEVELS["s100"]})
        AS s100_px,

    -- Stop 0.5% + trailing 1%: effective stop = MAX(fixed, trail)
    MIN(CASE WHEN is_exit
        AND bar_low <= GREATEST(entry_price * {STOP_LEVELS["s050"]}, run_high * {TRAIL_FACTOR})
        THEN bar_num END) AS s050t_bar,
    ARG_MIN(
        LEAST(bar_open, GREATEST(entry_price * {STOP_LEVELS["s050"]}, run_high * {TRAIL_FACTOR})),
        bar_num
    ) FILTER (WHERE is_exit
        AND bar_low <= GREATEST(entry_price * {STOP_LEVELS["s050"]}, run_high * {TRAIL_FACTOR}))
        AS s050t_px,

    -- Stop 1.0% + trailing 1%: effective stop = MAX(fixed, trail)
    MIN(CASE WHEN is_exit
        AND bar_low <= GREATEST(entry_price * {STOP_LEVELS["s100"]}, run_high * {TRAIL_FACTOR})
        THEN bar_num END) AS s100t_bar,
    ARG_MIN(
        LEAST(bar_open, GREATEST(entry_price * {STOP_LEVELS["s100"]}, run_high * {TRAIL_FACTOR})),
        bar_num
    ) FILTER (WHERE is_exit
        AND bar_low <= GREATEST(entry_price * {STOP_LEVELS["s100"]}, run_high * {TRAIL_FACTOR}))
        AS s100t_px,

    -- EOD close (last bar within cutoff)
    ARG_MAX(bar_close, bar_num) AS eod_close

FROM post_bars p
GROUP BY p.symbol, p.trade_date, p.entry_price, p.signal_strength
ORDER BY p.trade_date, p.signal_strength DESC
"""


# ── SQL Builders (public) ────────────────────────────────────────────────────

def build_nse_compact_sql(or_window, min_range_pct, start_date, end_date,
                          min_price=50, min_turnover=70_000_000):
    """Compact ORB SQL for NSE: 1 row per signal with precomputed exits."""
    base = _nse_base_ctes(or_window, min_range_pct, start_date, end_date,
                          min_price, min_turnover)
    exits = _exit_ctes_and_select("nse")
    return f"WITH\n{base}\n{exits}"


def build_us_compact_sql(or_window, min_range_pct, start_date, end_date,
                         min_price=5, min_volume=2_000_000):
    """Compact ORB SQL for US: 1 row per signal with precomputed exits."""
    base = _us_base_ctes(or_window, min_range_pct, start_date, end_date,
                         min_price, min_volume)
    exits = _exit_ctes_and_select("us")
    return f"WITH\n{base}\n{exits}"


# ── Signal Processing ────────────────────────────────────────────────────────

def group_compact_signals(rows):
    """Group compact signal rows by trade_date.

    Returns: dict[date_str, list[signal_dict]]
    Each signal_dict has: symbol, entry_price, signal_strength,
        and precomputed exit columns (t100_bar, t100_px, s050_bar, ..., eod_close).
    """
    by_date = defaultdict(list)
    for row in rows:
        d = str(row["trade_date"])[:10]
        sig = {
            "symbol": row["symbol"],
            "entry_price": float(row["entry_price"]),
            "signal_strength": float(row.get("signal_strength") or 0),
        }
        # Copy all exit columns
        for col in ("t100_bar", "t100_px", "t150_bar", "t150_px",
                     "t200_bar", "t200_px", "s050_bar", "s050_px",
                     "s100_bar", "s100_px", "s050t_bar", "s050t_px",
                     "s100t_bar", "s100t_px", "eod_close"):
            v = row.get(col)
            sig[col] = float(v) if v is not None else None
        by_date[d].append(sig)
    return dict(by_date)


def resolve_exit_compact(signal, target_pct, stop_pct, trailing_pct):
    """Resolve exit from precomputed columns.

    Args:
        signal: dict with precomputed exit columns
        target_pct: target in % (1.0, 1.5, 2.0)
        stop_pct: stop in % (0.5, 1.0)
        trailing_pct: trailing in % (0 or 1.0)

    Returns: (exit_price, exit_type) or (None, None) if no valid exit
    """
    entry = signal["entry_price"]
    if not entry or entry <= 0:
        return None, None

    # Look up the right columns
    t_col = TARGET_COL[target_pct]
    if trailing_pct > 0:
        s_col = STOP_COL_TRAIL[stop_pct]
    else:
        s_col = STOP_COL_NO_TRAIL[stop_pct]

    t_bar = signal.get(f"{t_col}_bar")
    t_px = signal.get(f"{t_col}_px")
    s_bar = signal.get(f"{s_col}_bar")
    s_px = signal.get(f"{s_col}_px")
    eod = signal.get("eod_close")

    # Determine exit: earliest of target or stop (stop wins ties)
    if s_bar is not None and t_bar is not None:
        if s_bar <= t_bar:
            return s_px, "stop"
        else:
            return t_px, "target"
    elif s_bar is not None:
        return s_px, "stop"
    elif t_bar is not None:
        return t_px, "target"
    elif eod is not None:
        return eod, "eod"
    else:
        return None, None


# ── Portfolio Simulator ──────────────────────────────────────────────────────

def simulate_portfolio(signals_by_date, config, benchmark):
    """Day-by-day intraday portfolio simulation using compact signal data.

    All positions open and close within the same day.

    Args:
        signals_by_date: dict[date_str, list[signal_dict]]
        config: {initial_capital, max_positions, target_pct, stop_pct,
                 trailing_stop_pct, exchange, risk_free_rate, ...}
        benchmark: dict[date_str, close_price]

    Returns:
        BacktestResult (already computed)
    """
    capital = config["initial_capital"]
    max_pos = config["max_positions"]
    exchange = config["exchange"]
    target_pct = config["target_pct_sweep"]   # in % for column lookup
    stop_pct = config["stop_pct_sweep"]       # in % for column lookup
    trailing_pct = config["trailing_pct_sweep"]  # in % for column lookup
    rfr = config.get("risk_free_rate", 0.02)
    charges_fn = nse_intraday_charges if exchange == "NSE" else us_intraday_charges

    params = {
        "or_window": config.get("or_window"),
        "min_range_pct": config.get("min_range_pct"),
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "trailing_stop_pct": trailing_pct,
        "max_positions": max_pos,
    }

    result = BacktestResult(
        STRATEGY_NAME, params, "PORTFOLIO", exchange, capital,
        slippage_bps=5, risk_free_rate=rfr,
        description="ORB with corrected next-bar-open entry",
    )

    all_dates = sorted(benchmark.keys())
    if not all_dates:
        return result

    equity = float(capital)
    bm_first = benchmark[all_dates[0]]
    bm_epochs = []
    bm_values = []

    for date_str in all_dates:
        epoch = _date_to_epoch(date_str)
        bm_epochs.append(epoch)
        bm_values.append(benchmark[date_str] / bm_first * capital)

        signals = signals_by_date.get(date_str, [])
        if not signals:
            result.add_equity_point(epoch, equity)
            continue

        # Already sorted by signal_strength DESC from SQL ORDER BY
        order_value = equity / max_pos if equity > 0 else 0
        if order_value < 1000:
            result.add_equity_point(epoch, equity)
            continue

        day_pnl = 0.0
        trades_today = 0

        for sig in signals:
            if trades_today >= max_pos:
                break

            exit_price, exit_type = resolve_exit_compact(
                sig, target_pct, stop_pct, trailing_pct)

            if exit_price is None or exit_price <= 0:
                continue

            entry_price = sig["entry_price"]
            qty = int(order_value / entry_price)
            if qty <= 0:
                continue

            trade_value = qty * entry_price
            charges = charges_fn(trade_value)
            slip = trade_value * SLIPPAGE * 2

            gross_pnl = (exit_price - entry_price) * qty
            net_pnl = gross_pnl - charges - slip
            day_pnl += net_pnl
            trades_today += 1

            result.add_trade(
                entry_epoch=epoch, exit_epoch=epoch,
                entry_price=entry_price, exit_price=exit_price,
                quantity=qty, charges=charges, slippage=slip,
            )

        equity += day_pnl
        result.add_equity_point(epoch, equity)

    if len(bm_epochs) == len(result.equity_curve):
        result.set_benchmark_values(bm_epochs, bm_values)

    result.compute()
    return result


# ── Benchmark ────────────────────────────────────────────────────────────────

def fetch_benchmark(cr, market, start_date, end_date):
    """Fetch benchmark daily closes. Returns dict[date_str, close]."""
    if market == "nse":
        sql = f"""
        SELECT CAST(to_timestamp(date_epoch) AS DATE)::VARCHAR AS d, close
        FROM nse.nse_charting_day
        WHERE symbol = 'NIFTYBEES'
          AND CAST(to_timestamp(date_epoch) AS DATE)
              BETWEEN '{start_date}' AND '{end_date}'
          AND close > 0
        ORDER BY date_epoch
        """
    else:
        sql = f"""
        SELECT CAST(date AS VARCHAR) AS d, adjClose AS close
        FROM fmp.stock_eod
        WHERE symbol = 'SPY'
          AND date BETWEEN '{start_date}' AND '{end_date}'
          AND adjClose > 0
        ORDER BY date
        """
    rows = cr.query(sql, timeout=120, limit=100000, verbose=True,
                    memory_mb=4096, threads=4)
    bm = {}
    for r in rows:
        d = str(r.get("d", ""))[:10]
        c = r.get("close")
        if d and c and float(c) > 0:
            bm[d] = float(c)
    print(f"  Benchmark: {len(bm)} trading days")
    return bm


# ── Helpers ──────────────────────────────────────────────────────────────────

def _date_to_epoch(date_str):
    d = date_str[:10]
    return int(datetime(int(d[:4]), int(d[5:7]), int(d[8:10]),
                        tzinfo=timezone.utc).timestamp())


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    market = "nse"
    if "--market" in sys.argv:
        idx = sys.argv.index("--market")
        if idx + 1 < len(sys.argv):
            market = sys.argv[idx + 1].lower()

    if market == "nse":
        exchange = "NSE"
        start_date = "2015-03-01"
        end_date = "2022-10-31"
        capital = 1_000_000
        rfr = 0.065
        min_price = 50
        min_turnover = 70_000_000
        description = "ORB corrected execution on NSE (native minute data 2015-2022)"
    elif market == "us":
        exchange = "US"
        start_date = "2020-01-06"
        end_date = "2026-03-09"
        capital = 1_000_000
        rfr = 0.02
        min_price = 5
        min_turnover = 2_000_000
        description = "ORB corrected execution on US (FMP minute data 2020-2026)"
    else:
        print(f"Unknown market: {market}. Use --market nse or --market us")
        return

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME} ({market.upper()})")
    print(f"  Period: {start_date} to {end_date}")
    print(f"  Capital: {capital:,.0f} | RFR: {rfr*100:.1f}%")
    print(f"  Entry: NEXT bar open after signal (corrected)")
    print(f"  Exits: precomputed in SQL (compact, 1 row per signal)")
    print("=" * 80)

    print(f"\nFetching benchmark...")
    benchmark = fetch_benchmark(cr, market, start_date, end_date)
    if not benchmark:
        print("No benchmark data. Aborting.")
        return

    signal_configs = [(ow, mrp) for ow in [15, 30] for mrp in [0.5, 1.0]]
    sim_params = list(product(
        [1.0, 1.5, 2.0],   # target_pct (%)
        [0.5, 1.0],        # stop_pct (%)
        [0, 1.0],          # trailing_stop_pct (%)
        [5, 10],           # max_positions
    ))

    total = len(signal_configs) * len(sim_params)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs ({market.upper()} ORB corrected)")
    print(f"  Signal configs: {len(signal_configs)}, sim params: {len(sim_params)}")
    print(f"  Fixed: max_entry_bar={MAX_ENTRY_BAR}, eod_cutoff=bar {EOD_CUTOFF}, "
          f"slippage={SLIPPAGE*10000:.0f}bps")
    print(f"{'='*80}")

    sweep = SweepResult(
        f"{STRATEGY_NAME}_{market}", "PORTFOLIO", exchange, capital,
        slippage_bps=5, description=description,
    )

    config_num = 0

    # Split long date ranges into chunks to keep SQL fast
    if market == "nse":
        date_chunks = [
            ("2015-03-01", "2017-12-31"),
            ("2018-01-01", "2020-06-30"),
            ("2020-07-01", "2022-10-31"),
        ]
    else:
        date_chunks = [(start_date, end_date)]  # US is shorter, no chunking needed

    for ow, mrp in signal_configs:
        print(f"\nFetching signals: or_window={ow}, min_range={mrp}%...")
        signals = {}

        for chunk_start, chunk_end in date_chunks:
            if market == "nse":
                sql = build_nse_compact_sql(ow, mrp, chunk_start, chunk_end,
                                            min_price, min_turnover)
            else:
                sql = build_us_compact_sql(ow, mrp, chunk_start, chunk_end,
                                           min_price, int(min_turnover))

            label = f"  [{chunk_start} to {chunk_end}] "
            print(f"{label}querying...")
            rows = cr.query(sql, timeout=900, limit=10_000_000, verbose=True,
                            memory_mb=16384, threads=6)
            if rows:
                chunk_signals = group_compact_signals(rows)
                signals.update(chunk_signals)
                n = sum(len(v) for v in chunk_signals.values())
                print(f"{label}{n} signals across {len(chunk_signals)} days")
                del rows, chunk_signals
            else:
                print(f"{label}no signals")

        if not signals:
            print("  No signals found across all chunks, skipping")
            config_num += len(sim_params)
            continue

        n_signals = sum(len(v) for v in signals.values())
        print(f"  TOTAL: {n_signals} signals across {len(signals)} trading days")

        for target, stop, trail, max_pos in sim_params:
            config_num += 1

            config = {
                "initial_capital": capital,
                "max_positions": max_pos,
                "target_pct_sweep": target,
                "stop_pct_sweep": stop,
                "trailing_pct_sweep": trail,
                "exchange": exchange,
                "risk_free_rate": rfr,
                "or_window": ow,
                "min_range_pct": mrp,
            }

            r = simulate_portfolio(signals, config, benchmark)
            sweep.add_config(
                {"or_window": ow, "min_range_pct": mrp,
                 "target_pct": target, "stop_pct": stop,
                 "trailing_stop_pct": trail, "max_positions": max_pos},
                r,
            )
            r.compact()

            s = r.to_dict()["summary"]
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            trades = s.get("total_trades") or 0
            wr = (s.get("win_rate") or 0) * 100
            print(f"  [{config_num}/{total}] ow={ow} mr={mrp}% t={target}% "
                  f"s={stop}% tr={trail}% p={max_pos} | "
                  f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={cal:.2f} "
                  f"WR={wr:.0f}% T={trades}")

        del signals

    # Results
    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20)

    sorted_configs = sweep._sorted("calmar_ratio")
    if sorted_configs:
        _, best = sorted_configs[0]
        best.print_summary()

        best_s = best.to_dict()["summary"]
        best_cal = best_s.get("calmar_ratio") or 0
        print(f"\n  Biased ORB Calmar: 5.36 (pipeline, same-bar entry)")
        print(f"  Corrected Calmar:  {best_cal:.2f} (next-bar open entry)")
        if best_cal > 1.0:
            print(f"  VERDICT: Genuine edge (Calmar > 1.0)")
        else:
            print(f"  VERDICT: Edge likely from entry bias (Calmar < 1.0)")


if __name__ == "__main__":
    main()

"""Shared library for quality dip-buy standalone scripts.

Provides data fetching, quality filter, dip detection, multi-position
portfolio simulator, and always-invested adjustment. No main() -- used
as an import by experiment scripts.

Strategy logic ported from engine/signals/quality_dip_buy.py (Polars)
to pure Python (lists/dicts) for standalone execution.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult

TRADING_DAYS_PER_YEAR = 252
SLIPPAGE = 0.0005  # 5 bps


# ── Data Fetching ────────────────────────────────────────────────────────────

def fetch_universe(cr, exchange, start_epoch, end_epoch,
                   price_threshold=50, turnover_threshold=70_000_000,
                   turnover_period=125, warmup_days=1500,
                   mktcap_threshold=None, source="native"):
    """Fetch OHLCV data for qualifying stocks on an exchange.

    Two-pass: first get qualifying symbols, then fetch OHLCV.

    Args:
        source: "native" uses nse.nse_charting_day (better quality for NSE),
                "fmp" uses fmp.stock_eod (matches pipeline, needed for validation).
                Only affects NSE. US always uses fmp.stock_eod.

    Returns:
        dict[symbol, list[dict]] where each dict has {epoch, open, close, volume}
        Sorted by epoch within each symbol.
    """
    warmup_epoch = start_epoch - warmup_days * 86400

    if exchange == "NSE":
        if source == "fmp":
            return _fetch_nse_fmp_universe(cr, warmup_epoch, end_epoch,
                                           price_threshold, turnover_threshold)
        return _fetch_nse_universe(cr, warmup_epoch, end_epoch,
                                   price_threshold, turnover_threshold, turnover_period)
    elif exchange in ("US", "NASDAQ", "NYSE"):
        return _fetch_us_universe(cr, warmup_epoch, end_epoch,
                                  mktcap_threshold or 1_000_000_000)
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")


def _fetch_nse_universe(cr, start_epoch, end_epoch,
                        price_threshold, turnover_threshold, turnover_period):
    """Fetch NSE universe from nse.nse_charting_day."""
    # Pass 1: get qualifying symbols (close > threshold, avg turnover > threshold)
    print("  Pass 1: finding qualifying NSE symbols...")
    sql = f"""
    SELECT symbol, AVG(close * volume) as avg_turnover, AVG(close) as avg_close
    FROM nse.nse_charting_day
    WHERE date_epoch >= {start_epoch} AND date_epoch <= {end_epoch}
    GROUP BY symbol
    HAVING AVG(close) > {price_threshold}
       AND AVG(close * volume) > {turnover_threshold}
    ORDER BY avg_turnover DESC
    """
    results = cr.query(sql, timeout=600, limit=100000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  No qualifying symbols found")
        return {}

    symbols = [r["symbol"] for r in results]
    print(f"  Found {len(symbols)} qualifying symbols")

    # Pass 2: fetch OHLCV for qualifying symbols
    print(f"  Pass 2: fetching OHLCV for {len(symbols)} symbols...")
    sym_list = ", ".join(f"'{s}'" for s in symbols)
    sql = f"""
    SELECT symbol, date_epoch, open, close, volume
    FROM nse.nse_charting_day
    WHERE symbol IN ({sym_list})
      AND date_epoch >= {start_epoch} AND date_epoch <= {end_epoch}
    ORDER BY symbol, date_epoch
    """
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        return {}

    return _build_price_data(results, epoch_col="date_epoch", close_col="close",
                             open_col="open", volume_col="volume")


def _fetch_nse_fmp_universe(cr, start_epoch, end_epoch,
                            price_threshold, turnover_threshold):
    """Fetch NSE universe from fmp.stock_eod (symbols with .NS suffix).

    Matches pipeline data source for validation. Symbols returned are bare
    (without .NS suffix) to match the rest of the lib.
    """
    # Pass 1: find qualifying .NS symbols
    print("  Pass 1: finding qualifying NSE symbols (FMP source)...")
    sql = f"""
    SELECT symbol,
           AVG(close * volume) as avg_turnover,
           AVG(close) as avg_close
    FROM fmp.stock_eod
    WHERE symbol LIKE '%.NS'
      AND CAST(dateEpoch AS BIGINT) >= {start_epoch}
      AND CAST(dateEpoch AS BIGINT) <= {end_epoch}
    GROUP BY symbol
    HAVING AVG(close) > {price_threshold}
       AND AVG(close * volume) > {turnover_threshold}
    ORDER BY avg_turnover DESC
    """
    results = cr.query(sql, timeout=600, limit=100000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        print("  No qualifying FMP NSE symbols found")
        return {}

    fmp_symbols = [r["symbol"] for r in results]
    print(f"  Found {len(fmp_symbols)} qualifying symbols")

    # Pass 2: fetch OHLCV
    print(f"  Pass 2: fetching OHLCV for {len(fmp_symbols)} symbols (FMP)...")
    sym_list = ", ".join(f"'{s}'" for s in fmp_symbols)
    sql = f"""
    SELECT symbol, CAST(dateEpoch AS BIGINT) AS date_epoch, open, close, volume
    FROM fmp.stock_eod
    WHERE symbol IN ({sym_list})
      AND CAST(dateEpoch AS BIGINT) >= {start_epoch}
      AND CAST(dateEpoch AS BIGINT) <= {end_epoch}
    ORDER BY symbol, date_epoch
    """
    results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                       memory_mb=16384, threads=6)
    if not results:
        return {}

    # Strip .NS suffix from symbols to match lib convention
    for r in results:
        if r["symbol"].endswith(".NS"):
            r["symbol"] = r["symbol"][:-3]

    return _build_price_data(results, epoch_col="date_epoch", close_col="close",
                             open_col="open", volume_col="volume")


def _fetch_us_universe(cr, start_epoch, end_epoch, mktcap_threshold):
    """Fetch US universe from fmp.stock_eod + fmp.profile."""
    # Pass 1: get qualifying symbols from profile
    print("  Pass 1: finding qualifying US symbols...")
    sql = f"""
    SELECT symbol
    FROM fmp.profile
    WHERE exchange IN ('NASDAQ', 'NYSE')
      AND isActivelyTrading = true
      AND marketCap > {mktcap_threshold}
    ORDER BY marketCap DESC
    """
    results = cr.query(sql, timeout=120, limit=100000, verbose=True,
                       memory_mb=8192, threads=4)
    if not results:
        print("  No qualifying US symbols found")
        return {}

    symbols = [r["symbol"] for r in results]
    print(f"  Found {len(symbols)} qualifying symbols")

    # Pass 2: fetch OHLCV -- limit to top 500 by market cap to avoid query size issues
    if len(symbols) > 500:
        symbols = symbols[:500]
        print(f"  Trimmed to top {len(symbols)} by market cap")

    # Batch symbols to stay under 50K char query limit
    BATCH_SIZE = 200
    all_results = []
    for batch_start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[batch_start:batch_start + BATCH_SIZE]
        sym_list = ", ".join(f"'{s}'" for s in batch)
        print(f"  Pass 2: fetching OHLCV batch {batch_start//BATCH_SIZE + 1} "
              f"({len(batch)} symbols)...")
        sql = f"""
        SELECT symbol, dateEpoch, open, adjClose, volume
        FROM fmp.stock_eod
        WHERE symbol IN ({sym_list})
          AND dateEpoch >= {start_epoch} AND dateEpoch <= {end_epoch}
        ORDER BY symbol, dateEpoch
        """
        batch_results = cr.query(sql, timeout=600, limit=10000000, verbose=True,
                                 memory_mb=16384, threads=6)
        if batch_results:
            all_results.extend(batch_results)

    if not all_results:
        return {}

    return _build_price_data(all_results, epoch_col="dateEpoch", close_col="adjClose",
                             open_col="open", volume_col="volume")


def _build_price_data(results, epoch_col, close_col, open_col, volume_col):
    """Convert query results to dict[symbol, list[dict]]."""
    price_data = {}
    for r in results:
        c = float(r.get(close_col) or 0)
        o = float(r.get(open_col) or 0)
        if c <= 0:
            continue
        sym = r["symbol"]
        if sym not in price_data:
            price_data[sym] = []
        price_data[sym].append({
            "epoch": int(r[epoch_col]),
            "open": o if o > 0 else c,
            "close": c,
            "volume": float(r.get(volume_col) or 0),
        })

    # Sort each symbol by epoch
    for sym in price_data:
        price_data[sym].sort(key=lambda x: x["epoch"])

    print(f"  Loaded {len(price_data)} symbols, "
          f"{sum(len(v) for v in price_data.values())} total bars")
    return price_data


def fetch_benchmark(cr, symbol, exchange, start_epoch, end_epoch,
                    warmup_days=100, source="native"):
    """Fetch benchmark close prices. Returns dict[epoch, close].

    Args:
        source: "native" uses nse.nse_charting_day, "fmp" uses fmp.stock_eod.
                Only affects NSE. US always uses fmp.stock_eod.
    """
    warmup = start_epoch - warmup_days * 86400
    if exchange == "NSE" and source != "fmp":
        sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}'
                    AND date_epoch >= {warmup} AND date_epoch <= {end_epoch}
                  ORDER BY date_epoch"""
        epoch_col, close_col = "date_epoch", "close"
    elif exchange == "NSE" and source == "fmp":
        fmp_symbol = f"{symbol}.NS"
        sql = f"""SELECT CAST(dateEpoch AS BIGINT) AS date_epoch, close
                  FROM fmp.stock_eod
                  WHERE symbol = '{fmp_symbol}'
                    AND CAST(dateEpoch AS BIGINT) >= {warmup}
                    AND CAST(dateEpoch AS BIGINT) <= {end_epoch}
                  ORDER BY date_epoch"""
        epoch_col, close_col = "date_epoch", "close"
    else:
        sql = f"""SELECT dateEpoch, adjClose FROM fmp.stock_eod
                  WHERE symbol = '{symbol}'
                    AND dateEpoch >= {warmup} AND dateEpoch <= {end_epoch}
                  ORDER BY dateEpoch"""
        epoch_col, close_col = "dateEpoch", "adjClose"

    results = cr.query(sql, timeout=300, limit=10000000, verbose=True,
                       memory_mb=8192, threads=4)
    prices = {}
    for r in results:
        c = float(r.get(close_col) or 0)
        if c > 0:
            prices[int(r[epoch_col])] = c
    print(f"  Benchmark {symbol} ({source}): {len(prices)} days")
    return prices


def fetch_sector_map(cr, exchange):
    """Fetch symbol -> sector mapping from fmp.profile."""
    if exchange == "NSE":
        sql = "SELECT symbol, sector FROM fmp.profile WHERE symbol LIKE '%.NS'"
    elif exchange in ("US", "NASDAQ", "NYSE"):
        sql = "SELECT symbol, sector FROM fmp.profile WHERE exchange IN ('NASDAQ', 'NYSE')"
    else:
        return {}

    results = cr.query(sql, timeout=60, limit=100000, verbose=False, format="json")
    if not results:
        return {}

    sector_map = {}
    for row in results:
        sym = row.get("symbol", "")
        sector = row.get("sector") or "Unknown"
        if exchange == "NSE" and sym.endswith(".NS"):
            sym = sym[:-3]
        sector_map[sym] = sector

    print(f"  Sector data: {len(sector_map)} stocks, "
          f"{len(set(sector_map.values()))} sectors")
    return sector_map


# ── Quality Filter ───────────────────────────────────────────────────────────

def compute_quality_universe(price_data, consecutive_years, min_yearly_return,
                             rescreen_days=63, start_epoch=None):
    """Compute which symbols pass the quality filter at each rescreen epoch.

    Quality = N consecutive years of positive trailing returns.

    Returns:
        dict[epoch, set[symbol]] -- quality universe at each rescreen point.
    """
    min_bars = consecutive_years * TRADING_DAYS_PER_YEAR + 10

    # Collect all unique epochs across all symbols (post start_epoch)
    all_epochs = set()
    for sym, bars in price_data.items():
        for b in bars:
            if start_epoch is None or b["epoch"] >= start_epoch:
                all_epochs.add(b["epoch"])
    sorted_epochs = sorted(all_epochs)

    if not sorted_epochs:
        return {}

    # Pre-build close arrays indexed by epoch for each symbol
    sym_closes = {}
    for sym, bars in price_data.items():
        if len(bars) < min_bars:
            continue
        sym_closes[sym] = {b["epoch"]: b["close"] for b in bars}

    # Build epoch -> sorted list of bars for quick lookback
    sym_bar_lists = {}
    for sym, bars in price_data.items():
        if sym in sym_closes:
            sym_bar_lists[sym] = bars  # already sorted by epoch

    quality_universe = {}
    last_screen_epoch = None
    rescreen_interval = rescreen_days * 86400

    for epoch in sorted_epochs:
        if last_screen_epoch is not None and (epoch - last_screen_epoch) < rescreen_interval:
            quality_universe[epoch] = quality_universe[last_screen_epoch]
            continue

        quality_set = set()
        for sym, bars in sym_bar_lists.items():
            # Find the bar index closest to this epoch
            idx = _find_epoch_idx(bars, epoch)
            if idx is None or idx < min_bars:
                continue

            # Check N consecutive years of positive returns
            passes = True
            for yr in range(consecutive_years):
                recent_idx = idx - yr * TRADING_DAYS_PER_YEAR
                older_idx = idx - (yr + 1) * TRADING_DAYS_PER_YEAR
                if recent_idx < 0 or older_idx < 0:
                    passes = False
                    break
                recent_close = bars[recent_idx]["close"]
                older_close = bars[older_idx]["close"]
                if older_close <= 0:
                    passes = False
                    break
                yr_return = recent_close / older_close - 1.0
                if yr_return <= min_yearly_return:
                    passes = False
                    break

            if passes:
                quality_set.add(sym)

        quality_universe[epoch] = quality_set
        last_screen_epoch = epoch

    pool_sizes = [len(v) for v in quality_universe.values() if v]
    avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
    print(f"  Quality filter: {consecutive_years}yr, min_return={min_yearly_return*100:.0f}%, "
          f"avg pool={avg_pool:.0f} stocks")
    return quality_universe


def _find_epoch_idx(bars, target_epoch):
    """Binary search for the closest bar at or before target_epoch."""
    lo, hi = 0, len(bars) - 1
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid]["epoch"] <= target_epoch:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return result


# ── Dip Detection ────────────────────────────────────────────────────────────

def compute_dip_entries(price_data, quality_universe, peak_lookback, dip_threshold,
                        start_epoch=None):
    """Find entry signals: quality stock with price dip from rolling peak.

    Returns list of entry dicts: {epoch, symbol, peak_price, entry_epoch, entry_price}
    Entry price = next day's open (MOC execution).
    """
    entries = []

    for sym, bars in price_data.items():
        if len(bars) < peak_lookback + 1:
            continue

        closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]
        epochs = [b["epoch"] for b in bars]

        for i in range(peak_lookback, len(bars) - 1):
            epoch = epochs[i]
            if start_epoch and epoch < start_epoch:
                continue

            # Check quality universe
            universe = quality_universe.get(epoch)
            if universe is None or sym not in universe:
                continue

            # Rolling peak
            window_start = max(0, i - peak_lookback + 1)
            rolling_peak = max(closes[window_start:i + 1])
            if rolling_peak <= 0:
                continue

            # Dip percentage
            dip_pct = (rolling_peak - closes[i]) / rolling_peak
            if dip_pct < dip_threshold:
                continue

            # Next day's open = entry price (MOC)
            entry_price = opens[i + 1]
            if entry_price <= 0:
                continue

            entries.append({
                "epoch": epoch,
                "symbol": sym,
                "peak_price": rolling_peak,
                "dip_pct": dip_pct,
                "entry_epoch": epochs[i + 1],
                "entry_price": entry_price,
            })

    entries.sort(key=lambda x: x["entry_epoch"])
    print(f"  Dip entries: {len(entries)} signals "
          f"(peak={peak_lookback}d, dip>={dip_threshold*100:.0f}%)")
    return entries


# ── Regime Filter ────────────────────────────────────────────────────────────

def compute_regime_epochs(benchmark_data, sma_period):
    """Compute bullish epochs where benchmark close > SMA(period).

    Returns set[epoch] of bullish days. Empty set = no filter (all days allowed).
    """
    if sma_period <= 0:
        return set()

    sorted_epochs = sorted(benchmark_data.keys())
    if len(sorted_epochs) < sma_period:
        return set()

    closes = [benchmark_data[e] for e in sorted_epochs]
    bull_epochs = set()

    running_sum = 0.0
    for i, epoch in enumerate(sorted_epochs):
        running_sum += closes[i]
        if i >= sma_period:
            running_sum -= closes[i - sma_period]
        if i >= sma_period - 1:
            sma = running_sum / sma_period
            if closes[i] > sma:
                bull_epochs.add(epoch)

    pct = len(bull_epochs) / len(sorted_epochs) * 100
    print(f"  Regime filter: SMA({sma_period}), "
          f"{len(bull_epochs)}/{len(sorted_epochs)} days bullish ({pct:.0f}%)")
    return bull_epochs


# ── RSI ──────────────────────────────────────────────────────────────────────

def compute_rsi_series(closes, period=14):
    """Compute RSI using exponential moving average. Returns list[float]."""
    n = len(closes)
    rsi = [50.0] * n  # default neutral

    if n < period + 1:
        return rsi

    # Seed: simple average of first `period` changes
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss > 0:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - 100.0 / (1.0 + rs)

    # EWM continuation
    alpha = 2.0 / (period + 1)
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0)
        loss = max(-delta, 0)
        avg_gain = avg_gain * (1 - alpha) + gain * alpha
        avg_loss = avg_loss * (1 - alpha) + loss * alpha
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)
        else:
            rsi[i] = 100.0

    return rsi


# ── Multi-Position Portfolio Simulator ───────────────────────────────────────

def simulate_portfolio(
    entries,
    price_data,
    benchmark_data,
    *,
    capital,
    max_positions,
    max_per_instrument=1,
    tsl_pct,
    max_hold_days,
    exchange,
    slippage=SLIPPAGE,
    regime_epochs=None,
    rsi_data=None,
    rsi_threshold=0,
    sector_map=None,
    max_per_sector=0,
    execution_prices=None,
    strategy_name="quality_dip_buy",
    description="",
    params=None,
    start_epoch=None,
):
    """Run multi-position portfolio simulation.

    Args:
        entries: list of dip entry signal dicts from compute_dip_entries()
        price_data: dict[symbol, list[{epoch, open, close}]]
        benchmark_data: dict[epoch, close] for benchmark
        capital: starting capital
        max_positions: max concurrent positions
        max_per_instrument: max positions per symbol (for tiered entries)
        tsl_pct: trailing stop-loss pct (0 = peak recovery only)
        max_hold_days: max holding period in calendar days (0 = no limit)
        exchange: exchange code for charges
        slippage: slippage fraction (default 5 bps)
        regime_epochs: set of bullish epochs (empty = no filter)
        rsi_data: dict[symbol, list[float]] RSI values aligned to price_data
        rsi_threshold: only enter if RSI < threshold (0 = off)
        sector_map: dict[symbol, sector] for sector diversification
        max_per_sector: max positions per sector (0 = off)
        execution_prices: dict[symbol, dict[epoch, price]] override entry prices
        strategy_name: for BacktestResult
        description: for BacktestResult
        params: dict for BacktestResult
        start_epoch: simulation start epoch

    Returns:
        BacktestResult
    """
    result = BacktestResult(
        strategy_name, params or {}, "PORTFOLIO", exchange, capital,
        slippage_bps=int(slippage * 10000), description=description,
    )

    # Build per-symbol epoch->close and epoch->open lookups
    sym_close = {}
    sym_open = {}
    for sym, bars in price_data.items():
        sym_close[sym] = {b["epoch"]: b["close"] for b in bars}
        sym_open[sym] = {b["epoch"]: b["open"] for b in bars}

    # Build per-symbol RSI lookup
    sym_rsi = {}
    if rsi_data:
        for sym, rsi_values in rsi_data.items():
            bars = price_data.get(sym, [])
            sym_rsi[sym] = {bars[i]["epoch"]: rsi_values[i]
                            for i in range(min(len(bars), len(rsi_values)))}

    # Build aligned trading calendar
    all_epochs = set()
    for sym, bars in price_data.items():
        for b in bars:
            if start_epoch is None or b["epoch"] >= start_epoch:
                all_epochs.add(b["epoch"])
    trading_days = sorted(all_epochs)

    if not trading_days:
        return result, []

    # Index entries by entry_epoch for fast lookup
    entries_by_epoch = {}
    for e in entries:
        ep = e["entry_epoch"]
        if ep not in entries_by_epoch:
            entries_by_epoch[ep] = []
        entries_by_epoch[ep].append(e)

    # Sort entries within each epoch by dip depth (deepest first)
    for ep in entries_by_epoch:
        entries_by_epoch[ep].sort(key=lambda x: -x["dip_pct"])

    # State
    cash = capital
    positions = {}  # key -> Position dict
    sector_counts = {}  # sector -> count
    pending_sells = []  # list of pos_keys to sell at next open
    day_wise_log = []  # for always-invested adjustment

    # Build benchmark values for the simulation period
    bm_epochs = []
    bm_values = []
    if benchmark_data:
        first_bm = None
        for ep in trading_days:
            if ep in benchmark_data:
                if first_bm is None:
                    first_bm = benchmark_data[ep]
                bm_epochs.append(ep)
                bm_values.append(benchmark_data[ep] / first_bm * capital)

    for epoch in trading_days:
        # ── 1. EXECUTE pending sells at today's open (MOC) ──
        if pending_sells:
            keys_to_sell = list(pending_sells)
            pending_sells.clear()
            for pos_key in keys_to_sell:
                if pos_key not in positions:
                    continue
                pos = positions[pos_key]
                sym = pos["symbol"]
                open_price = sym_open.get(sym, {}).get(epoch)
                if open_price is None or open_price <= 0:
                    # No open price today, try close
                    open_price = sym_close.get(sym, {}).get(epoch, pos["entry_price"])

                sell_val = pos["qty"] * open_price
                sell_ch = calculate_charges(exchange, sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
                sell_sl = sell_val * slippage
                cash += sell_val - sell_ch - sell_sl

                result.add_trade(
                    entry_epoch=pos["entry_epoch"],
                    exit_epoch=epoch,
                    entry_price=pos["entry_price"],
                    exit_price=open_price,
                    quantity=pos["qty"],
                    side="LONG",
                    charges=pos["buy_charges"] + sell_ch,
                    slippage=pos["buy_slippage"] + sell_sl,
                )

                if max_per_sector > 0 and sector_map:
                    sector = sector_map.get(pos["symbol"], "Unknown")
                    sector_counts[sector] = max(0, sector_counts.get(sector, 0) - 1)

                del positions[pos_key]

        # ── 2. EXECUTE pending entries at today's open ──
        # Entries are already at next-day's open via compute_dip_entries(),
        # which sets entry_epoch = epochs[i+1] and entry_price = opens[i+1].
        # The simulator processes entries when epoch == entry_epoch,
        # so we're buying at the correct day's open price.
        day_entries = entries_by_epoch.get(epoch, [])
        for entry in day_entries:
            if len(positions) >= max_positions:
                break

            sym = entry["symbol"]

            # Skip if pending sell for this symbol (avoid same-day re-entry)
            if any(positions.get(k, {}).get("symbol") == sym for k in pending_sells):
                continue

            # Check per-instrument limit
            sym_positions = sum(1 for p in positions.values() if p["symbol"] == sym)
            if sym_positions >= max_per_instrument:
                continue

            # Regime filter (checked on signal day, not entry day)
            if regime_epochs and entry["epoch"] not in regime_epochs:
                continue

            # RSI filter (checked on signal day)
            if rsi_threshold > 0 and sym_rsi:
                rsi_val = sym_rsi.get(sym, {}).get(entry["epoch"], 50)
                if rsi_val >= rsi_threshold:
                    continue

            # Sector filter
            if max_per_sector > 0 and sector_map:
                sector = sector_map.get(sym, "Unknown")
                if sector_counts.get(sector, 0) >= max_per_sector:
                    continue

            # Determine entry price
            if execution_prices and sym in execution_prices:
                entry_price = execution_prices[sym].get(epoch, entry["entry_price"])
            else:
                entry_price = entry["entry_price"]

            if entry_price <= 0:
                continue

            # Position sizing: equal weight based on current account value
            invested_value = sum(
                pos["qty"] * sym_close.get(pos["symbol"], {}).get(epoch, pos["entry_price"])
                for pos in positions.values()
            )
            account_value = cash + invested_value
            order_value = account_value / max_positions
            qty = int(order_value / entry_price)
            if qty <= 0:
                continue

            cost = qty * entry_price
            buy_ch = calculate_charges(exchange, cost, "EQUITY", "DELIVERY", "BUY_SIDE")
            buy_sl = cost * slippage
            total_cost = cost + buy_ch + buy_sl

            if total_cost > cash:
                continue

            cash -= total_cost
            pos_key = f"{sym}_{epoch}_{len(positions)}"
            positions[pos_key] = {
                "symbol": sym,
                "qty": qty,
                "entry_price": entry_price,
                "entry_epoch": epoch,
                "peak_price": entry["peak_price"],
                "trail_high": entry_price,
                "reached_peak": False,
                "buy_charges": buy_ch,
                "buy_slippage": buy_sl,
            }

            if max_per_sector > 0 and sector_map:
                sector = sector_map.get(sym, "Unknown")
                sector_counts[sector] = sector_counts.get(sector, 0) + 1

        # ── 3. MTM all positions at today's close ──
        invested_value = 0
        for pos in positions.values():
            close = sym_close.get(pos["symbol"], {}).get(epoch)
            if close:
                invested_value += pos["qty"] * close
            else:
                invested_value += pos["qty"] * pos["entry_price"]

        result.add_equity_point(epoch, cash + invested_value)
        day_wise_log.append({
            "epoch": epoch,
            "margin_available": cash,
            "invested_value": invested_value,
        })

        # ── 4. CHECK EXIT SIGNALS at today's close (execute at next open) ──
        for key, pos in positions.items():
            if key in pending_sells:
                continue
            sym = pos["symbol"]
            close = sym_close.get(sym, {}).get(epoch)
            if close is None:
                continue

            # Update trailing high
            if close > pos["trail_high"]:
                pos["trail_high"] = close

            hold_days = (epoch - pos["entry_epoch"]) / 86400
            should_exit = False

            if tsl_pct == 0:
                # Peak recovery exit
                if close >= pos["peak_price"]:
                    should_exit = True
                if max_hold_days > 0 and hold_days >= max_hold_days:
                    should_exit = True
            else:
                if close >= pos["peak_price"]:
                    pos["reached_peak"] = True
                if pos["reached_peak"] and close <= pos["trail_high"] * (1 - tsl_pct / 100.0):
                    should_exit = True
                if max_hold_days > 0 and hold_days >= max_hold_days:
                    should_exit = True

            if should_exit:
                pending_sells.append(key)

    # Close any remaining positions at last day's close
    last_epoch = trading_days[-1] if trading_days else 0
    for key, pos in list(positions.items()):
        close = sym_close.get(pos["symbol"], {}).get(last_epoch, pos["entry_price"])
        sell_val = pos["qty"] * close
        sell_ch = calculate_charges(exchange, sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
        sell_sl = sell_val * slippage
        cash += sell_val - sell_ch - sell_sl
        result.add_trade(
            entry_epoch=pos["entry_epoch"],
            exit_epoch=last_epoch,
            entry_price=pos["entry_price"],
            exit_price=close,
            quantity=pos["qty"],
            side="LONG",
            charges=pos["buy_charges"] + sell_ch,
            slippage=pos["buy_slippage"] + sell_sl,
        )

    # Set benchmark
    if bm_epochs and bm_values:
        result.set_benchmark_values(bm_epochs, bm_values)

    return result, day_wise_log


# ── Always-Invested Adjustment ───────────────────────────────────────────────

def compute_always_invested(day_wise_log, benchmark_data, capital):
    """Adjust equity curve: idle cash earns benchmark returns.

    Uses the margin/invested breakdown from simulate_portfolio() to accurately
    compound benchmark returns on idle cash while preserving actual trade flows.

    Algorithm (from run_quality_dip_v2.py):
        cash_flow[t] = margin[t] - margin[t-1]  (net from trade entries/exits)
        adjusted_margin[t] = adjusted_margin[t-1] * (1 + bm_ret) + cash_flow[t]
        adjusted_equity[t] = invested_value[t] + adjusted_margin[t]

    Args:
        day_wise_log: list of {epoch, margin_available, invested_value} from simulator
        benchmark_data: dict[epoch, close] for benchmark
        capital: starting capital

    Returns:
        dict with adjusted metrics or None if insufficient data.
    """
    if not day_wise_log or len(day_wise_log) < 2:
        return None

    # Build daily benchmark returns
    sorted_bm = sorted(benchmark_data.keys())
    bm_returns = {}
    for i in range(1, len(sorted_bm)):
        prev = benchmark_data[sorted_bm[i - 1]]
        curr = benchmark_data[sorted_bm[i]]
        if prev > 0:
            bm_returns[sorted_bm[i]] = curr / prev - 1.0

    # Compute adjusted equity curve
    adjusted_margin = day_wise_log[0]["margin_available"]
    adjusted_values = []

    for i, day in enumerate(day_wise_log):
        epoch = day["epoch"]
        invested = day["invested_value"]
        margin = day["margin_available"]

        if i == 0:
            adjusted_values.append(invested + adjusted_margin)
            continue

        # Cash flow: difference in original margin (captures trade entries/exits)
        prev_margin = day_wise_log[i - 1]["margin_available"]
        cash_flow = margin - prev_margin

        # Apply benchmark return to adjusted idle cash, then add cash flow
        bm_ret = bm_returns.get(epoch, 0.0)
        adjusted_margin = adjusted_margin * (1 + bm_ret) + cash_flow

        adjusted_values.append(invested + adjusted_margin)

    # Compute metrics on adjusted curve
    from lib.metrics import compute_metrics

    daily_returns = []
    for i in range(1, len(adjusted_values)):
        if adjusted_values[i - 1] > 0:
            daily_returns.append(adjusted_values[i] / adjusted_values[i - 1] - 1)
        else:
            daily_returns.append(0.0)

    if not daily_returns:
        return None

    bench_returns = [bm_returns.get(day_wise_log[i + 1]["epoch"], 0.0)
                     for i in range(len(daily_returns))]

    metrics = compute_metrics(daily_returns, bench_returns, periods_per_year=252)
    port = metrics.get("portfolio", {})

    return {
        "cagr_adj": port.get("cagr"),
        "max_drawdown_adj": port.get("max_drawdown"),
        "calmar_adj": port.get("calmar_ratio"),
        "sharpe_adj": port.get("sharpe_ratio"),
    }

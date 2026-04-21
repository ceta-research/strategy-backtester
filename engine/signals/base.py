"""Base protocol and shared utilities for signal generators.

Each strategy implements SignalGenerator to produce orders from OHLCV data.
The pipeline dispatches to the correct generator based on strategy_type in config.
"""

from typing import Protocol

import polars as pl

from engine.config_loader import get_scanner_config_iterator


# ---------------------------------------------------------------------------
# SignalGenerator protocol
# ---------------------------------------------------------------------------

class SignalGenerator(Protocol):
    """Interface that every EOD strategy must implement."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        """Produce orders from raw OHLCV data.

        Must return DataFrame with columns:
            instrument, entry_epoch, exit_epoch,
            entry_price, exit_price, entry_volume, exit_volume,
            scanner_config_ids, entry_config_ids, exit_config_ids
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_STRATEGY_REGISTRY: dict[str, type] = {}


def register_strategy(name: str, cls: type):
    _STRATEGY_REGISTRY[name] = cls


def get_signal_generator(strategy_type: str) -> SignalGenerator:
    """Look up and instantiate the signal generator for a strategy type."""
    if strategy_type not in _STRATEGY_REGISTRY:
        available = ", ".join(sorted(_STRATEGY_REGISTRY.keys()))
        raise ValueError(f"Unknown strategy_type '{strategy_type}'. Available: {available}")
    return _STRATEGY_REGISTRY[strategy_type]()


# ---------------------------------------------------------------------------
# Shared utilities (extracted from scanner.py / order_generator.py)
# ---------------------------------------------------------------------------

def fill_missing_dates(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Fill missing trading dates per instrument so rolling windows work correctly."""
    min_epoch = df_tick_data["date_epoch"].min()
    max_epoch = df_tick_data["date_epoch"].max()

    all_epochs = list(range(min_epoch, max_epoch + 86400, 86400))
    epoch_df = pl.DataFrame({"date_epoch": all_epochs}).cast({"date_epoch": pl.Int64})

    instruments = df_tick_data.select("instrument").unique()
    full_grid = instruments.join(epoch_df, how="cross")

    existing = df_tick_data.select("instrument", "date_epoch")
    missing = full_grid.join(existing, on=["instrument", "date_epoch"], how="anti")

    if missing.height > 0:
        missing = missing.with_columns([
            pl.col("instrument").str.split(":").list.get(0).alias("exchange"),
            pl.col("instrument").str.split(":").list.get(1).alias("symbol"),
        ])
        df_tick_data = pl.concat([df_tick_data, missing], how="diagonal")
        df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])

    return df_tick_data


def backfill_close(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Backward-fill close prices within each instrument group."""
    return df_tick_data.with_columns(
        pl.col("close").backward_fill().over("instrument").alias("close")
    )


def add_next_day_values(df_tick_data: pl.DataFrame) -> pl.DataFrame:
    """Add next-day epoch, open, and volume columns. Drop last row per instrument."""
    df_tick_data = df_tick_data.sort(["instrument", "date_epoch"]).with_columns([
        pl.col("date_epoch").shift(-1).over("instrument").alias("next_epoch"),
        pl.col("open").shift(-1).over("instrument").alias("next_open"),
        pl.col("volume").shift(-1).over("instrument").alias("next_volume"),
    ])
    return df_tick_data.filter(pl.col("next_epoch").is_not_null())


def sanitize_orders(df_orders: pl.DataFrame, min_entry_price: float = 0.10,
                     max_return_mult: float = 5.0,
                     diagnostic_threshold: float = 20.0) -> pl.DataFrame:
    """Remove bad orders caused by data quality issues (splits, zero prices, etc).

    Applied in pipeline after signal generation, before simulation.

    Filters:
        1. entry_price <= min_entry_price (sub-penny, zero, or near-zero)
        2. exit_price <= 0
        3. Per-trade return > max_return_mult (caps exit_price; catches reverse splits,
           price unit mismatches, and other data errors)

    Args:
        df_orders: DataFrame with entry_price, exit_price columns.
        min_entry_price: Minimum valid entry price (default $0.10).
        max_return_mult: Maximum allowed exit/entry ratio (default 5.0 = 500%).
        diagnostic_threshold: If > 0, log the count of orders that would
            exceed this ratio without dropping them. Surfaces suspicious
            orders when the cap is permissive (e.g. 999x). Pass 0 to
            disable. Default 20.0 (2000%).

    Returns:
        Sanitized DataFrame with bad rows removed and extreme exits capped.
    """
    if df_orders.is_empty():
        return df_orders

    before = df_orders.height

    # Filter out zero / near-zero entry prices
    df_orders = df_orders.filter(pl.col("entry_price") > min_entry_price)

    # Filter out zero / negative exit prices
    df_orders = df_orders.filter(pl.col("exit_price") > 0)

    # Count pre-cap so the log reflects raw signal-gen output, not the
    # post-cap state.
    diag_count = 0
    if diagnostic_threshold and diagnostic_threshold > 0:
        diag_count = int(
            df_orders.filter(
                pl.col("exit_price") > pl.col("entry_price") * diagnostic_threshold
            ).height
        )

    # Cap extreme returns (reverse splits, data errors)
    max_exit = pl.col("entry_price") * max_return_mult
    df_orders = df_orders.with_columns(
        pl.when(pl.col("exit_price") > max_exit)
        .then(max_exit)
        .otherwise(pl.col("exit_price"))
        .alias("exit_price")
    )

    removed = before - df_orders.height
    if removed > 0:
        print(f"  Sanitized: removed {removed} bad orders ({removed/before*100:.1f}%), "
              f"capped returns at {max_return_mult:.0f}x")
    if diag_count > 0:
        print(
            f"  Sanitize diagnostic: {diag_count} orders exceed "
            f"{diagnostic_threshold:.0f}x return (current cap: "
            f"{max_return_mult:.0f}x — not dropped). Review for data quality."
        )

    return df_orders


def run_scanner(context: dict, df_tick_data: pl.DataFrame) -> tuple[dict[str, set], "pl.DataFrame"]:
    """Run the standard scanner phase: liquidity + price filter, return tagged df.

    This is the shared scanner used by all dip-buy / momentum / breakout generators.
    Skips fill_missing_dates / backfill_close / n_day_gain (use apply_liquidity_filter
    for strategies that need those).

    Returns:
        (shortlist_tracker, df_trimmed)
        - shortlist_tracker: {scanner_config_id: set(uid_strings)}
        - df_trimmed: df filtered to start_epoch with scanner_config_ids column
    """
    import time as _time

    t0 = _time.time()
    df = df_tick_data.clone()
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])

    shortlist_tracker = {}
    for scanner_config in get_scanner_config_iterator(context):
        df_scan = df.clone()

        filter_exprs = []
        for instrument in scanner_config["instruments"]:
            if instrument["symbols"]:
                filter_exprs.append(
                    (pl.col("exchange") == instrument["exchange"])
                    & (pl.col("symbol").is_in(instrument["symbols"]))
                )
            else:
                filter_exprs.append(pl.col("exchange") == instrument["exchange"])
        if filter_exprs:
            combined = filter_exprs[0]
            for expr in filter_exprs[1:]:
                combined = combined | expr
            df_scan = df_scan.filter(combined)

        atc = scanner_config["avg_day_transaction_threshold"]
        df_scan = df_scan.with_columns(
            (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
        )
        df_scan = df_scan.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("avg_txn_turnover")
            .rolling_mean(window_size=atc["period"], min_samples=1)
            .over("instrument")
            .alias("avg_txn_turnover")
        )
        df_scan = df_scan.drop_nulls()
        df_scan = df_scan.filter(pl.col("close") > scanner_config["price_threshold"])
        df_scan = df_scan.filter(pl.col("avg_txn_turnover") > atc["threshold"])

        uid_series = df_scan.select(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )["uid"]
        shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

    df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
    df_trimmed = df_trimmed.with_columns(
        (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
    )
    signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
    uids = df_trimmed["uid"].to_list()
    uid_to_signals = {}
    for uid in uids:
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None
    df_trimmed = df_trimmed.with_columns(
        pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
    )

    elapsed = round(_time.time() - t0, 2)
    print(f"  Scanner: {elapsed}s, {df_trimmed.height} rows")

    return shortlist_tracker, df_trimmed


def walk_forward_exit(
    epochs: list, closes: list, start_idx: int,
    entry_epoch: int, entry_price: float, peak_price: float,
    trailing_stop_fraction: float, max_hold_days: int,
    *,
    require_peak_recovery: bool,
    opens: list = None,
) -> tuple:
    """Walk forward from entry to find exit via TSL (with optional peak recovery gate).

    Signal is evaluated at close[T]. If opens is provided, exit at open[T+1]
    (MOC execution matching standalone behavior). Otherwise exit at close[T].

    Args:
        epochs: list of date_epoch values for the instrument
        closes: list of close prices for the instrument
        start_idx: index in epochs/closes where entry_epoch lives
        entry_epoch: entry date epoch
        entry_price: entry price
        peak_price: rolling peak price at entry (target for recovery)
        trailing_stop_fraction: trailing stop-loss as a FRACTION (0.05 = 5%,
            0 = no TSL / peak-recovery-only mode). Explicitly NOT a percent —
            passing 5.0 would silently disable TSL (WFE-1 footgun noted in
            code review 2026-04-21). `engine.exits.trailing_stop` uses
            percent units; keep the two conventions distinguishable at the
            parameter name level.
        max_hold_days: max holding period in calendar days (0 = no limit)
        require_peak_recovery: keyword-only, REQUIRED.
            True  — TSL only activates after price recovers to peak_price.
                    Correct for dip-buy strategies (entry is below peak).
            False — TSL is active from entry.
                    Correct for breakout/momentum strategies (entry IS at peak).
            Audit P0 #10: pre-fix, this had a default of True, and breakout
            signals silently inherited dip-buy semantics. Making it mandatory
            forces each signal to make the choice explicit.
        opens: optional list of open prices; if provided, exit at next-day open

    Returns:
        (exit_epoch, exit_price) or (None, None) if no exit found and no data remains
    """
    # Runtime guard: trailing_stop_fraction MUST be a fraction (0 <= f <= 1).
    # Historical WFE-1 footgun: passing 5.0 ("5 percent") computes
    # `trail_high * (1 - 5.0) = -4 * trail_high`, which the `c <= ...` test
    # never satisfies for positive c — silently disabling TSL. Raise instead.
    if trailing_stop_fraction < 0 or trailing_stop_fraction > 1:
        raise ValueError(
            f"trailing_stop_fraction must be in [0, 1] (e.g. 0.05 for 5%); "
            f"got {trailing_stop_fraction}. If you have a percent value, "
            f"divide by 100 at the call site."
        )

    def _exit_at(j):
        """Return exit epoch/price: next-day open if available, else close[j]."""
        if opens is not None and j + 1 < len(epochs):
            next_open = opens[j + 1]
            if next_open is not None and next_open > 0:
                return epochs[j + 1], next_open
        return epochs[j], closes[j]

    trail_high = entry_price

    if trailing_stop_fraction == 0:
        for j in range(start_idx, len(epochs)):
            c = closes[j]
            if c is None:
                continue
            hold_days = (epochs[j] - entry_epoch) / 86400
            if max_hold_days > 0 and hold_days >= max_hold_days:
                return _exit_at(j)
            if c >= peak_price:
                return _exit_at(j)
    else:
        reached_peak = not require_peak_recovery  # If no gate, TSL active immediately
        for j in range(start_idx, len(epochs)):
            c = closes[j]
            if c is None:
                continue
            if c > trail_high:
                trail_high = c
            hold_days = (epochs[j] - entry_epoch) / 86400
            if max_hold_days > 0 and hold_days >= max_hold_days:
                return _exit_at(j)
            if c >= peak_price:
                reached_peak = True
            if reached_peak and c <= trail_high * (1 - trailing_stop_fraction):
                return _exit_at(j)

    # No exit trigger found - exit at last available bar
    if len(epochs) > start_idx:
        return epochs[-1], closes[-1]

    return None, None


def compute_direction_score(df_tick_data: pl.DataFrame, n_day_ma: int = 3) -> dict:
    """Compute market breadth direction score per epoch.

    For each date, calculates what fraction of instruments have close > N-day MA.
    Returns dict mapping epoch -> direction_score (0.0 to 1.0).
    Matches ATO_Simulator's direction_score filter logic.

    Args:
        df_tick_data: Full tick data (all instruments)
        n_day_ma: Moving average period (default 3, matching ATO config)

    Returns:
        dict of {epoch: float} where float is fraction of instruments in uptrend
    """
    df = df_tick_data.select(["instrument", "date_epoch", "close"]).sort(
        ["instrument", "date_epoch"]
    )

    # N-day MA per instrument
    df = df.with_columns(
        pl.col("close")
        .rolling_mean(window_size=n_day_ma, min_samples=1)
        .over("instrument")
        .alias("n_day_ma")
    )

    # Uptrend flag: 1 if close > MA, else 0
    df = df.with_columns(
        pl.when(pl.col("close") > pl.col("n_day_ma"))
        .then(1.0)
        .otherwise(0.0)
        .alias("uptrend")
    )

    # Aggregate: mean uptrend across all instruments per date
    direction_scores = (
        df.group_by("date_epoch", maintain_order=True)
        .agg(pl.col("uptrend").mean().alias("direction_score"))
        .sort("date_epoch")
    )

    return dict(
        zip(
            direction_scores["date_epoch"].to_list(),
            direction_scores["direction_score"].to_list(),
        )
    )


def compute_rsi(series: pl.Expr, period: int) -> pl.Expr:
    """Compute RSI using exponential moving average of gains/losses."""
    delta = series.diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    avg_gain = gain.ewm_mean(span=period, adjust=False, min_samples=period)
    avg_loss = loss.ewm_mean(span=period, adjust=False, min_samples=period)
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def build_regime_filter(df_tick_data, regime_instrument, regime_sma_period):
    """Build set of epochs where the regime instrument is above its SMA.

    Returns set of date_epochs where regime is bullish (close > SMA).
    Empty set means all epochs are allowed (no filter).
    """
    if not regime_instrument or regime_sma_period <= 0:
        return set()

    df_regime = df_tick_data.filter(
        pl.col("instrument") == regime_instrument
    ).sort("date_epoch")

    if df_regime.is_empty():
        print(f"  Warning: regime instrument {regime_instrument} not found in data")
        return set()

    df_regime = df_regime.with_columns(
        pl.col("close")
        .rolling_mean(window_size=regime_sma_period, min_samples=regime_sma_period)
        .alias("regime_sma")
    )

    bull_epochs = set(
        df_regime.filter(
            (pl.col("close") > pl.col("regime_sma"))
            & (pl.col("regime_sma").is_not_null())
        )["date_epoch"].to_list()
    )

    total = df_regime.filter(pl.col("regime_sma").is_not_null()).height
    pct = len(bull_epochs) / total * 100 if total > 0 else 0
    print(f"  Regime filter: {regime_instrument} > SMA({regime_sma_period}), "
          f"{len(bull_epochs)}/{total} days bullish ({pct:.0f}%)")

    return bull_epochs


# ---------------------------------------------------------------------------
# Fundamental data utilities (shared by dip-buy / breakout strategies)
# ---------------------------------------------------------------------------

FILING_LAG_DAYS = 45


def fetch_fundamentals(exchanges):
    """Fetch FY fundamental ratios from fmp.financial_ratios.

    Returns dict[bare_symbol, list[{epoch, roe, de, pe}]] sorted by epoch.
    """
    from lib.cr_client import CetaResearch

    cr = CetaResearch()

    suffix_filters = []
    suffixes = []
    for exchange in exchanges:
        if exchange == "NSE":
            suffix_filters.append("symbol LIKE '%.NS'")
            suffixes.append(".NS")
        elif exchange == "US":
            suffix_filters.append(
                "symbol NOT LIKE '%.%' AND symbol NOT LIKE '%-%'"
            )

    if not suffix_filters:
        return {}

    where_clause = " OR ".join(f"({f})" for f in suffix_filters)
    sql = f"""
    SELECT symbol, CAST(dateEpoch AS BIGINT) AS dateEpoch,
           netIncomePerShare, shareholdersEquityPerShare,
           debtToEquityRatio, priceToEarningsRatio
    FROM fmp.financial_ratios
    WHERE ({where_clause})
      AND period = 'FY'
      AND shareholdersEquityPerShare IS NOT NULL
      AND shareholdersEquityPerShare > 0
    ORDER BY symbol, dateEpoch
    """

    print("  Fetching fundamental ratios (FY)...")
    try:
        results = cr.query(
            sql, timeout=600, limit=10000000, verbose=False,
            memory_mb=16384, threads=6,
        )
    except Exception as e:
        print(f"  WARNING: Could not fetch fundamentals: {e}")
        return {}

    if not results:
        print("  WARNING: No fundamental data fetched")
        return {}

    fundamentals = {}
    for r in results:
        sym = r["symbol"]
        for suffix in suffixes:
            if sym.endswith(suffix):
                sym = sym[: -len(suffix)]
                break

        epoch = int(r.get("dateEpoch") or 0)
        if epoch <= 0:
            continue

        ni = r.get("netIncomePerShare")
        eq = r.get("shareholdersEquityPerShare")
        roe = (
            (float(ni) / float(eq) * 100)
            if (ni is not None and eq and float(eq) > 0)
            else None
        )
        de = r.get("debtToEquityRatio")
        pe = r.get("priceToEarningsRatio")

        if sym not in fundamentals:
            fundamentals[sym] = []
        fundamentals[sym].append({
            "epoch": epoch,
            "roe": roe,
            "de": float(de) if de is not None else None,
            "pe": float(pe) if pe is not None else None,
        })

    for sym in fundamentals:
        fundamentals[sym].sort(key=lambda x: x["epoch"])

    print(
        f"  Loaded fundamentals for {len(fundamentals)} symbols, "
        f"{sum(len(v) for v in fundamentals.values())} data points"
    )
    return fundamentals


def get_fundamental_at(fundamentals, symbol, epoch, lag_days=FILING_LAG_DAYS):
    """Get latest fundamental data available before lag-adjusted epoch."""
    records = fundamentals.get(symbol)
    if not records:
        return None
    lag_epoch = epoch - lag_days * 86400
    best = None
    for rec in records:
        if rec["epoch"] <= lag_epoch:
            best = rec
        else:
            break
    return best


def passes_fundamental_filter(
    fundamentals, symbol, epoch, roe_threshold, pe_threshold, de_threshold,
    missing_mode,
):
    """Check if a stock passes fundamental filters at a given epoch."""
    if roe_threshold <= 0 and pe_threshold <= 0 and de_threshold <= 0:
        return True

    fund = get_fundamental_at(fundamentals, symbol, epoch)
    if fund is None:
        return missing_mode != "skip"

    if roe_threshold > 0:
        roe = fund.get("roe")
        if roe is not None and roe < roe_threshold:
            return False

    if de_threshold > 0:
        de = fund.get("de")
        if de is not None and de > de_threshold:
            return False

    if pe_threshold > 0:
        pe = fund.get("pe")
        if pe is not None and (pe <= 0 or pe > pe_threshold):
            return False

    return True


EMPTY_ORDERS_SCHEMA = {
    "instrument": pl.Utf8,
    "entry_epoch": pl.Float64,
    "exit_epoch": pl.Float64,
    "entry_price": pl.Float64,
    "exit_price": pl.Float64,
    "entry_volume": pl.Float64,
    "exit_volume": pl.Float64,
    "scanner_config_ids": pl.Utf8,
    "entry_config_ids": pl.Utf8,
    "exit_config_ids": pl.Utf8,
}

ORDER_COLUMNS = [
    "instrument", "entry_epoch", "exit_epoch",
    "entry_price", "exit_price", "entry_volume", "exit_volume",
    "scanner_config_ids", "entry_config_ids", "exit_config_ids",
]


def empty_orders() -> pl.DataFrame:
    """Return an empty DataFrame with the standard order schema."""
    return pl.DataFrame(schema=EMPTY_ORDERS_SCHEMA)


def validate_orders(df_orders: pl.DataFrame, strategy_type: str) -> None:
    """Validate that generate_orders() output has required columns.

    Raises ValueError with a clear message if columns are missing,
    so bugs in signal generators surface immediately instead of causing
    cryptic errors deep in the simulator.
    """
    missing = [col for col in ORDER_COLUMNS if col not in df_orders.columns]
    if missing:
        raise ValueError(
            f"{strategy_type}.generate_orders() output missing columns: {', '.join(missing)}. "
            f"Required: {ORDER_COLUMNS}"
        )


def finalize_orders(all_order_rows: list[dict], elapsed: float) -> pl.DataFrame:
    """Convert order rows to sorted DataFrame, or return empty if none."""
    if not all_order_rows:
        print(f"  Signal gen: {elapsed}s, 0 orders")
        return empty_orders()

    df_orders = pl.DataFrame(all_order_rows)
    # Select standard columns plus any extra columns present (e.g. dip_pct for ranking)
    cols = [c for c in ORDER_COLUMNS if c in df_orders.columns]
    extra_cols = [c for c in df_orders.columns if c not in ORDER_COLUMNS]
    df_orders = df_orders.select(cols + extra_cols).sort(["instrument", "entry_epoch", "exit_epoch"])
    print(f"  Signal gen: {elapsed}s, {df_orders.height} orders")
    return df_orders


def apply_liquidity_filter(df_tick_data: pl.DataFrame, context: dict) -> pl.DataFrame:
    """Apply scanner-phase liquidity/price filters and tag scanner_config_ids.

    Runs the standard scanner logic: exchange/symbol filter, rolling avg turnover,
    n-day gain, price threshold. Returns DataFrame with scanner_config_ids column.
    """
    df_tick_data = fill_missing_dates(df_tick_data)
    df_tick_data = backfill_close(df_tick_data)
    shortlist_tracker = {}

    for scanner_config in get_scanner_config_iterator(context):
        df = df_tick_data.clone()

        # Exchange/symbol filter
        filter_exprs = []
        for instrument in scanner_config["instruments"]:
            if instrument["symbols"]:
                filter_exprs.append(
                    (pl.col("exchange") == instrument["exchange"])
                    & (pl.col("symbol").is_in(instrument["symbols"]))
                )
            else:
                filter_exprs.append(pl.col("exchange") == instrument["exchange"])

        if filter_exprs:
            combined = filter_exprs[0]
            for expr in filter_exprs[1:]:
                combined = combined | expr
            df = df.filter(combined)

        # Rolling avg turnover
        atc = scanner_config["avg_day_transaction_threshold"]
        df = df.with_columns(
            (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
        )
        df = df.sort(["instrument", "date_epoch"]).with_columns(
            pl.col("avg_txn_turnover")
            .rolling_mean(window_size=atc["period"], min_samples=1)
            .over("instrument")
            .alias("avg_txn_turnover")
        )

        # N-day gain
        ngc = scanner_config["n_day_gain_threshold"]
        df = df.with_columns(
            pl.col("close").shift(ngc["n"] - 1).over("instrument").alias("shifted_close")
        )
        df = df.with_columns(
            ((pl.col("close") - pl.col("shifted_close")) * 100.0 / pl.col("shifted_close")).alias("gain")
        )

        df = df.drop_nulls()
        df = df.filter(pl.col("close") > scanner_config["price_threshold"])
        df = df.filter(pl.col("avg_txn_turnover") > atc["threshold"])
        df = df.filter(pl.col("gain") > ngc["threshold"])

        uid_series = df.select(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )["uid"]
        shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

    # Trim prefetch, add UIDs, tag scanner signals
    start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
    df_tick_data = df_tick_data.filter(pl.col("date_epoch") >= start_epoch)
    df_tick_data = df_tick_data.drop_nulls()
    df_tick_data = df_tick_data.with_columns(
        (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
    )

    # Build scanner_config_ids column
    signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
    uids = df_tick_data["uid"].to_list()
    uid_to_signals = {}
    for uid in uids:
        signals = [str(k) for k, v in signal_sets.items() if uid in v]
        uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None

    signal_series = pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
    df_tick_data = df_tick_data.with_columns(signal_series)

    return df_tick_data

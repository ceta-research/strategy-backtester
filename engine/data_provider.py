"""Data providers: fetch OHLCV data from CR API or local parquet.

CRDataProvider fetches from Ceta Research API.
ParquetDataProvider reads local parquet files (ATO_Simulator format).
FMPParquetDataProvider reads local FMP EOD parquet files.
DuckDBParquetDataProvider reads local FMP parquet via DuckDB SQL.
PolarsParquetDataProvider reads local FMP parquet via Polars lazy scan.
BhavcopyDataProvider reads nse.nse_bhavcopy_historical (UNADJUSTED prices,
  survivorship-bias-free, 5072 symbols).
NseChartingDataProvider reads nse.nse_charting_day (split-adjusted, ~2447
  symbols; matches the standalone champion strategies).

Corporate-action handling (audit P4.3, 2026-04-21):
  - CRDataProvider / FMPParquetDataProvider / DuckDBParquetDataProvider /
    PolarsParquetDataProvider all SELECT `close` from `fmp.stock_eod`.
    FMP's `close` is SPLIT-ADJUSTED but NOT dividend-adjusted. The separate
    `adjClose` column (split + dividend adjusted) is NOT fetched. The
    simulator, scanner, and ranking all consume `close` — so long-hold
    strategies understate total return by roughly the dividend yield over
    the holding period (typically 1-3%/year on the Indian / US universes).
  - ParquetDataProvider reads ATO_Simulator-format kite parquet files,
    which carry split-adjusted prices in the `close` column.
  - BhavcopyDataProvider is EXPLICITLY UNADJUSTED (see its docstring).
    Oscillation filter not applied because legitimate split-day jumps are
    expected. Consumers must apply corporate actions externally.
  - NseChartingDataProvider reads nse.nse_charting_day which is also
    split-adjusted.

Missing-data handling (audit P4.4, 2026-04-21):
  All providers return whatever the source yields: missing trading days
  appear as ABSENT ROWS, not null rows. The scanner (`engine.scanner.
  fill_missing_dates`) is the single canonical place that inserts rows
  for gap days, then backward-fills `close` across null rows. Signal
  generators downstream should never assume every calendar-day row exists
  unless they have gone through the scanner.
"""

import io
import logging
import os
import sys
from datetime import datetime, timezone

import polars as pl

# Add parent dir to path so lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cr_client import CetaResearch

from engine.constants import SECONDS_IN_ONE_DAY

_logger = logging.getLogger(__name__)


def _epoch_to_date_str(epoch):
    """Convert epoch to YYYY-MM-DD string for display."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def remove_price_oscillations(df: pl.DataFrame, price_col: str = "close",
                              date_col: str = "date_epoch",
                              symbol_col: str = "instrument",
                              spike_threshold: float = 2.0,
                              mild_threshold: float = 1.3,
                              min_mild_count: int = 5,
                              verbose: bool = True) -> pl.DataFrame:
    """Remove rows with oscillating price data from a Polars DataFrame.

    FMP EOD data contains bad rows where ALL price fields spike for 1-2 days
    then revert (phantom holiday rows, broken split adjustments). ~2,500 symbols
    affected across 19 exchanges, ~137,000 bad days.

    Two-tier detection (single pass — iterating creates false positives by
    changing neighbors after each removal):
      Tier 1 (spike_threshold, default 2.0x): Any spike+revert is flagged.
        A 100%+ move that fully reverts in 1-2 days is always bad data.
      Tier 2 (mild_threshold, default 1.3x): Only flagged if the symbol has
        >= min_mild_count such oscillations. Catches persistent oscillation
        (e.g. BSAC 30-40% flips, 70+ times) without flagging legitimate
        earnings-day volatility.

    Detection uses ±1 and ±2 neighbor windows:
      ±1: price[N]/price[N-1] spikes AND price[N-1] ≈ price[N+1] (revert)
      ±2: price[N] far from both price[N-2] and price[N+2], which agree

    Args:
        df: Polars DataFrame with price and date columns
        price_col: column to check for oscillations (default "close")
        date_col: date/epoch column for ordering (default "date_epoch")
        symbol_col: symbol grouping column (default "instrument")
        spike_threshold: tier 1 threshold (default 2.0 = 100% move)
        mild_threshold: tier 2 threshold (default 1.3 = 30% move)
        min_mild_count: minimum mild oscillations per symbol to flag (default 5)
        verbose: print summary

    Returns:
        pl.DataFrame with bad rows removed
    """
    if df.is_empty() or price_col not in df.columns:
        return df

    rows_before = df.height
    st = spike_threshold
    inv_st = 1.0 / st

    # Compute neighbor prices (single pass)
    df_nb = df.sort([symbol_col, date_col]).with_columns([
        pl.col(price_col).shift(1).over(symbol_col).alias("_p1"),
        pl.col(price_col).shift(-1).over(symbol_col).alias("_n1"),
        pl.col(price_col).shift(2).over(symbol_col).alias("_p2"),
        pl.col(price_col).shift(-2).over(symbol_col).alias("_n2"),
    ])

    # ±1 window: spike vs prev, but prev ≈ next (revert)
    w1_spike = (
        pl.col("_p1").is_not_null() & pl.col("_n1").is_not_null()
        & (pl.col("_p1") > 0) & (pl.col("_n1") > 0)
        & ((pl.col(price_col) / pl.col("_p1") > st) | (pl.col(price_col) / pl.col("_p1") < inv_st))
        & (pl.col("_n1") / pl.col("_p1") >= 0.5)
        & (pl.col("_n1") / pl.col("_p1") <= 2.0)
    )

    # ±2 window: spike vs 2-day neighbors, which agree
    w2_spike = (
        pl.col("_p2").is_not_null() & pl.col("_n2").is_not_null()
        & (pl.col("_p2") > 0) & (pl.col("_n2") > 0)
        & ((pl.col(price_col) / pl.col("_p2") > st) | (pl.col(price_col) / pl.col("_p2") < inv_st))
        & ((pl.col(price_col) / pl.col("_n2") > st) | (pl.col(price_col) / pl.col("_n2") < inv_st))
        & (pl.col("_n2") / pl.col("_p2") >= 0.5)
        & (pl.col("_n2") / pl.col("_p2") <= 2.0)
    )

    # Tier 1: definite spikes
    tier1_mask = w1_spike | w2_spike
    tier1_rows = df_nb.filter(tier1_mask).select([symbol_col, date_col])

    # Tier 2: mild oscillations (only if symbol has enough)
    tier2_rows = pl.DataFrame()
    if mild_threshold < spike_threshold:
        mt = mild_threshold
        inv_mt = 1.0 / mt

        w1_mild = (
            pl.col("_p1").is_not_null() & pl.col("_n1").is_not_null()
            & (pl.col("_p1") > 0) & (pl.col("_n1") > 0)
            & ((pl.col(price_col) / pl.col("_p1") > mt) | (pl.col(price_col) / pl.col("_p1") < inv_mt))
            & (pl.col("_n1") / pl.col("_p1") >= 0.5)
            & (pl.col("_n1") / pl.col("_p1") <= 2.0)
        )
        w2_mild = (
            pl.col("_p2").is_not_null() & pl.col("_n2").is_not_null()
            & (pl.col("_p2") > 0) & (pl.col("_n2") > 0)
            & ((pl.col(price_col) / pl.col("_p2") > mt) | (pl.col(price_col) / pl.col("_p2") < inv_mt))
            & ((pl.col(price_col) / pl.col("_n2") > mt) | (pl.col(price_col) / pl.col("_n2") < inv_mt))
            & (pl.col("_n2") / pl.col("_p2") >= 0.5)
            & (pl.col("_n2") / pl.col("_p2") <= 2.0)
        )

        mild_mask = (w1_mild | w2_mild) & ~tier1_mask
        mild_rows = df_nb.filter(mild_mask).select([symbol_col, date_col])

        if mild_rows.height > 0:
            sym_counts = mild_rows.group_by(symbol_col, maintain_order=True).agg(pl.len().alias("cnt"))
            frequent_syms = sym_counts.filter(pl.col("cnt") >= min_mild_count).select(symbol_col)
            tier2_rows = mild_rows.join(frequent_syms, on=symbol_col, how="inner")

    # Combine bad rows and remove
    all_bad = pl.concat([tier1_rows, tier2_rows]) if tier2_rows.height > 0 else tier1_rows
    total_removed = all_bad.height

    if total_removed > 0:
        df = df_nb.join(all_bad, on=[symbol_col, date_col], how="anti")
        bad_symbols = all_bad[symbol_col].n_unique()
    else:
        df = df_nb
        bad_symbols = 0

    # Drop helper columns
    df = df.drop(["_p1", "_n1", "_p2", "_n2"], strict=False)

    if total_removed > 0:
        # Structured logging (audit P4.2): emit summary at INFO and the
        # actual affected-symbol list at DEBUG so users can investigate
        # data-quality issues without parsing stdout.
        _logger.info(
            "remove_price_oscillations: removed %d bad rows from %d symbols "
            "(%d -> %d rows)",
            total_removed, bad_symbols, rows_before, df.height,
        )
        try:
            affected = sorted(set(all_bad[symbol_col].to_list()))
            _logger.debug(
                "remove_price_oscillations: affected symbols (%d): %s",
                len(affected), affected,
            )
        except Exception:
            # Best-effort; never let logging break the pipeline.
            pass

        if verbose:
            print(f"  Price oscillation filter: removed {total_removed} bad rows "
                  f"from {bad_symbols} symbols ({rows_before} → {df.height} rows)")

    return df


OHLCV_NUMERIC_COLS = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]


def cast_ohlcv_dtypes(df: pl.DataFrame, numeric_cols: list[str] = None) -> pl.DataFrame:
    """Cast OHLCV columns to correct numeric types (Int64 for epochs, Float64 for prices)."""
    cols = numeric_cols or OHLCV_NUMERIC_COLS
    cast_exprs = []
    for col in cols:
        if col in df.columns:
            if "epoch" in col:
                cast_exprs.append(pl.col(col).cast(pl.Int64).alias(col))
            else:
                cast_exprs.append(pl.col(col).cast(pl.Float64).alias(col))
    if cast_exprs:
        df = df.with_columns(cast_exprs)
    return df


def _parse_fmp_symbols_polars(df: pl.DataFrame, exchange_suffix: dict) -> pl.DataFrame:
    """Derive exchange, bare symbol, and instrument from FMP symbol column.

    Vectorized Polars implementation replacing pandas .apply() + list comprehension.
    """
    # Build expression chain for suffix-based exchanges (NSE, BSE)
    # US exchanges have no suffix - symbols without dots default to "US"
    exchange_expr = pl.lit("US")
    bare_symbol_expr = pl.col("symbol")
    for exchange, suffix in exchange_suffix.items():
        if not suffix:
            continue  # skip US exchanges (no suffix to match on)
        exchange_expr = (
            pl.when(pl.col("symbol").str.ends_with(suffix))
            .then(pl.lit(exchange))
            .otherwise(exchange_expr)
        )
        bare_symbol_expr = (
            pl.when(pl.col("symbol").str.ends_with(suffix))
            .then(pl.col("symbol").str.slice(0, pl.col("symbol").str.len_chars() - len(suffix)))
            .otherwise(bare_symbol_expr)
        )

    df = df.with_columns([
        exchange_expr.alias("exchange"),
        bare_symbol_expr.alias("symbol"),
    ])
    df = df.with_columns(
        (pl.col("exchange") + pl.lit(":") + pl.col("symbol")).alias("instrument")
    )
    return df


class CRDataProvider:
    """Fetch OHLCV data from Ceta Research API via SQL."""

    def __init__(self, api_key=None, format="json", timeout=600, memory_mb=16384,
                 threads=6, disk_mb=40960,
                 spike_threshold=2.0, mild_threshold=1.3, min_mild_count=5):
        """
        Args:
            api_key: CR API key (falls back to CR_API_KEY env var).
            format: Response format - "json" (default, backward compat) or
                    "parquet" (much faster for bulk data: no JSON conversion,
                    smaller transfer, faster parsing).
            timeout: CR API query timeout in seconds.
            memory_mb: CR API memory allocation in MB. Default 16_384 (16 GiB)
                is the maximum memory tier. Lowering below 8 GiB can cause
                OOM on full NSE-universe queries spanning 10+ years.
            threads: CR API parallel threads.
            disk_mb: CR API disk allocation in MB.
            spike_threshold: Price oscillation tier 1 threshold (x multiplier).
            mild_threshold: Price oscillation tier 2 threshold (x multiplier).
            min_mild_count: Min tier 2 oscillations to flag a symbol.
        """
        self.client = CetaResearch(api_key=api_key)
        self._format = format
        self._timeout = timeout
        self._memory_mb = memory_mb
        self._threads = threads
        self._disk_mb = disk_mb
        self._spike_threshold = spike_threshold
        self._mild_threshold = mild_threshold
        self._min_mild_count = min_mild_count

    # FMP symbol suffix per exchange (empty string = no suffix, uses profile join)
    EXCHANGE_SUFFIX = {
        "NSE": ".NS",
        "BSE": ".BO",
        "NASDAQ": "",
        "NYSE": "",
        "AMEX": "",
        "LSE": ".L",
        "JPX": ".T",
        "HKSE": ".HK",
        "XETRA": ".DE",
        "KSC": ".KS",
        "ASX": ".AX",
        "TSX": ".TO",
        "SHH": ".SS",
        "SHZ": ".SZ",
        "TAI": ".TW",
        "TWO": ".TWO",
        "PAR": ".PA",
        "STO": ".ST",
        "SIX": ".SW",
        "JKT": ".JK",
        "SAO": ".SA",
        "SET": ".BK",
        "JNB": ".JO",
        "SES": ".SI",
    }

    # US exchanges need profile join (no suffix to distinguish from crypto/OTC)
    # "US" is a macro exchange that expands to NASDAQ+NYSE+AMEX
    US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "US"}

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Fetch OHLCV data from fmp.stock_eod.

        CRITICAL: Fetches from (start_epoch - prefetch_days * 86400) through end_epoch.
        Without the prefetch buffer, scanner rolling windows and order_gen rolling windows
        produce NaN for the first ~400 days.

        fmp.stock_eod schema: date (varchar), dateEpoch (int), open, high, low, close,
        adjClose, volume, symbol (e.g. 'RELIANCE.NS'). No 'exchange' column.

        Args:
            exchanges: list of exchange codes (e.g. ["NSE", "BSE"])
            symbols: optional list of bare symbols to filter (e.g. ["RELIANCE", "TCS"]).
                     If None, fetches all for the given exchanges.
            start_epoch: simulation start epoch
            end_epoch: simulation end epoch
            prefetch_days: number of days to prefetch before start_epoch

        Returns:
            pl.DataFrame with columns: date_epoch, open, high, low, close, average_price,
                                     volume, symbol, instrument, exchange
        """
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        # Build FMP symbol list: bare symbols + exchange suffix
        fmp_symbols = []
        symbol_exchange_map = {}  # fmp_symbol -> (exchange, bare_symbol)
        for exchange in exchanges:
            suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
            if symbols:
                for s in symbols:
                    fmp_sym = f"{s}{suffix}"
                    fmp_symbols.append(fmp_sym)
                    symbol_exchange_map[fmp_sym] = (exchange, s)
            else:
                # Without explicit symbols, filter by suffix pattern
                symbol_exchange_map[suffix] = (exchange, None)

        where_clauses = [
            f"CAST(dateEpoch AS BIGINT) >= {fetch_start}",
            f"CAST(dateEpoch AS BIGINT) <= {end_epoch}",
        ]

        # Determine if we need a profile join for US exchanges
        us_exchanges = [e for e in exchanges if e in self.US_EXCHANGES]
        non_us_exchanges = [e for e in exchanges if e not in self.US_EXCHANGES]

        if fmp_symbols:
            symbol_list = ", ".join(f"'{s}'" for s in fmp_symbols)
            where_clauses.append(f"symbol IN ({symbol_list})")
        elif non_us_exchanges:
            # Non-US: filter by suffix patterns
            suffix_clauses = []
            for exchange in non_us_exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                if suffix:
                    suffix_clauses.append(f"symbol LIKE '%{suffix}'")
            if suffix_clauses:
                where_clauses.append(f"({' OR '.join(suffix_clauses)})")

        where = " AND ".join(where_clauses)

        if us_exchanges and not fmp_symbols:
            # US exchanges: join with profile to filter by exchange (excludes crypto/OTC)
            # Expand "US" macro to actual exchange names for the profile join
            actual_us_exchanges = set()
            for e in us_exchanges:
                if e == "US":
                    actual_us_exchanges.update(["NASDAQ", "NYSE", "AMEX"])
                else:
                    actual_us_exchanges.add(e)
            us_exchange_list = ", ".join(f"'{e}'" for e in sorted(actual_us_exchanges))
            us_where = where
            if non_us_exchanges:
                # Mixed US + non-US: need UNION
                non_us_sql = f"""
                    SELECT
                        CAST(dateEpoch AS BIGINT) AS date_epoch,
                        open, high, low, close,
                        (high + low + close) / 3.0 AS average_price,
                        volume, symbol
                    FROM fmp.stock_eod
                    WHERE {where}
                """
                sql = f"""
                    SELECT
                        CAST(e.dateEpoch AS BIGINT) AS date_epoch,
                        e.open, e.high, e.low, e.close,
                        (e.high + e.low + e.close) / 3.0 AS average_price,
                        e.volume, e.symbol
                    FROM fmp.stock_eod e
                    JOIN fmp.profile p ON e.symbol = p.symbol
                    WHERE CAST(e.dateEpoch AS BIGINT) >= {fetch_start}
                      AND CAST(e.dateEpoch AS BIGINT) <= {end_epoch}
                      AND p.exchange IN ({us_exchange_list})
                    UNION ALL
                    {non_us_sql}
                    ORDER BY symbol, date_epoch
                """
            else:
                sql = f"""
                    SELECT
                        CAST(e.dateEpoch AS BIGINT) AS date_epoch,
                        e.open, e.high, e.low, e.close,
                        (e.high + e.low + e.close) / 3.0 AS average_price,
                        e.volume, e.symbol
                    FROM fmp.stock_eod e
                    JOIN fmp.profile p ON e.symbol = p.symbol
                    WHERE CAST(e.dateEpoch AS BIGINT) >= {fetch_start}
                      AND CAST(e.dateEpoch AS BIGINT) <= {end_epoch}
                      AND p.exchange IN ({us_exchange_list})
                    ORDER BY e.symbol, date_epoch
                """
        else:
            sql = f"""
                SELECT
                    CAST(dateEpoch AS BIGINT) AS date_epoch,
                    open, high, low, close,
                    (high + low + close) / 3.0 AS average_price,
                    volume, symbol
                FROM fmp.stock_eod
                WHERE {where}
                ORDER BY symbol, date_epoch
            """

        print(f"  Fetching data: {len(exchanges)} exchanges, "
              f"prefetch={prefetch_days}d, "
              f"range={_epoch_to_date_str(fetch_start)} to "
              f"{_epoch_to_date_str(end_epoch)}")

        results = self.client.query(
            sql,
            timeout=self._timeout,
            limit=10000000,
            verbose=True,
            memory_mb=self._memory_mb,
            threads=self._threads,
            disk_mb=self._disk_mb,
            format=self._format,
        )

        if not results:
            return pl.DataFrame()

        if self._format == "parquet":
            df = pl.read_parquet(io.BytesIO(results))
        else:
            df = pl.DataFrame(results)

        # Derive exchange and bare symbol from FMP symbol
        df = _parse_fmp_symbols_polars(df, self.EXCHANGE_SUFFIX)

        df = cast_ohlcv_dtypes(df)

        df = df.sort(["instrument", "date_epoch"])
        df = remove_price_oscillations(
            df, price_col="close",
            spike_threshold=self._spike_threshold,
            mild_threshold=self._mild_threshold,
            min_mild_count=self._min_mild_count,
            verbose=True,
        )

        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments")
        return df


class ParquetDataProvider:
    """Local parquet data provider for verification against ATO_Simulator."""

    def __init__(self, base_path):
        """
        Args:
            base_path: path to tick_data root, e.g. ~/ATO_DATA/tick_data
                       Expects parquet at: {base_path}/data_source=kite/granularity=day/exchange={exchange}/
        """
        self.base_path = os.path.expanduser(base_path)

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Read OHLCV from local parquet files matching ATO_Simulator's format.

        Returns pl.DataFrame with same schema as CRDataProvider.
        """
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        all_dfs = []
        for exchange in exchanges:
            parquet_dir = os.path.join(
                self.base_path, f"data_source=kite/granularity=day/exchange={exchange}"
            )
            if not os.path.isdir(parquet_dir):
                print(f"  Warning: no parquet dir at {parquet_dir}")
                continue

            parquet_files = [
                os.path.join(parquet_dir, f)
                for f in os.listdir(parquet_dir)
                if f.endswith(".parquet")
            ]
            if not parquet_files:
                print(f"  Warning: no parquet files in {parquet_dir}")
                continue

            for pf in parquet_files:
                df = pl.read_parquet(pf)

                # Filter by epoch range
                df = df.filter(
                    (pl.col("date_epoch") >= fetch_start) & (pl.col("date_epoch") <= end_epoch)
                )

                # Filter by symbols if specified
                if symbols:
                    df = df.filter(pl.col("symbol").is_in(symbols))

                if df.is_empty():
                    continue

                # Add instrument and exchange columns if not present
                if "instrument" not in df.columns:
                    df = df.with_columns(
                        (pl.lit(exchange) + pl.lit(":") + pl.col("symbol").cast(pl.Utf8)).alias("instrument")
                    )
                if "exchange" not in df.columns:
                    df = df.with_columns(pl.lit(exchange).alias("exchange"))

                all_dfs.append(df)

        if not all_dfs:
            print("  No data found in parquet files.")
            return pl.DataFrame()

        df = pl.concat(all_dfs, how="diagonal")

        df = cast_ohlcv_dtypes(df)

        df = df.sort(["instrument", "date_epoch"])
        df = remove_price_oscillations(df, price_col="close", verbose=True)

        print(f"  Loaded {df.height} rows, {df['instrument'].n_unique()} instruments from parquet")
        return df


class FMPParquetDataProvider:
    """Read OHLCV from local FMP EOD parquet files.

    FMP parquet schema: date, symbol, open, high, low, close, adjClose,
    volume, dateEpoch (uint32), data_source.

    Exchange is derived from symbol suffix (.NS=NSE, .BO=BSE, no dot=US).

    DEPRECATED: Use PolarsParquetDataProvider instead (15x faster).
    """

    EXCHANGE_SUFFIX = {
        "NSE": ".NS",
        "BSE": ".BO",
    }

    def __init__(self, parquet_dir):
        """
        Args:
            parquet_dir: path to FMP EOD parquet dir,
                         e.g. ~/data/data_source=fmp/tick_data/eod/
        """
        self.parquet_dir = os.path.expanduser(parquet_dir)

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Read OHLCV from local FMP parquet files.

        Returns pl.DataFrame with same schema as CRDataProvider.
        """
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        # Build FMP symbol set for filtering
        fmp_symbol_set = None
        if symbols:
            fmp_symbol_set = set()
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                for s in symbols:
                    fmp_symbol_set.add(f"{s}{suffix}")

        parquet_files = [
            os.path.join(self.parquet_dir, f)
            for f in os.listdir(self.parquet_dir)
            if f.endswith(".parquet")
        ]

        if not parquet_files:
            print(f"  Warning: no parquet files in {self.parquet_dir}")
            return pl.DataFrame()

        # Build exchange suffix map for post-read filtering
        suffix_map = {self.EXCHANGE_SUFFIX[ex]: ex for ex in exchanges if ex in self.EXCHANGE_SUFFIX}

        all_dfs = []
        read_cols = ["symbol", "open", "high", "low", "close", "volume", "dateEpoch"]

        for pf in parquet_files:
            df = pl.read_parquet(pf, columns=read_cols)

            if df.is_empty():
                continue

            # Filter by date range
            df = df.filter(
                (pl.col("dateEpoch") >= fetch_start) & (pl.col("dateEpoch") <= end_epoch)
            )
            if df.is_empty():
                continue

            # Filter by specific symbols
            if fmp_symbol_set:
                df = df.filter(pl.col("symbol").is_in(list(fmp_symbol_set)))
            elif suffix_map:
                suffix_filters = [pl.col("symbol").str.ends_with(s) for s in suffix_map]
                combined = suffix_filters[0]
                for sf in suffix_filters[1:]:
                    combined = combined | sf
                df = df.filter(combined)
            else:
                # US exchanges: symbols without dots
                df = df.filter(~pl.col("symbol").str.contains(r"\."))

            if df.is_empty():
                continue

            all_dfs.append(df)

        if not all_dfs:
            print("  No data found in FMP parquet files.")
            return pl.DataFrame()

        df = pl.concat(all_dfs)

        # Rename dateEpoch -> date_epoch, derive average_price
        df = df.rename({"dateEpoch": "date_epoch"}).with_columns(
            ((pl.col("high") + pl.col("low") + pl.col("close")) / 3.0).alias("average_price")
        )

        # Derive exchange and bare symbol
        df = _parse_fmp_symbols_polars(df, self.EXCHANGE_SUFFIX)

        df = cast_ohlcv_dtypes(df)

        df = df.sort(["instrument", "date_epoch"])
        df = remove_price_oscillations(df, price_col="close", verbose=True)

        print(f"  Loaded {df.height} rows, {df['instrument'].n_unique()} instruments from FMP parquet")
        return df


class DuckDBParquetDataProvider:
    """Read OHLCV from local FMP EOD parquet files using DuckDB.

    DuckDB pushes filters directly into the parquet scan, avoiding the need
    to load entire files into memory. Much faster than pandas for selective
    queries on large, unpartitioned parquet files.
    """

    EXCHANGE_SUFFIX = {
        "NSE": ".NS",
        "BSE": ".BO",
    }

    def __init__(self, parquet_dir):
        self.parquet_dir = os.path.expanduser(parquet_dir)

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Read OHLCV from local FMP parquet files via DuckDB SQL.

        Returns pl.DataFrame with same schema as CRDataProvider.
        """
        import duckdb

        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)
        glob_path = os.path.join(self.parquet_dir, "*.parquet")

        where_clauses = [
            f"CAST(dateEpoch AS BIGINT) >= {fetch_start}",
            f"CAST(dateEpoch AS BIGINT) <= {end_epoch}",
        ]

        if symbols:
            fmp_symbols = []
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                for s in symbols:
                    fmp_symbols.append(f"{s}{suffix}")
            symbol_list = ", ".join(f"'{s}'" for s in fmp_symbols)
            where_clauses.append(f"symbol IN ({symbol_list})")
        else:
            suffix_clauses = []
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange)
                if suffix:
                    suffix_clauses.append(f"symbol LIKE '%{suffix}'")
            if suffix_clauses:
                where_clauses.append(f"({' OR '.join(suffix_clauses)})")
            else:
                where_clauses.append("symbol NOT LIKE '%.%'")

        where = " AND ".join(where_clauses)

        sql = f"""
            SELECT
                CAST(dateEpoch AS BIGINT) AS date_epoch,
                symbol, open, high, low, close,
                (high + low + close) / 3.0 AS average_price,
                volume
            FROM read_parquet('{glob_path}', union_by_name=true)
            WHERE {where}
            ORDER BY symbol, date_epoch
        """

        con = duckdb.connect()
        arrow_table = con.execute(sql).fetch_arrow_table()
        con.close()

        df = pl.from_arrow(arrow_table)

        if df.is_empty():
            print("  No data found in FMP parquet via DuckDB.")
            return pl.DataFrame()

        # Derive exchange and bare symbol
        df = _parse_fmp_symbols_polars(df, self.EXCHANGE_SUFFIX)

        df = cast_ohlcv_dtypes(df)

        df = df.sort(["instrument", "date_epoch"])
        df = remove_price_oscillations(df, price_col="close", verbose=True)

        print(f"  Loaded {df.height} rows, {df['instrument'].n_unique()} instruments from DuckDB parquet")
        return df


class PolarsParquetDataProvider:
    """Read OHLCV from local FMP EOD parquet files using Polars lazy scan.

    Uses polars.scan_parquet() with lazy evaluation, predicate pushdown,
    and projection pushdown. Only materializes the final filtered result.
    """

    EXCHANGE_SUFFIX = {
        "NSE": ".NS",
        "BSE": ".BO",
    }

    def __init__(self, parquet_dir):
        self.parquet_dir = os.path.expanduser(parquet_dir)

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Read OHLCV from local FMP parquet files via Polars lazy scan.

        Returns pl.DataFrame natively (no pandas conversion).
        """
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)
        glob_path = os.path.join(self.parquet_dir, "*.parquet")

        # Lazy scan with projection pushdown (only needed columns)
        lf = pl.scan_parquet(
            glob_path,
            hive_partitioning=False,
            cast_options=pl.ScanCastOptions(categorical_to_string="allow"),
        ).select(["symbol", "open", "high", "low", "close", "volume", "dateEpoch"])

        # Predicate pushdown: date range filter
        lf = lf.filter(
            (pl.col("dateEpoch").cast(pl.Int64) >= fetch_start)
            & (pl.col("dateEpoch").cast(pl.Int64) <= end_epoch)
        )

        # Symbol filter
        if symbols:
            fmp_symbols = []
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                for s in symbols:
                    fmp_symbols.append(f"{s}{suffix}")
            lf = lf.filter(pl.col("symbol").is_in(fmp_symbols))
        else:
            suffix_clauses = []
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange)
                if suffix:
                    suffix_clauses.append(pl.col("symbol").str.ends_with(suffix))
            if suffix_clauses:
                combined = suffix_clauses[0]
                for clause in suffix_clauses[1:]:
                    combined = combined | clause
                lf = lf.filter(combined)
            else:
                # US exchanges: symbols without dots
                lf = lf.filter(~pl.col("symbol").str.contains(r"\."))

        # Compute derived columns
        lf = lf.with_columns([
            (pl.col("dateEpoch").cast(pl.Int64)).alias("date_epoch"),
            ((pl.col("high") + pl.col("low") + pl.col("close")) / 3.0).alias("average_price"),
        ])

        # Derive exchange and bare symbol
        exchange_expr = pl.lit("US")
        bare_symbol_expr = pl.col("symbol")
        for exchange, suffix in self.EXCHANGE_SUFFIX.items():
            exchange_expr = pl.when(pl.col("symbol").str.ends_with(suffix)).then(pl.lit(exchange)).otherwise(exchange_expr)
            bare_symbol_expr = pl.when(pl.col("symbol").str.ends_with(suffix)).then(
                pl.col("symbol").str.slice(0, pl.col("symbol").str.len_chars() - len(suffix))
            ).otherwise(bare_symbol_expr)

        lf = lf.with_columns([
            exchange_expr.alias("exchange"),
            bare_symbol_expr.alias("bare_symbol"),
        ])

        lf = lf.with_columns(
            (pl.col("exchange") + pl.lit(":") + pl.col("bare_symbol")).alias("instrument")
        )

        # Select final columns, sort, collect
        lf = lf.select([
            "date_epoch", "open", "high", "low", "close", "average_price",
            "volume", pl.col("bare_symbol").alias("symbol"), "instrument", "exchange",
        ]).sort(["instrument", "date_epoch"])

        # Collect (materializes the lazy frame)
        df = lf.collect()
        df = remove_price_oscillations(df, price_col="close", verbose=True)

        print(f"  Loaded {df.height} rows, {df['instrument'].n_unique()} instruments from Polars parquet")
        return df


class BhavcopyDataProvider:
    """Fetch NSE OHLCV data from nse.nse_bhavcopy_historical via CR API.

    Bhavcopy includes delisted/halted stocks (5,072 symbols vs 2,447 in charting),
    making it the gold standard for survivorship-bias-free NSE backtesting.

    WARNING: Bhavcopy prices are UNADJUSTED for stock splits and bonuses.
    Consumers using this for historical backtesting must apply corporate actions
    from a separate source. The oscillation filter is NOT applied here because
    legitimate price jumps from splits are expected in unadjusted data.
    """

    def __init__(self, api_key=None, turnover_threshold=0, price_threshold=0):
        """
        Args:
            api_key: CR API key (falls back to CR_API_KEY env var).
            turnover_threshold: min avg daily turnover in INR (e.g. 70_000_000).
                If > 0, runs a pre-filter query to select only qualifying symbols.
                Uses full-period average (AVG(close * volume)), matching the
                standalone fetch_universe() methodology.
            price_threshold: min avg close price (e.g. 50).
        """
        self.client = CetaResearch(api_key=api_key)
        self.turnover_threshold = turnover_threshold
        self.price_threshold = price_threshold

    def _fetch_qualifying_symbols(self, start_epoch, end_epoch):
        """Pre-filter: find symbols meeting turnover/price thresholds over full period.

        Uses AVG(TURNOVER) — bhavcopy's pre-computed daily turnover column (actual
        traded value in INR). This matches standalone fetch_universe(source="bhavcopy")
        which also uses AVG(TURNOVER), NOT AVG(CLOSE * VOLUME).

        Includes warmup window (1500 days before start_epoch) in the averaging period
        to match standalone methodology.

        WARNING: `AVG(CLOSE) > price_threshold` runs on UNADJUSTED prices,
        so splits skew the average (a pre-split ₹5000 stock passes a ₹50
        threshold even after a 100:1 split drops it to ₹50). For
        split-aware selection use `NseChartingDataProvider`.
        """
        having_clauses = []
        if self.turnover_threshold > 0:
            having_clauses.append(f"AVG(TURNOVER) > {self.turnover_threshold}")
        if self.price_threshold > 0:
            having_clauses.append(f"AVG(CLOSE) > {self.price_threshold}")

        if not having_clauses:
            return None  # no filtering needed

        having = " AND ".join(having_clauses)

        # Include warmup window for averaging (matches standalone's warmup_days=1500)
        warmup_start = start_epoch - 1500 * SECONDS_IN_ONE_DAY

        sql = f"""
        SELECT SYMBOL
        FROM nse.nse_bhavcopy_historical
        WHERE SERIES = 'EQ'
          AND (date_epoch - date_epoch % 86400) >= {warmup_start}
          AND (date_epoch - date_epoch % 86400) <= {end_epoch}
        GROUP BY SYMBOL
        HAVING {having}
        ORDER BY SYMBOL
        """

        print(f"  Pre-filtering bhavcopy: AVG(TURNOVER)>{self.turnover_threshold/1e6:.0f}M, "
              f"AVG(CLOSE)>{self.price_threshold}")

        results = self.client.query(
            sql, timeout=120, limit=100000, verbose=True,
            memory_mb=4096, threads=4, format="parquet",
        )

        if not results:
            return []

        df = pl.read_parquet(io.BytesIO(results))
        symbols = df["SYMBOL"].to_list()
        print(f"  Found {len(symbols)} qualifying symbols")
        return symbols

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Fetch OHLCV from nse.nse_bhavcopy_historical.

        If turnover_threshold or price_threshold are set, runs a pre-filter query
        to select only qualifying symbols (matching standalone fetch_universe behavior).

        Returns pl.DataFrame matching CRDataProvider output format:
            date_epoch, open, high, low, close, average_price, volume, symbol, instrument, exchange
        """
        # Pre-filter by quality if thresholds are set and no explicit symbols given
        if not symbols and (self.turnover_threshold > 0 or self.price_threshold > 0):
            symbols = self._fetch_qualifying_symbols(start_epoch, end_epoch)
            if symbols is not None and len(symbols) == 0:
                print("  No symbols pass quality filter.")
                return pl.DataFrame()

        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        where_parts = [
            "SERIES = 'EQ'",
            f"(date_epoch - date_epoch % 86400) >= {fetch_start}",
            f"(date_epoch - date_epoch % 86400) <= {end_epoch}",
        ]
        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            where_parts.append(f"SYMBOL IN ({sym_list})")

        where = " AND ".join(where_parts)

        sql = f"""
        SELECT
            (date_epoch - date_epoch % 86400) AS date_epoch,
            OPEN AS open, HIGH AS high, LOW AS low, CLOSE AS close,
            (HIGH + LOW + CLOSE) / 3.0 AS average_price,
            VOLUME AS volume,
            SYMBOL AS symbol
        FROM nse.nse_bhavcopy_historical
        WHERE {where}
        ORDER BY symbol, date_epoch
        """

        print(f"  Fetching bhavcopy data: prefetch={prefetch_days}d, "
              f"range={_epoch_to_date_str(fetch_start)} to "
              f"{_epoch_to_date_str(end_epoch)}")

        results = self.client.query(
            sql, timeout=600, limit=10000000, verbose=True,
            memory_mb=16384, threads=6, format="parquet",
        )

        if not results:
            return pl.DataFrame()

        df = pl.read_parquet(io.BytesIO(results))

        # Cast numeric types
        df = cast_ohlcv_dtypes(df)

        # Add exchange and instrument columns (all NSE)
        df = df.with_columns([
            pl.lit("NSE").alias("exchange"),
            (pl.lit("NSE:") + pl.col("symbol")).alias("instrument"),
        ])

        df = df.sort(["instrument", "date_epoch"])

        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments (bhavcopy)")
        return df


class NseChartingDataProvider:
    """Fetch NSE OHLCV from nse.nse_charting_day via CR API.

    This is the same data source the standalone champion strategies use.
    Split-adjusted prices, ~2,447 symbols. Best quality for NSE backtesting.
    """

    def __init__(self, api_key=None):
        self.client = CetaResearch(api_key=api_key)

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        """Fetch OHLCV from nse.nse_charting_day.

        Two-pass: first find qualifying symbols, then fetch OHLCV.
        Returns pl.DataFrame matching CRDataProvider output format.
        """
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            sym_filter = f"AND symbol IN ({sym_list})"
        else:
            sym_filter = ""

        sql = f"""
        SELECT symbol, date_epoch,
            open, high, low, close,
            (high + low + close) / 3.0 AS average_price,
            volume
        FROM nse.nse_charting_day
        WHERE date_epoch >= {fetch_start}
          AND date_epoch <= {end_epoch}
          {sym_filter}
        ORDER BY symbol, date_epoch
        """

        print(f"  Fetching nse_charting_day: prefetch={prefetch_days}d, "
              f"range={_epoch_to_date_str(fetch_start)} to "
              f"{_epoch_to_date_str(end_epoch)}")

        results = self.client.query(
            sql, timeout=600, limit=10000000, verbose=True,
            memory_mb=16384, threads=6, format="parquet",
        )

        if not results:
            return pl.DataFrame()

        df = pl.read_parquet(io.BytesIO(results))

        # Cast numeric types
        df = cast_ohlcv_dtypes(df)

        # Add exchange and instrument columns
        df = df.with_columns([
            pl.lit("NSE").alias("exchange"),
            (pl.lit("NSE:") + pl.col("symbol")).alias("instrument"),
        ])

        # Apply oscillation filter (same as CRDataProvider)
        df = remove_price_oscillations(df)

        df = df.sort(["instrument", "date_epoch"])

        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments (nse_charting_day)")
        return df

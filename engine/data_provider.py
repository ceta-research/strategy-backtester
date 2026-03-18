"""Data providers: fetch OHLCV data from CR API or local parquet.

CRDataProvider fetches from Ceta Research API.
ParquetDataProvider reads local parquet files (ATO_Simulator format).
FMPParquetDataProvider reads local FMP EOD parquet files.
DuckDBParquetDataProvider reads local FMP parquet via DuckDB SQL.
PolarsParquetDataProvider reads local FMP parquet via Polars lazy scan.
"""

import io
import os
import sys
from datetime import datetime, timezone

import polars as pl

# Add parent dir to path so lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cr_client import CetaResearch

from engine.constants import SECONDS_IN_ONE_DAY


def _epoch_to_date_str(epoch):
    """Convert epoch to YYYY-MM-DD string for display."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


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

    def __init__(self, api_key=None, format="json"):
        """
        Args:
            api_key: CR API key (falls back to CR_API_KEY env var).
            format: Response format - "json" (default, backward compat) or
                    "parquet" (much faster for bulk data: no JSON conversion,
                    smaller transfer, faster parsing).
        """
        self.client = CetaResearch(api_key=api_key)
        self._format = format

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
            timeout=600,
            limit=10000000,
            verbose=True,
            memory_mb=16384,
            threads=6,
            disk_mb=40960,
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

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        cast_exprs = []
        for col in numeric_cols:
            if col in df.columns:
                if col == "date_epoch":
                    cast_exprs.append(pl.col(col).cast(pl.Int64).alias(col))
                else:
                    cast_exprs.append(pl.col(col).cast(pl.Float64).alias(col))
        if cast_exprs:
            df = df.with_columns(cast_exprs)

        df = df.sort(["instrument", "date_epoch"])

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

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        cast_exprs = []
        for col in numeric_cols:
            if col in df.columns:
                if col == "date_epoch":
                    cast_exprs.append(pl.col(col).cast(pl.Int64).alias(col))
                else:
                    cast_exprs.append(pl.col(col).cast(pl.Float64).alias(col))
        if cast_exprs:
            df = df.with_columns(cast_exprs)

        df = df.sort(["instrument", "date_epoch"])

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

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        cast_exprs = []
        for col in numeric_cols:
            if col in df.columns:
                if col == "date_epoch":
                    cast_exprs.append(pl.col(col).cast(pl.Int64).alias(col))
                else:
                    cast_exprs.append(pl.col(col).cast(pl.Float64).alias(col))
        if cast_exprs:
            df = df.with_columns(cast_exprs)

        df = df.sort(["instrument", "date_epoch"])

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

        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        cast_exprs = []
        for col in numeric_cols:
            if col in df.columns:
                if col == "date_epoch":
                    cast_exprs.append(pl.col(col).cast(pl.Int64).alias(col))
                else:
                    cast_exprs.append(pl.col(col).cast(pl.Float64).alias(col))
        if cast_exprs:
            df = df.with_columns(cast_exprs)

        df = df.sort(["instrument", "date_epoch"])

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

        print(f"  Loaded {df.height} rows, {df['instrument'].n_unique()} instruments from Polars parquet")
        return df

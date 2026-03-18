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

import pandas as pd

# Add parent dir to path so lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cr_client import CetaResearch

from engine.constants import SECONDS_IN_ONE_DAY


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

    # FMP symbol suffix per exchange
    EXCHANGE_SUFFIX = {
        "NSE": ".NS",
        "BSE": ".BO",
    }

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
            DataFrame with columns: date_epoch, open, high, low, close, average_price,
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

        if fmp_symbols:
            symbol_list = ", ".join(f"'{s}'" for s in fmp_symbols)
            where_clauses.append(f"symbol IN ({symbol_list})")
        else:
            # Filter by suffix patterns for each exchange
            suffix_clauses = []
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                if suffix:
                    suffix_clauses.append(f"symbol LIKE '%{suffix}'")
            if suffix_clauses:
                where_clauses.append(f"({' OR '.join(suffix_clauses)})")

        where = " AND ".join(where_clauses)

        sql = f"""
            SELECT
                CAST(dateEpoch AS BIGINT) AS date_epoch,
                open, high, low, close,
                (high + low + close) / 3.0 AS average_price,
                volume,
                symbol
            FROM fmp.stock_eod
            WHERE {where}
            ORDER BY symbol, date_epoch
        """

        print(f"  Fetching data: {len(exchanges)} exchanges, "
              f"prefetch={prefetch_days}d, "
              f"range={pd.Timestamp(fetch_start, unit='s').date()} to "
              f"{pd.Timestamp(end_epoch, unit='s').date()}")

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
            return pd.DataFrame()

        if self._format == "parquet":
            df = pd.read_parquet(io.BytesIO(results), engine="pyarrow")
        else:
            df = pd.DataFrame(results)

        # Derive exchange and bare symbol from FMP symbol (e.g. 'RELIANCE.NS' -> NSE, RELIANCE)
        suffix_to_exchange = {v: k for k, v in self.EXCHANGE_SUFFIX.items()}
        def parse_fmp_symbol(fmp_sym):
            for suffix, exchange in suffix_to_exchange.items():
                if fmp_sym.endswith(suffix):
                    bare = fmp_sym[: -len(suffix)]
                    return exchange, bare
            return "UNKNOWN", fmp_sym

        parsed = df["symbol"].apply(parse_fmp_symbol)
        df["exchange"] = [p[0] for p in parsed]
        df["symbol"] = [p[1] for p in parsed]
        df["instrument"] = df["exchange"] + ":" + df["symbol"]

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date_epoch"] = df["date_epoch"].astype(int)
        df.sort_values(["instrument", "date_epoch"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        print(f"  Fetched {len(df)} rows, {df['instrument'].nunique()} instruments")
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

        Returns DataFrame with same schema as CRDataProvider.
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
                df = pd.read_parquet(pf, engine="pyarrow")

                # Filter by epoch range
                df = df[(df["date_epoch"] >= fetch_start) & (df["date_epoch"] <= end_epoch)]

                # Filter by symbols if specified
                if symbols:
                    df = df[df["symbol"].isin(symbols)]

                if df.empty:
                    continue

                # Add instrument and exchange columns if not present
                if "instrument" not in df.columns:
                    df["instrument"] = exchange + ":" + df["symbol"].astype(str)
                if "exchange" not in df.columns:
                    df["exchange"] = exchange

                all_dfs.append(df)

        if not all_dfs:
            print("  No data found in parquet files.")
            return pd.DataFrame()

        df = pd.concat(all_dfs, ignore_index=True)

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date_epoch"] = df["date_epoch"].astype(int)
        df.sort_values(["instrument", "date_epoch"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        print(f"  Loaded {len(df)} rows, {df['instrument'].nunique()} instruments from parquet")
        return df


class FMPParquetDataProvider:
    """Read OHLCV from local FMP EOD parquet files.

    FMP parquet schema: date, symbol, open, high, low, close, adjClose,
    volume, dateEpoch (uint32), data_source.

    Exchange is derived from symbol suffix (.NS=NSE, .BO=BSE, no dot=US).
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

        Uses pyarrow filters for predicate pushdown (date range + symbol).
        Returns DataFrame with same schema as CRDataProvider.
        """
        import pyarrow.parquet as pq
        import pyarrow as pa

        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        # Build FMP symbol set for filtering
        fmp_symbol_set = None
        if symbols:
            fmp_symbol_set = set()
            for exchange in exchanges:
                suffix = self.EXCHANGE_SUFFIX.get(exchange, "")
                for s in symbols:
                    fmp_symbol_set.add(f"{s}{suffix}")

        # Read only needed columns
        read_cols = ["symbol", "open", "high", "low", "close", "volume", "dateEpoch"]

        parquet_files = [
            os.path.join(self.parquet_dir, f)
            for f in os.listdir(self.parquet_dir)
            if f.endswith(".parquet")
        ]

        if not parquet_files:
            print(f"  Warning: no parquet files in {self.parquet_dir}")
            return pd.DataFrame()

        # Build pyarrow filters for pushdown
        filters = [
            ("dateEpoch", ">=", fetch_start),
            ("dateEpoch", "<=", end_epoch),
        ]
        if fmp_symbol_set:
            filters.append(("symbol", "in", fmp_symbol_set))

        # Build exchange suffix map for post-read filtering
        suffix_map = {self.EXCHANGE_SUFFIX[ex]: ex for ex in exchanges if ex in self.EXCHANGE_SUFFIX}

        all_dfs = []
        for pf in parquet_files:
            pf_reader = pq.ParquetFile(pf)

            # Read only needed columns, use row group filtering
            table = pf_reader.read(columns=read_cols)
            if table.num_rows == 0:
                continue

            df = table.to_pandas()
            del table

            # Filter by date range
            df = df[(df["dateEpoch"] >= fetch_start) & (df["dateEpoch"] <= end_epoch)]
            if df.empty:
                continue

            # Filter by specific symbols (most selective, do first when available)
            if fmp_symbol_set:
                df = df[df["symbol"].isin(fmp_symbol_set)]
            elif suffix_map:
                mask = pd.Series(False, index=df.index)
                for suffix in suffix_map:
                    mask |= df["symbol"].str.endswith(suffix)
                df = df[mask]
            else:
                # US exchanges: symbols without dots
                df = df[~df["symbol"].str.contains(r"\.")]

            if df.empty:
                continue

            all_dfs.append(df)

        if not all_dfs:
            print("  No data found in FMP parquet files.")
            return pd.DataFrame()

        df = pd.concat(all_dfs, ignore_index=True)

        # Rename dateEpoch -> date_epoch
        df.rename(columns={"dateEpoch": "date_epoch"}, inplace=True)

        # Derive average_price
        df["average_price"] = (df["high"] + df["low"] + df["close"]) / 3.0

        # Derive exchange and bare symbol
        suffix_to_exchange = {v: k for k, v in self.EXCHANGE_SUFFIX.items()}

        def parse_fmp_symbol(fmp_sym):
            for suffix, exchange in suffix_to_exchange.items():
                if fmp_sym.endswith(suffix):
                    return exchange, fmp_sym[: -len(suffix)]
            return "US", fmp_sym

        parsed = df["symbol"].apply(parse_fmp_symbol)
        df["exchange"] = [p[0] for p in parsed]
        df["symbol"] = [p[1] for p in parsed]
        df["instrument"] = df["exchange"] + ":" + df["symbol"]

        # Ensure correct dtypes
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date_epoch"] = df["date_epoch"].astype(int)
        df.sort_values(["instrument", "date_epoch"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        print(f"  Loaded {len(df)} rows, {df['instrument'].nunique()} instruments from FMP parquet")
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

        Returns DataFrame with same schema as CRDataProvider.
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
        df = con.execute(sql).fetchdf()
        con.close()

        if df.empty:
            print("  No data found in FMP parquet via DuckDB.")
            return pd.DataFrame()

        # Derive exchange and bare symbol
        suffix_to_exchange = {v: k for k, v in self.EXCHANGE_SUFFIX.items()}

        def parse_fmp_symbol(fmp_sym):
            for suffix, exchange in suffix_to_exchange.items():
                if fmp_sym.endswith(suffix):
                    return exchange, fmp_sym[: -len(suffix)]
            return "US", fmp_sym

        parsed = df["symbol"].apply(parse_fmp_symbol)
        df["exchange"] = [p[0] for p in parsed]
        df["symbol"] = [p[1] for p in parsed]
        df["instrument"] = df["exchange"] + ":" + df["symbol"]

        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["date_epoch"] = df["date_epoch"].astype(int)
        df.sort_values(["instrument", "date_epoch"], inplace=True)
        df.reset_index(drop=True, inplace=True)

        print(f"  Loaded {len(df)} rows, {df['instrument'].nunique()} instruments from DuckDB parquet")
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

        Returns DataFrame with same schema as CRDataProvider (pandas DataFrame).
        """
        import polars as pl

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
        # Determine exchange from suffix
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
        pl_df = lf.collect()

        # Convert to pandas for compatible output
        df = pl_df.to_pandas()

        df["date_epoch"] = df["date_epoch"].astype(int)
        df.reset_index(drop=True, inplace=True)

        print(f"  Loaded {len(df)} rows, {df['instrument'].nunique()} instruments from Polars parquet")
        return df

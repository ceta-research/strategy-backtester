"""Local Parquet data providers, for research scripts and verification only.

No config selects them: `static.data_provider` picks only the engine's providers in
engine/data_provider.py. Pass one as `run_pipeline(config_path, data_provider=...)`.

ParquetDataProvider reads ATO_Simulator-format kite parquet files, which carry split-adjusted
prices in `close`. FMPParquetDataProvider, DuckDBParquetDataProvider and PolarsParquetDataProvider
read local FMP EOD parquet; FMP's `close` is split-adjusted but not dividend-adjusted. All four read
static local files with no invalidation: re-running the data pipeline overwrites them.
"""

import os

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.data_provider import _parse_fmp_symbols_polars, cast_ohlcv_dtypes, remove_price_oscillations


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
            # Symbols come from scanner config (internal); f-string is safe here.
            # Full parameterization is impractical with DuckDB read_parquet SQL.
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

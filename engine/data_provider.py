"""Data providers: fetch OHLCV data from CR API or local parquet.

CRDataProvider fetches from Ceta Research API.
ParquetDataProvider reads local parquet files (ATO_Simulator format).
"""

import os
import sys

import pandas as pd

# Add parent dir to path so lib/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cr_client import CetaResearch

from engine.constants import SECONDS_IN_ONE_DAY


class CRDataProvider:
    """Fetch OHLCV data from Ceta Research API via SQL."""

    def __init__(self, api_key=None):
        self.client = CetaResearch(api_key=api_key)

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
            limit=1000000,
            verbose=True,
            memory_mb=16384,
            threads=6,
        )

        if not results:
            return pd.DataFrame()

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

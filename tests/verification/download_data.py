#!/usr/bin/env python3
"""Download NSE data from CR API and save as parquet in ATO_Simulator's format.

Fetches fmp.stock_eod for 30 NSE symbols over 2019-01-01 to 2022-01-01.
Saves to ~/ATO_DATA/tick_data/data_source=kite/granularity=day/exchange=NSE/data.parquet
"""

import os
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lib.cr_client import CetaResearch

# The 30 symbols from ATO_Simulator's get_tradeable_symbols("kite", "NSE")
NSE_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR",
    "ICICIBANK", "KOTAKBANK", "LT", "SBIN", "BHARTIARTL",
    "AXISBANK", "ITC", "ASIANPAINT", "MARUTI", "TITAN",
    "SUNPHARMA", "BAJFINANCE", "NESTLEIND", "WIPRO", "ULTRACEMCO",
    "HCLTECH", "TECHM", "POWERGRID", "NTPC", "M&M",
    "TATAMOTORS", "ONGC", "JSWSTEEL", "GRASIM", "BPCL",
]

# Fixed 3-year window: gives ~400-day prefetch + ~2 years simulation
START_DATE = "2019-01-01"
END_DATE = "2022-01-01"

OUTPUT_DIR = os.path.expanduser("~/ATO_DATA/tick_data/data_source=kite/granularity=day/exchange=NSE")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data.parquet")


def main():
    print(f"Downloading NSE data for {len(NSE_SYMBOLS)} symbols")
    print(f"Date range: {START_DATE} to {END_DATE}")

    # FMP stock_eod uses symbol suffixes: RELIANCE.NS for NSE
    fmp_symbols = [f"{s}.NS" for s in NSE_SYMBOLS]
    symbol_list = ", ".join(f"'{s}'" for s in fmp_symbols)

    # stock_eod schema: date (varchar), dateEpoch, open, high, low, close, adjClose, volume, symbol
    # No 'exchange' column - exchange is encoded in symbol suffix (.NS = NSE)
    # dateEpoch is already a unix timestamp column
    sql = f"""
        SELECT
            CAST(dateEpoch AS BIGINT) AS date_epoch,
            open, high, low, close,
            (high + low + close) / 3.0 AS average_price,
            volume,
            symbol
        FROM fmp.stock_eod
        WHERE symbol IN ({symbol_list})
          AND date >= '{START_DATE}'
          AND date <= '{END_DATE}'
        ORDER BY symbol, date_epoch
    """

    cr = CetaResearch()
    print("Executing query...")
    results = cr.query(sql, timeout=600, limit=100000, verbose=True, memory_mb=16384, threads=6)

    if not results:
        print("ERROR: No data returned from API")
        sys.exit(1)

    df = pd.DataFrame(results)
    print(f"Received {len(df)} rows")

    # Strip .NS suffix from symbol to match ATO_Simulator's format (bare symbols)
    df["symbol"] = df["symbol"].str.replace(".NS", "", regex=False)

    # Cast to ATO_Simulator's expected dtypes
    df["date_epoch"] = df["date_epoch"].astype(np.uint32)
    for col in ["open", "high", "low", "close", "average_price"]:
        df[col] = df[col].astype(np.float64)
    df["volume"] = df["volume"].astype(np.float32)
    df["symbol"] = df["symbol"].astype(str)

    df.sort_values(["symbol", "date_epoch"], inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Write parquet with ATO_Simulator's expected schema
    schema = pa.schema([
        ("date_epoch", pa.uint32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("average_price", pa.float64()),
        ("volume", pa.float32()),
        ("symbol", pa.string()),
    ])

    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pq.write_table(table, OUTPUT_FILE, compression="ZSTD")

    # Summary
    symbols_found = df["symbol"].nunique()
    missing = set(NSE_SYMBOLS) - set(df["symbol"].unique())
    date_min = pd.Timestamp(df["date_epoch"].min(), unit="s").date()
    date_max = pd.Timestamp(df["date_epoch"].max(), unit="s").date()

    print(f"\nSaved to: {OUTPUT_FILE}")
    print(f"Rows: {len(df)}")
    print(f"Symbols: {symbols_found}/{len(NSE_SYMBOLS)}")
    if missing:
        print(f"Missing symbols: {sorted(missing)}")
    print(f"Date range: {date_min} to {date_max}")
    print(f"File size: {os.path.getsize(OUTPUT_FILE) / 1024:.0f} KB")


if __name__ == "__main__":
    main()

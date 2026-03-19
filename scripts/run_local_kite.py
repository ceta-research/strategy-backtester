#!/usr/bin/env python3
"""Run EOD Technical using the same local Kite parquet file that ATO_Simulator uses."""

import os
import sys

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.pipeline import run_pipeline
from engine.constants import SECONDS_IN_ONE_DAY

KITE_PARQUET = "/Users/swas/Desktop/Swas/Kite/ATO_SUITE/data/tick_data/data_source=kite/granularity=day/exchange=NSE/1747083733.767452.parquet"


class LocalKiteDataProvider:
    """Read from the exact same parquet file ATO_Simulator uses."""

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        df = pl.read_parquet(KITE_PARQUET)
        print(f"  Local Kite parquet: {df.height:,} rows, {df['symbol'].n_unique()} symbols")

        # Filter by date range
        df = df.filter(
            (pl.col("date_epoch").cast(pl.Int64) >= fetch_start)
            & (pl.col("date_epoch").cast(pl.Int64) <= end_epoch)
        )

        # Filter by symbols if specified
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))

        # Drop extra columns, cast symbol to string, add exchange/instrument
        keep_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume", "symbol"]
        df = df.select([c for c in keep_cols if c in df.columns])
        df = df.with_columns([
            pl.col("symbol").cast(pl.Utf8),
            pl.lit("NSE").alias("exchange"),
        ])
        df = df.with_columns(
            (pl.lit("NSE:") + pl.col("symbol")).alias("instrument")
        )

        # Ensure dtypes
        df = df.with_columns([
            pl.col("date_epoch").cast(pl.Int64),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("average_price").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
        ]).sort(["instrument", "date_epoch"])

        print(f"  Filtered: {df.height:,} rows, {df['instrument'].n_unique()} instruments")
        return df


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "strategies/eod_technical/config_kite_ato_match.yaml"
    results = run_pipeline(config_path, data_provider=LocalKiteDataProvider())

    print(f"\n{'='*80}")
    print("RESULTS (Local Kite parquet - same file as ATO_Simulator)")
    print(f"{'='*80}")
    for r in results:
        cagr = (r.get('cagr') or 0) * 100
        dd = (r.get('max_drawdown') or 0) * 100
        calmar = r.get('calmar_ratio') or 0
        sv = r.get('start_value') or 0
        ev = r.get('end_value') or 0
        growth = ev / sv if sv > 0 else 0
        print(f"  {r['config_id']:<12} CAGR={cagr:>6.1f}%  MaxDD={dd:>6.1f}%  Calmar={calmar:>5.2f}  Growth={growth:>6.1f}x")

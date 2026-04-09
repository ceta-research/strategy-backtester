#!/usr/bin/env python3
"""Run EOD Technical using nse.nse_charting_day - NSE's own data."""

import io
import os
import sys

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.pipeline import run_pipeline
from engine.constants import SECONDS_IN_ONE_DAY


class NseChartingDataProvider:
    def __init__(self):
        self.client = CetaResearch()

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)
        where = f"date_epoch >= {fetch_start} AND date_epoch <= {end_epoch}"
        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            where += f" AND symbol IN ({sym_list})"

        sql = f"""SELECT date_epoch, open, high, low, close,
                         (high + low + close) / 3.0 AS average_price,
                         volume, symbol
                  FROM nse.nse_charting_day WHERE {where} ORDER BY symbol, date_epoch"""
        print(f"  Fetching from nse.nse_charting_day...")
        results = self.client.query(sql, timeout=600, limit=10000000, verbose=True,
                                     memory_mb=16384, threads=6, disk_mb=40960, format="parquet")
        if not results:
            return pl.DataFrame()
        df = pl.read_parquet(io.BytesIO(results))
        df = df.with_columns([
            pl.col("symbol").cast(pl.Utf8),
            pl.lit("NSE").alias("exchange"),
            (pl.lit("NSE:") + pl.col("symbol").cast(pl.Utf8)).alias("instrument"),
        ])
        df = df.with_columns([
            pl.col("date_epoch").cast(pl.Int64), pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64), pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64), pl.col("average_price").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
        ]).sort(["instrument", "date_epoch"])
        print(f"  {df.height:,} rows, {df['instrument'].n_unique()} instruments")
        return df


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "strategies/eod_technical/config_kite_ato_match.yaml"
    results = run_pipeline(config_path, data_provider=NseChartingDataProvider())

    print(f"\n{'='*80}")
    print("RESULTS (nse.nse_charting_day - NSE native data)")
    print(f"{'='*80}")
    for r in results:
        cagr = (r.get('cagr') or 0) * 100
        dd = (r.get('max_drawdown') or 0) * 100
        calmar = r.get('calmar_ratio') or 0
        sv = r.get('start_value') or 0
        ev = r.get('end_value') or 0
        growth = ev / sv if sv > 0 else 0
        print(f"  {r['config_id']:<12} CAGR={cagr:>6.1f}%  MaxDD={dd:>6.1f}%  Calmar={calmar:>5.2f}  Growth={growth:>6.1f}x")

#!/usr/bin/env python3
"""Run BB Mean Reversion + SMA200 across all data sources.

Usage:
    python scripts/run_bb_mean_reversion.py                    # all variants
    python scripts/run_bb_mean_reversion.py --variant us       # US only (FMP API)
    python scripts/run_bb_mean_reversion.py --variant nse_fmp  # NSE via FMP API
    python scripts/run_bb_mean_reversion.py --variant nse_native  # NSE via nse_charting_day
    python scripts/run_bb_mean_reversion.py --variant nse_kite    # NSE via local Kite parquet
"""

import argparse
import io
import json
import os
import sys
import time

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.pipeline import run_pipeline
from engine.constants import SECONDS_IN_ONE_DAY
from lib.cr_client import CetaResearch


KITE_PARQUET = "/Users/swas/Desktop/Swas/Kite/ATO_SUITE/data/tick_data/data_source=kite/granularity=day/exchange=NSE/1747083733.767452.parquet"

STRATEGIES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies", "bb_mean_reversion")


class NseChartingDataProvider:
    """NSE native data via nse.nse_charting_day."""
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


class LocalKiteDataProvider:
    """NSE data from local Kite parquet."""
    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)
        df = pl.read_parquet(KITE_PARQUET)
        print(f"  Local Kite parquet: {df.height:,} rows, {df['symbol'].n_unique()} symbols")
        df = df.filter(
            (pl.col("date_epoch").cast(pl.Int64) >= fetch_start)
            & (pl.col("date_epoch").cast(pl.Int64) <= end_epoch)
        )
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))
        keep_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume", "symbol"]
        df = df.select([c for c in keep_cols if c in df.columns])
        df = df.with_columns([
            pl.col("symbol").cast(pl.Utf8),
            pl.lit("NSE").alias("exchange"),
        ])
        df = df.with_columns(
            (pl.lit("NSE:") + pl.col("symbol")).alias("instrument")
        )
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


VARIANTS = {
    "us_etf": {
        "config": os.path.join(STRATEGIES_DIR, "config_us_etf.yaml"),
        "provider": None,  # default CRDataProvider
        "label": "US ETF (SPY/QQQ/DIA/IWM)",
    },
    "us": {
        "config": os.path.join(STRATEGIES_DIR, "config_us.yaml"),
        "provider": None,  # default CRDataProvider
        "label": "US Broad (FMP API)",
    },
    "nse_fmp": {
        "config": os.path.join(STRATEGIES_DIR, "config_nse_fmp.yaml"),
        "provider": None,  # default CRDataProvider
        "label": "NSE (FMP API)",
    },
    "nse_native": {
        "config": os.path.join(STRATEGIES_DIR, "config_nse_native.yaml"),
        "provider": NseChartingDataProvider,
        "label": "NSE (nse_charting_day)",
    },
    "nse_kite": {
        "config": os.path.join(STRATEGIES_DIR, "config_nse_kite.yaml"),
        "provider": LocalKiteDataProvider,
        "label": "NSE (Kite parquet)",
    },
}


def run_variant(name, variant):
    print(f"\n{'='*80}")
    print(f"  BB Mean Reversion + SMA200: {variant['label']}")
    print(f"{'='*80}")

    provider_cls = variant["provider"]
    provider = provider_cls() if provider_cls else None

    t0 = time.time()
    results = run_pipeline(variant["config"], data_provider=provider)
    elapsed = round(time.time() - t0, 1)

    print(f"\n--- {variant['label']} Results ({elapsed}s) ---")
    if not results:
        print("  No results.")
        return {"variant": name, "label": variant["label"], "results": []}

    for r in results:
        cagr = (r.get('cagr') or 0) * 100
        dd = (r.get('max_drawdown') or 0) * 100
        calmar = r.get('calmar_ratio') or 0
        sharpe = r.get('sharpe_ratio') or 0
        sortino = r.get('sortino_ratio') or 0
        sv = r.get('start_value') or 0
        ev = r.get('end_value') or 0
        growth = ev / sv if sv > 0 else 0
        print(f"  {r['config_id']:<40} CAGR={cagr:>6.1f}%  DD={dd:>6.1f}%  "
              f"Calmar={calmar:>5.2f}  Sharpe={sharpe:>5.2f}  Sortino={sortino:>5.2f}  "
              f"Growth={growth:>5.1f}x")

    # Return summary for comparison
    best = results[0]
    return {
        "variant": name,
        "label": variant["label"],
        "best_config": best.get("config_id"),
        "cagr": best.get("cagr"),
        "max_drawdown": best.get("max_drawdown"),
        "calmar_ratio": best.get("calmar_ratio"),
        "sharpe_ratio": best.get("sharpe_ratio"),
        "sortino_ratio": best.get("sortino_ratio"),
        "num_configs": len(results),
        "elapsed": elapsed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", nargs="+", choices=list(VARIANTS.keys()),
                        help="Which variant(s) to run. Default: all.")
    args = parser.parse_args()

    variants_to_run = args.variant or list(VARIANTS.keys())
    summaries = []

    for name in variants_to_run:
        variant = VARIANTS[name]
        summary = run_variant(name, variant)
        summaries.append(summary)

    # Print comparison table
    print(f"\n{'='*80}")
    print("  BB Mean Reversion + SMA200: Cross-Data-Source Comparison")
    print(f"{'='*80}")
    print(f"  {'Variant':<25} {'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Sortino':>8}")
    print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for s in summaries:
        cagr = (s.get('cagr') or 0) * 100
        dd = (s.get('max_drawdown') or 0) * 100
        calmar = s.get('calmar_ratio') or 0
        sharpe = s.get('sharpe_ratio') or 0
        sortino = s.get('sortino_ratio') or 0
        print(f"  {s['label']:<25} {cagr:>7.1f}% {dd:>7.1f}% {calmar:>8.2f} {sharpe:>8.2f} {sortino:>8.2f}")

    # Save results
    output_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "results_bb_mean_reversion.json")
    with open(output_path, "w") as f:
        json.dump(summaries, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()

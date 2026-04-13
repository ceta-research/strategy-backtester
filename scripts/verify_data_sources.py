#!/usr/bin/env python3
"""Run EOD Technical on NSE using 3 independent data sources: FMP, NSE Charting, Kite.

Compares results to verify FMP data quality.
"""

import os
import sys
import tempfile
import time

import polars as pl
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.pipeline import run_pipeline
from engine.constants import SECONDS_IN_ONE_DAY


class NativeNSEDataProvider:
    """Fetch from nse.nse_charting_day (NSE's own data)."""

    def __init__(self):
        self.client = CetaResearch()

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        where = f"date_epoch >= {fetch_start} AND date_epoch <= {end_epoch}"
        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            where += f" AND symbol IN ({sym_list})"

        sql = f"""
            SELECT
                date_epoch,
                open, high, low, close,
                (high + low + close) / 3.0 AS average_price,
                volume,
                symbol
            FROM nse.nse_charting_day
            WHERE {where}
            ORDER BY symbol, date_epoch
        """

        print(f"  Fetching from nse.nse_charting_day...")
        results = self.client.query(sql, timeout=600, limit=10000000, verbose=True,
                                     memory_mb=16384, threads=6, disk_mb=40960, format="parquet")

        if not results:
            return pl.DataFrame()

        import io
        df = pl.read_parquet(io.BytesIO(results))

        # Add exchange and instrument columns (NSE symbols are bare)
        df = df.with_columns([
            pl.lit("NSE").alias("exchange"),
            (pl.lit("NSE:") + pl.col("symbol").cast(pl.Utf8)).alias("instrument"),
        ])

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
        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments from NSE Charting")
        return df


class KiteDataProvider:
    """Fetch from kite.kite_historical_day (Zerodha Kite data)."""

    def __init__(self):
        self.client = CetaResearch()

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)

        where = f"date_epoch >= {fetch_start} AND date_epoch <= {end_epoch} AND exchange = 'NSE'"
        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            where += f" AND symbol IN ({sym_list})"

        sql = f"""
            SELECT
                date_epoch,
                open, high, low, close,
                COALESCE(average_price, (high + low + close) / 3.0) AS average_price,
                volume,
                symbol,
                exchange
            FROM kite.kite_historical_day
            WHERE {where}
            ORDER BY symbol, date_epoch
        """

        print(f"  Fetching from kite.kite_historical_day...")
        results = self.client.query(sql, timeout=600, limit=10000000, verbose=True,
                                     memory_mb=16384, threads=6, disk_mb=40960, format="parquet")

        if not results:
            return pl.DataFrame()

        import io
        df = pl.read_parquet(io.BytesIO(results))

        # Add instrument column (Kite symbols are bare)
        df = df.with_columns(
            (pl.lit("NSE:") + pl.col("symbol").cast(pl.Utf8)).alias("instrument")
        )

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
        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments from Kite")
        return df


def make_config():
    return {
        "static": {
            "strategy_type": "eod_technical",
            "start_margin": 1000000,
            "start_epoch": 1262304000,   # 2010-01-01
            "end_epoch": 1735689600,     # 2025-01-01
            "prefetch_days": 400,
            "data_granularity": "day",
        },
        "scanner": {
            "instruments": [[{"exchange": "NSE", "symbols": []}]],
            "price_threshold": [50],
            "avg_day_transaction_threshold": [{"period": 125, "threshold": 70000000}],
            "n_day_gain_threshold": [{"n": 180, "threshold": 0}],
        },
        "entry": {
            "n_day_ma": [3],
            "n_day_high": [5],
            "direction_score": [{"n_day_ma": 3, "score": 0.54}],
        },
        "exit": {
            "min_hold_time_days": [4],
            "trailing_stop_pct": [10],
        },
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_performer"],
            "order_ranking_window_days": [180],
            "max_positions": [10],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
        },
    }


def main():
    sources = [
        ("FMP (fmp.stock_eod)", None),  # default CRDataProvider
        ("NSE Charting (nse.nse_charting_day)", NativeNSEDataProvider()),
        ("Kite (kite.kite_historical_day)", KiteDataProvider()),
    ]

    summary = []

    for label, provider in sources:
        print(f"\n{'=' * 70}")
        print(f"  DATA SOURCE: {label}")
        print(f"{'=' * 70}")

        config = make_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            tmp = f.name

        t0 = time.time()
        try:
            results = run_pipeline(tmp, data_provider=provider)
            elapsed = round(time.time() - t0, 1)

            if results:
                best = results[0]
                cagr = (best.get("cagr") or 0) * 100
                dd = (best.get("max_drawdown") or 0) * 100
                calmar = best.get("calmar_ratio") or 0
                sharpe = best.get("sharpe_ratio") or 0
                start_v = best.get("start_value") or 0
                end_v = best.get("end_value") or 0
                days = best.get("num_trading_days") or 0
                summary.append({
                    "source": label, "cagr": cagr, "max_dd": dd,
                    "calmar": calmar, "sharpe": sharpe,
                    "start": start_v, "end": end_v, "days": days,
                    "elapsed": elapsed,
                })
            else:
                summary.append({"source": label, "cagr": 0, "error": "no results"})
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            print(f"  ERROR: {e}")
            summary.append({"source": label, "cagr": 0, "error": str(e)[:80]})
        finally:
            os.unlink(tmp)

    # Print comparison
    print(f"\n\n{'=' * 90}")
    print("DATA SOURCE VERIFICATION: EOD Technical on NSE (2010-2025)")
    print(f"{'=' * 90}")
    print(f"{'Source':<40} {'CAGR':>8} {'MaxDD':>8} {'Calmar':>8} {'Sharpe':>8} {'Days':>6} {'Time':>6}")
    print("-" * 85)
    for s in summary:
        if "error" not in s:
            print(f"{s['source']:<40} {s['cagr']:>7.1f}% {s['max_dd']:>7.1f}% {s['calmar']:>8.2f} {s['sharpe']:>8.2f} {s['days']:>6} {s['elapsed']:>5.0f}s")
        else:
            print(f"{s['source']:<40} FAILED: {s.get('error', 'unknown')}")


if __name__ == "__main__":
    main()

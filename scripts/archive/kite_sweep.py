#!/usr/bin/env python3
"""Aggressive parameter sweep on Kite data to match ATO_Simulator's ~15% CAGR."""

import io
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


class KiteDataProvider:
    def __init__(self):
        self.client = CetaResearch()

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - (prefetch_days * SECONDS_IN_ONE_DAY)
        where = f"date_epoch >= {fetch_start} AND date_epoch <= {end_epoch} AND exchange = 'NSE'"
        if symbols:
            sym_list = ", ".join(f"'{s}'" for s in symbols)
            where += f" AND symbol IN ({sym_list})"

        sql = f"""
            SELECT date_epoch, open, high, low, close,
                   COALESCE(average_price, (high + low + close) / 3.0) AS average_price,
                   volume, symbol, exchange
            FROM kite.kite_historical_day
            WHERE {where}
            ORDER BY symbol, date_epoch
        """
        print(f"  Fetching from kite.kite_historical_day...")
        results = self.client.query(sql, timeout=600, limit=10000000, verbose=True,
                                     memory_mb=16384, threads=6, disk_mb=40960, format="parquet")
        if not results:
            return pl.DataFrame()

        df = pl.read_parquet(io.BytesIO(results))
        df = df.with_columns(
            (pl.lit("NSE:") + pl.col("symbol").cast(pl.Utf8)).alias("instrument")
        )
        numeric_cols = ["date_epoch", "open", "high", "low", "close", "average_price", "volume"]
        cast_exprs = [
            pl.col(c).cast(pl.Int64 if c == "date_epoch" else pl.Float64).alias(c)
            for c in numeric_cols if c in df.columns
        ]
        df = df.with_columns(cast_exprs).sort(["instrument", "date_epoch"])
        print(f"  Fetched {df.height} rows, {df['instrument'].n_unique()} instruments from Kite")
        return df


def main():
    # Match the notebook's config space
    config = {
        "static": {
            "strategy_type": "eod_technical",
            "start_margin": 10000000,  # 1 Cr (notebook uses 1 Cr)
            "start_epoch": 1430438400,   # 2015-05-01 (notebook period)
            "end_epoch": 1735689600,     # 2025-01-01
            "prefetch_days": 400,
            "data_granularity": "day",
        },
        "scanner": {
            "instruments": [[{"exchange": "NSE", "symbols": []}]],
            "price_threshold": [50],
            "avg_day_transaction_threshold": [
                {"period": 125, "threshold": 37000000},
                {"period": 125, "threshold": 70000000},
            ],
            "n_day_gain_threshold": [
                {"n": 180, "threshold": 0},
                {"n": 360, "threshold": 0},
            ],
        },
        "entry": {
            "n_day_ma": [3],
            "n_day_high": [2, 5],
            "direction_score": [{"n_day_ma": 3, "score": 0.54}],
        },
        "exit": {
            "min_hold_time_days": [1, 3, 4],
            "trailing_stop_loss": [10, 15, 20],
        },
        "simulation": {
            "default_sorting_type": ["top_gainer"],
            "order_sorting_type": ["top_performer"],
            "order_ranking_window_days": [180],
            "max_positions": [10, 20],
            "max_positions_per_instrument": [1],
            "order_value_multiplier": [1],
            "max_order_value": [{"type": "percentage_of_instrument_avg_txn", "value": 4.5}],
        },
    }

    # Count configs
    from engine.config_sweep import create_config_iterator
    from engine.config_loader import load_config
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        tmp = f.name

    c = load_config(tmp)
    s, _ = create_config_iterator(**c["scanner_config_input"])
    en, _ = create_config_iterator(**c["entry_config_input"])
    ex, _ = create_config_iterator(**c["exit_config_input"])
    si, _ = create_config_iterator(**c["simulation_config_input"])
    total = s * en * ex * si
    print(f"Total configs: {s} scanner x {en} entry x {ex} exit x {si} sim = {total}")

    provider = KiteDataProvider()
    results = run_pipeline(tmp, data_provider=provider)
    os.unlink(tmp)

    if not results:
        print("No results.")
        return

    # Print top 20
    print(f"\n{'=' * 100}")
    print(f"TOP 20 BY CALMAR (Kite data, 2015-2025)")
    print(f"{'=' * 100}")
    print(f"{'#':<3} {'Config':<12} {'CAGR':>7} {'MaxDD':>8} {'Calmar':>8} {'Sharpe':>8}")
    print("-" * 55)
    for i, r in enumerate(results[:20]):
        cagr = (r.get("cagr") or 0) * 100
        dd = (r.get("max_drawdown") or 0) * 100
        calmar = r.get("calmar_ratio") or 0
        sharpe = r.get("sharpe_ratio") or 0
        print(f"{i+1:<3} {r['config_id']:<12} {cagr:>6.1f}% {dd:>7.1f}% {calmar:>8.2f} {sharpe:>8.2f}")

    # Stats
    cagrs = [(r.get("cagr") or 0) * 100 for r in results]
    print(f"\nAll {len(results)} configs: min={min(cagrs):.1f}%, median={sorted(cagrs)[len(cagrs)//2]:.1f}%, max={max(cagrs):.1f}%")


if __name__ == "__main__":
    main()

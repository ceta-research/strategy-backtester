"""Benchmark: CR API (JSON vs parquet) vs local parquet (pandas vs DuckDB vs Polars).

Measures wall time, peak memory, and row counts for the same EOD data fetched
via five providers:
  1. CRDataProvider (parquet): remote API, parquet response (native format)
  2. CRDataProvider (json): remote API, JSON response (legacy)
  3. DuckDBParquetDataProvider: local parquet + DuckDB pushdown
  4. PolarsParquetDataProvider: local parquet + Polars lazy scan
  5. FMPParquetDataProvider: local parquet + pandas read/filter

Usage:
    source .venv/bin/activate
    cd strategy-backtester
    python scripts/benchmark_data_source.py [--scenario all|small|medium|large]
"""

import argparse
import gc
import os
import sys
import time
import tracemalloc
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.data_provider import (
    CRDataProvider,
    DuckDBParquetDataProvider,
    FMPParquetDataProvider,
    PolarsParquetDataProvider,
)

# Default FMP parquet directory
FMP_PARQUET_DIR = os.path.expanduser(
    "~/Desktop/Swas/Kite/ATO_SUITE/data/data_source=fmp/tick_data/eod"
)

# Benchmark scenarios
SCENARIOS = {
    "small": {
        "description": "NSE, 10 symbols, 1 year",
        "exchanges": ["NSE"],
        "symbols": [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
            "SBIN", "BAJFINANCE", "BHARTIARTL", "HINDUNILVR", "ITC",
        ],
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "prefetch_days": 400,
    },
    "medium": {
        "description": "NSE, all symbols, 5 years",
        "exchanges": ["NSE"],
        "symbols": None,
        "start_date": "2020-01-01",
        "end_date": "2024-12-31",
        "prefetch_days": 400,
    },
    "large": {
        "description": "NSE + BSE, all symbols, 5 years",
        "exchanges": ["NSE", "BSE"],
        "symbols": None,
        "start_date": "2020-01-01",
        "end_date": "2024-12-31",
        "prefetch_days": 400,
    },
}


def date_to_epoch(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def run_benchmark(provider, scenario, label):
    """Run a single benchmark and return metrics."""
    start_epoch = date_to_epoch(scenario["start_date"])
    end_epoch = date_to_epoch(scenario["end_date"])

    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()

    df = provider.fetch_ohlcv(
        exchanges=scenario["exchanges"],
        symbols=scenario["symbols"],
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        prefetch_days=scenario["prefetch_days"],
    )

    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rows = len(df) if df is not None and not df.empty else 0
    instruments = df["instrument"].nunique() if rows > 0 else 0

    result = {
        "label": label,
        "scenario": scenario["description"],
        "wall_time_s": round(elapsed, 2),
        "peak_memory_mb": round(peak / 1024 / 1024, 1),
        "rows": rows,
        "instruments": instruments,
    }

    print(f"  {label}: {elapsed:.2f}s, {peak / 1024 / 1024:.1f} MB peak, "
          f"{rows:,} rows, {instruments} instruments")

    del df
    gc.collect()

    return result


def run_scenario(name, scenario, parquet_dir):
    """Run all providers for a scenario."""
    print(f"\n{'='*60}")
    print(f"Scenario: {name} - {scenario['description']}")
    print(f"{'='*60}")

    results = []

    # 1. CR API with parquet format (should be fastest remote)
    print("\n  [CR API (parquet format)]")
    try:
        cr_pq = CRDataProvider(format="parquet")
        r = run_benchmark(cr_pq, scenario, "cr_api_parquet")
        results.append(r)
    except Exception as e:
        print(f"  CR API parquet failed: {e}")

    # 2. CR API with JSON format (legacy, for comparison)
    print("\n  [CR API (json format)]")
    try:
        cr_json = CRDataProvider(format="json")
        r = run_benchmark(cr_json, scenario, "cr_api_json")
        results.append(r)
    except Exception as e:
        print(f"  CR API json failed: {e}")

    # 3. DuckDB local
    if os.path.isdir(parquet_dir):
        print("\n  [DuckDB Local Parquet]")
        duckdb_provider = DuckDBParquetDataProvider(parquet_dir)
        r = run_benchmark(duckdb_provider, scenario, "duckdb_local")
        results.append(r)

    # 4. Polars local parquet
    if os.path.isdir(parquet_dir):
        print("\n  [Polars Local Parquet]")
        polars_provider = PolarsParquetDataProvider(parquet_dir)
        r = run_benchmark(polars_provider, scenario, "polars_local")
        results.append(r)

    # 5. Pandas local parquet
    if os.path.isdir(parquet_dir):
        print("\n  [Pandas Local Parquet]")
        parquet_provider = FMPParquetDataProvider(parquet_dir)
        r = run_benchmark(parquet_provider, scenario, "pandas_local")
        results.append(r)

    return results


def print_comparison(all_results):
    """Print summary comparison table."""
    print(f"\n{'='*100}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*100}")
    print(f"{'Scenario':<30} {'Provider':<18} {'Time (s)':<10} {'Memory (MB)':<13} {'Rows':<12} {'Instruments'}")
    print("-" * 95)

    for r in all_results:
        print(f"{r['scenario']:<30} {r['label']:<18} {r['wall_time_s']:<10} "
              f"{r['peak_memory_mb']:<13} {r['rows']:<12,} {r['instruments']}")

    # Find the best per scenario
    print(f"\n{'='*100}")
    print("RANKING (fastest to slowest per scenario)")
    print(f"{'='*100}")

    scenarios_seen = {}
    for r in all_results:
        scenarios_seen.setdefault(r["scenario"], []).append(r)

    for scenario, entries in scenarios_seen.items():
        ranked = sorted(entries, key=lambda r: r["wall_time_s"])
        print(f"\n  {scenario}:")
        for i, r in enumerate(ranked):
            marker = " <-- WINNER" if i == 0 else ""
            row_note = ""
            if ranked[0]["rows"] != r["rows"]:
                row_note = f" [ROWS MISMATCH: {r['rows']:,} vs {ranked[0]['rows']:,}]"
            if i > 0:
                slowdown = r["wall_time_s"] / ranked[0]["wall_time_s"]
                print(f"    {i+1}. {r['label']:<18} {r['wall_time_s']}s ({slowdown:.1f}x slower), "
                      f"{r['peak_memory_mb']} MB{row_note}")
            else:
                print(f"    {i+1}. {r['label']:<18} {r['wall_time_s']}s{marker}, "
                      f"{r['peak_memory_mb']} MB{row_note}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark data source providers")
    parser.add_argument("--scenario", default="all",
                        choices=["all", "small", "medium", "large"],
                        help="Which scenario to run")
    parser.add_argument("--parquet-dir", default=FMP_PARQUET_DIR,
                        help="Path to FMP EOD parquet directory")
    args = parser.parse_args()

    parquet_dir = args.parquet_dir

    print("Data Source Benchmark: CR API (json/parquet) vs Local (DuckDB/pandas)")
    print(f"FMP parquet dir: {parquet_dir}")
    print(f"Parquet dir exists: {os.path.isdir(parquet_dir)}")

    all_results = []

    if args.scenario == "all":
        for name in ["small", "medium", "large"]:
            results = run_scenario(name, SCENARIOS[name], parquet_dir)
            all_results.extend(results)
    else:
        results = run_scenario(args.scenario, SCENARIOS[args.scenario], parquet_dir)
        all_results.extend(results)

    print_comparison(all_results)


if __name__ == "__main__":
    main()

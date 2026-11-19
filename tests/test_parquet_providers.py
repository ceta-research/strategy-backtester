"""The local Parquet providers live in scripts/parquet_providers.py: no config selects them, so they stay
out of engine/ and the files a hosted run ships with."""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import data_provider  # noqa: E402
from scripts import parquet_providers  # noqa: E402

NAMES = ("ParquetDataProvider", "FMPParquetDataProvider", "DuckDBParquetDataProvider", "PolarsParquetDataProvider")
USERS = ("scripts/measure_bias_impact.py", "scripts/benchmark_data_source.py",
         "tests/verification/run_strategy_backtester.py")


def test_the_parquet_providers_live_outside_the_engine():
    for name in NAMES:
        assert hasattr(parquet_providers, name), name
        assert not hasattr(data_provider, name), name


def test_each_user_imports_them_from_their_new_home():
    for path in USERS:
        with open(os.path.join(ROOT, path)) as f:
            tree = ast.parse(f.read())
        found = [(n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 for a in n.names if a.name in NAMES]
        assert found, path
        assert {module for module, _ in found} == {"scripts.parquet_providers"}, (path, found)

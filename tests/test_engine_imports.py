"""The platform's image holds run.py, engine/, lib/ and tests/test_pipeline.py, so they import nothing else
from the repo: a hosted run that reached scripts/ or studies/ would fail there."""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = ("run.py", "tests/test_pipeline.py")
PACKAGES = ("engine", "lib")


def _repo_imports_outside(path):
    """The top-level repo folders other than engine/ and lib/ that path imports from, at any depth."""
    with open(path) as f:
        tree = ast.parse(f.read())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0]
    tops = {name.split(".")[0] for name in names}
    return sorted(t for t in tops if t not in PACKAGES and os.path.isdir(os.path.join(ROOT, t)))


def _shipped_files():
    files = [os.path.join(ROOT, f) for f in SHIPPED]
    for pkg in PACKAGES:
        for folder, _, names in os.walk(os.path.join(ROOT, pkg)):
            files += [os.path.join(folder, n) for n in names if n.endswith(".py")]
    return files


def test_the_shipped_files_import_only_engine_and_lib():
    outside = {os.path.relpath(f, ROOT): found for f in _shipped_files() if (found := _repo_imports_outside(f))}
    assert outside == {}


def test_an_import_from_another_folder_is_caught(tmp_path):
    path = tmp_path / "planted.py"
    path.write_text("def f():\n    from scripts.parquet_providers import ParquetDataProvider\n")
    assert _repo_imports_outside(str(path)) == ["scripts"]

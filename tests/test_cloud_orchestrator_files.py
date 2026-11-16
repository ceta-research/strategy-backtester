"""Tests for the file list lib/cloud_orchestrator.py uploads to cloud compute."""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cloud_orchestrator import ROOT, CloudOrchestrator

# Imported by uploaded files but not uploaded: the root CLAUDE.md gotcha lists them.
KNOWN_MISSING = {"engine/exits.py", "engine/order_key.py", "engine/internal_regime.py",
                 "lib/equity_curve.py"}


def _imported_modules(path):
    """Repo files an uploaded file imports, at module level or inside a function."""
    found = set()
    with open(os.path.join(ROOT, path)) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            rel = name.replace(".", "/")
            for cand in (rel + ".py", rel + "/__init__.py"):
                if rel.split("/")[0] in ("engine", "lib") and os.path.isfile(os.path.join(ROOT, cand)):
                    found.add(cand)
    return found


class TestDiscoverFiles(unittest.TestCase):

    def test_eod_upload_holds_every_repo_module_it_imports(self):
        files = set(CloudOrchestrator.__new__(CloudOrchestrator).discover_files("eod"))
        missing = {m for f in files for m in _imported_modules(f)} - files - KNOWN_MISSING
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()

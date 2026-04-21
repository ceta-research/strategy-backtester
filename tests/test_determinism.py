"""Determinism tests: sweep config_id stability + polars group_by audit."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config_sweep import create_config_iterator


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestConfigIteratorDeterminism(unittest.TestCase):
    """Same params → same sequence of config dicts."""

    def test_same_inputs_yield_same_ids(self):
        total_a, gen_a = create_config_iterator(
            a=[1, 2, 3], b=[10, 20], c=["x", "y"]
        )
        total_b, gen_b = create_config_iterator(
            a=[1, 2, 3], b=[10, 20], c=["x", "y"]
        )
        self.assertEqual(total_a, total_b)
        self.assertEqual(list(gen_a), list(gen_b))

    def test_id_assignment_is_sequential_and_stable(self):
        _, gen = create_config_iterator(a=[1, 2], b=[10, 20])
        ids = [c["id"] for c in gen]
        self.assertEqual(ids, [1, 2, 3, 4])

    def test_compound_params_deterministic(self):
        configs_a = list(create_config_iterator(
            score=[{"n": 3, "s": 0.5}, {"n": 5, "s": 0.6}],
            x=[1, 2],
        )[1])
        configs_b = list(create_config_iterator(
            score=[{"n": 3, "s": 0.5}, {"n": 5, "s": 0.6}],
            x=[1, 2],
        )[1])
        self.assertEqual(configs_a, configs_b)


class TestGroupByMaintainOrderAudit(unittest.TestCase):
    """All group_by calls in engine/ must carry `maintain_order=True`.

    Polars group_by is ordering-unstable by default; catches future
    regressions where a dev forgets the kwarg.
    """

    def _scan_file(self, path):
        """Return list of (line_no, line) for group_by calls missing maintain_order."""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        # Multiline-aware: find group_by( ... ) and check for maintain_order
        # within the call's arg list. We cheat and line-match because in
        # practice every call in this repo is on a single line.
        offenders = []
        for i, line in enumerate(text.splitlines(), 1):
            if "group_by(" in line and "maintain_order" not in line:
                # Heuristic: if the next 3 lines contain maintain_order, it's a
                # multi-line call; count as OK.
                tail = "\n".join(text.splitlines()[i:i + 3])
                if "maintain_order" in tail:
                    continue
                offenders.append((i, line.strip()))
        return offenders

    def test_engine_signals_group_by_has_maintain_order(self):
        signals_dir = os.path.join(REPO_ROOT, "engine", "signals")
        offenders_by_file = {}
        for fname in sorted(os.listdir(signals_dir)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(signals_dir, fname)
            offenders = self._scan_file(path)
            if offenders:
                offenders_by_file[fname] = offenders
        self.assertFalse(
            offenders_by_file,
            f"Found group_by() calls missing maintain_order=True: "
            f"{offenders_by_file}. Polars group_by is ordering-unstable by "
            f"default; add maintain_order=True or justify with a comment."
        )

    def test_engine_root_group_by_has_maintain_order(self):
        """Same audit for engine/*.py (non-signal modules)."""
        engine_dir = os.path.join(REPO_ROOT, "engine")
        offenders_by_file = {}
        for fname in sorted(os.listdir(engine_dir)):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            path = os.path.join(engine_dir, fname)
            if os.path.isdir(path):
                continue
            offenders = self._scan_file(path)
            if offenders:
                offenders_by_file[fname] = offenders
        self.assertFalse(
            offenders_by_file,
            f"engine/*.py group_by without maintain_order: {offenders_by_file}"
        )


if __name__ == "__main__":
    unittest.main()

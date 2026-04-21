"""Regression snapshot utility.

Captures the core summary metrics of a backtest result as a pinned JSON
snapshot, then diffs a later result against it with a tolerance. Used to
detect unintended changes when refactoring the simulator, metrics, or
exit logic.

Snapshot format (tests/regression/snapshots/<name>.json):
    {
      "strategy": "...",
      "params": {...},
      "pinned_metrics": {
        "cagr": 0.125,
        "max_drawdown": -0.41,
        ...
      },
      "tolerance": {"abs": 1e-6, "rel": 1e-4}
    }

Typical flow:
    1. Capture current behavior:   python -m tests.regression.snapshot capture <result.json>
    2. Re-run backtest after fix:  generate new result.json
    3. Compare:                    python -m tests.regression.snapshot compare <name> <result.json>
    4. If diff is intended, re-capture; if not, the PR reveals an unintended regression.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

# Metrics we pin. Extend deliberately — every added field becomes a regression gate.
PINNED_FIELDS = [
    "cagr", "total_return", "max_drawdown", "annualized_volatility",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "total_trades", "win_rate", "profit_factor",
    "final_value", "peak_value",
]


def _top_config(result_dict):
    """Return the dict to extract metrics from, handling all result shapes.

    Shapes observed in results/:
      - Single-result (BacktestResult.save()): {"summary": {...}, "strategy": {...}}
      - Sweep (SweepResult.save()): {"type": "sweep", "detailed": [{...}], "meta": {...},
        "all_configs": [{...}]}
      - Legacy list shape: [{...}] — bare list of configs
      - Legacy sweep key: {"configs": [...]} — kept for back-compat

    For sweeps, we prefer `detailed[0]` because it retains full summary;
    `all_configs[0]` has metrics flattened to top level (no "summary" sub-dict).
    """
    if isinstance(result_dict, list):
        return result_dict[0] if result_dict else {}
    if result_dict.get("type") == "sweep" or "detailed" in result_dict:
        detailed = result_dict.get("detailed") or result_dict.get("configs") or []
        if detailed:
            return detailed[0]
        # Fall back to all_configs (flattened, no summary key)
        all_configs = result_dict.get("all_configs") or []
        return all_configs[0] if all_configs else {}
    return result_dict


def _extract(result_dict):
    """Pull pinned fields from a BacktestResult.save() or SweepResult output."""
    top = _top_config(result_dict)
    if not top:
        return {k: None for k in PINNED_FIELDS}
    # Single-config has fields under top["summary"]; sweep's all_configs[i]
    # flattens them to top level. Try summary first, fall back to top.
    summary = top.get("summary")
    source = summary if summary else top
    return {k: source.get(k) for k in PINNED_FIELDS}


def _identity(result_dict):
    """Pull strategy/params for snapshot metadata, handling all shapes."""
    # Sweep shape: identity is in "meta"; top-level detailed config has params only.
    if isinstance(result_dict, dict):
        meta = result_dict.get("meta")
        if isinstance(meta, dict):
            top = _top_config(result_dict)
            return {
                "strategy": meta.get("strategy_name") or meta.get("name"),
                "params": top.get("params"),
                "instrument": meta.get("instrument"),
                "exchange": meta.get("exchange"),
            }
        strat = result_dict.get("strategy")
        if isinstance(strat, dict):
            return {
                "strategy": strat.get("name"),
                "params": strat.get("params"),
                "instrument": strat.get("instrument"),
                "exchange": strat.get("exchange"),
            }
    if isinstance(result_dict, list) and result_dict:
        top = result_dict[0]
        strat = top.get("strategy")
        return {
            "strategy": strat.get("name") if isinstance(strat, dict) else None,
            "params": top.get("params"),
        }
    return {}


def capture(result_path, name=None):
    """Write a snapshot from an existing result.json.

    Args:
        result_path: Path to a result.json produced by BacktestResult.save().
        name: Snapshot name (defaults to strategy name from the result).
    """
    with open(result_path) as f:
        result = json.load(f)

    ident = _identity(result)
    if name is None:
        name = ident.get("strategy", "unknown")

    snapshot = {
        **ident,
        "pinned_metrics": _extract(result),
        "tolerance": {"abs": 1e-6, "rel": 1e-4},
        "source_result": str(result_path),
    }

    path = SNAPSHOT_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Captured snapshot: {path}")
    return path


def compare(name, result_path):
    """Compare a new result against a pinned snapshot.

    Returns (ok: bool, diffs: list[dict]). If ok is False, diffs lists every
    pinned field that exceeded tolerance.
    """
    snap_path = SNAPSHOT_DIR / f"{name}.json"
    if not snap_path.exists():
        raise FileNotFoundError(f"No snapshot for {name!r} at {snap_path}")

    with open(snap_path) as f:
        snapshot = json.load(f)
    with open(result_path) as f:
        result = json.load(f)

    expected = snapshot["pinned_metrics"]
    actual = _extract(result)
    tol_abs = snapshot["tolerance"]["abs"]
    tol_rel = snapshot["tolerance"]["rel"]

    diffs = []
    for field in PINNED_FIELDS:
        e = expected.get(field)
        a = actual.get(field)
        if e is None and a is None:
            continue
        if e is None or a is None:
            diffs.append({"field": field, "expected": e, "actual": a,
                          "reason": "null_mismatch"})
            continue
        diff = abs(a - e)
        rel_diff = diff / abs(e) if e != 0 else diff
        if diff > tol_abs and rel_diff > tol_rel:
            diffs.append({"field": field, "expected": e, "actual": a,
                          "abs_diff": diff, "rel_diff": rel_diff})
    return len(diffs) == 0, diffs


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "capture":
        if len(argv) < 3:
            print("Usage: snapshot.py capture <result.json> [name]")
            return 1
        capture(argv[2], argv[3] if len(argv) > 3 else None)
        return 0
    if cmd == "compare":
        if len(argv) < 4:
            print("Usage: snapshot.py compare <name> <result.json>")
            return 1
        ok, diffs = compare(argv[2], argv[3])
        if ok:
            print(f"OK: {argv[2]} matches snapshot")
            return 0
        print(f"REGRESSION: {argv[2]}")
        for d in diffs:
            print(f"  {d}")
        return 2
    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

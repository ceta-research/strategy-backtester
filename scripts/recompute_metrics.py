"""Recompute metrics for a saved result.json using the corrected EquityCurve path.

Every BacktestResult.save() output embeds its `equity_curve`, so we can
recompute CAGR/Sharpe/vol correctly after the Layer 1 metrics fix WITHOUT
rerunning the simulation. This is the migration path for the ~50+ historical
results in `results/` that were produced with the buggy `years = n/ppy` formula.

Usage:
    python -m scripts.recompute_metrics <result.json> [--out <result_v2.json>]
    python -m scripts.recompute_metrics --dir results/ --out-dir results_v2/

Report shape:
    {
      "strategy": "...",
      "before": {"cagr": 0.099, "sharpe_ratio": 0.61, ...},
      "after":  {"cagr": 0.145, "sharpe_ratio": 0.73, ...},
      "delta":  {"cagr": 0.046, ...}
    }

The `after` numbers are the authoritative ones. Every external claim citing
pre-fix CAGR/Sharpe/Calmar should be reviewed against the post-fix values.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.equity_curve import EquityCurve, Frequency
from lib.metrics import compute_metrics_from_curve

RECOMPUTED_FIELDS = [
    "cagr", "total_return", "max_drawdown", "annualized_volatility",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "var_95", "cvar_95", "best_period", "worst_period",
    "skewness", "kurtosis",
]


def _detect_frequency(pairs):
    """Infer sampling frequency from the point density.

    Forward-filled (calendar) curves emit a point for every day including
    weekends: density ~ 365/year. Trading-day curves emit only on exchange
    sessions: density ~ 252/year. We threshold at 300 points/year.

    Intraday and lower-frequency curves are out of scope for migration of
    historical EOD results; fall back to DAILY_CALENDAR with a warning if
    density is way off.
    """
    if len(pairs) < 2:
        return Frequency.DAILY_CALENDAR
    span_years = (pairs[-1][0] - pairs[0][0]) / (365.25 * 86400)
    if span_years <= 0:
        return Frequency.DAILY_CALENDAR
    density = len(pairs) / span_years
    if density > 300:
        return Frequency.DAILY_CALENDAR
    if density > 200:
        return Frequency.DAILY_TRADING
    if density > 40:
        return Frequency.WEEKLY
    if density > 9:
        return Frequency.MONTHLY
    if density > 3:
        return Frequency.QUARTERLY
    return Frequency.ANNUAL


def _extract_curve(result, frequency=None):
    """Build EquityCurve from a result.json's `equity_curve` field.

    If `frequency` is None, infer from the point density. This is the right
    default for migration: it auto-handles the mix of trading-day and
    calendar-day curves in historical `results/`.
    """
    ec = result.get("equity_curve", [])
    if not ec:
        return None
    pairs = [(int(p["epoch"]), float(p["value"])) for p in ec]
    if frequency is None:
        frequency = _detect_frequency(pairs)
    return EquityCurve.from_pairs(pairs, frequency)


def _write_migrated(result_path, out_path, top, new_port_metrics, new_bench_metrics,
                    new_comparison, frequency_name, report):
    """Write a v2 result file with corrected summary + provenance of the old one.

    Preserves the original file's shape (single-result or sweep) and structure;
    only the summary/benchmark/comparison of the top config are overwritten.
    The full original summary is preserved under `summary_v1_pre_fix` for audit.
    """
    with open(result_path) as f:
        original = json.load(f)

    def _patch_config_dict(cfg):
        """Overwrite cfg["summary"], cfg["benchmark"], cfg["comparison"] in place."""
        old_summary = cfg.get("summary", {})
        # Preserve trade metrics and time-series outputs from the old result
        # (they don't depend on the ppy bug). Only overwrite the fields the
        # EquityCurve path actually recomputes.
        new_summary = dict(old_summary)
        new_summary.update(new_port_metrics)
        cfg["summary"] = new_summary
        cfg["summary_v1_pre_fix"] = old_summary
        cfg["benchmark"] = new_bench_metrics
        cfg["comparison"] = new_comparison
        cfg["equity_curve_frequency"] = frequency_name

    if isinstance(original, list):
        if original:
            _patch_config_dict(original[0])
    elif original.get("type") == "sweep":
        detailed = original.get("detailed") or []
        if detailed:
            _patch_config_dict(detailed[0])
            # Sweep files also carry `all_configs` — a flat list of per-config
            # metrics (no equity_curve). The config we just migrated must exist
            # there too; propagate the new fields so detailed and all_configs
            # agree. Match by params (the only reliable key — rank isn't always
            # present in all_configs).
            top_params = detailed[0].get("params")
            for ac in original.get("all_configs") or []:
                if ac.get("params") == top_params:
                    ac["_v1_pre_fix"] = {
                        k: ac.get(k) for k in new_port_metrics.keys()
                        if k in ac
                    }
                    ac.update(new_port_metrics)
                    break
        original["migration_version"] = "v2"
        original["migration_frequency"] = frequency_name
    else:
        _patch_config_dict(original)

    # Always record a top-level migration marker
    if isinstance(original, dict):
        original.setdefault("migration_report", report)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(original, f, indent=2)


def recompute_one(result_path, frequency=None, risk_free_rate=None):
    """Recompute metrics for a single result.json. Returns diff report dict."""
    with open(result_path) as f:
        result = json.load(f)

    # Handle sweep shape: pick top detailed config (has equity_curve + summary)
    if isinstance(result, list):
        top = result[0] if result else {}
        is_sweep = True
    elif result.get("type") == "sweep":
        detailed = result.get("detailed") or result.get("configs") or []
        top = detailed[0] if detailed else {}
        is_sweep = True
    else:
        top = result
        is_sweep = False

    # Honor persisted frequency from v1.1+ results if present; else auto-detect.
    if frequency is None:
        persisted = top.get("equity_curve_frequency")
        if not persisted and isinstance(result, dict):
            persisted = result.get("equity_curve_frequency")
        if persisted:
            try:
                frequency = Frequency[persisted]
            except KeyError:
                frequency = None  # unknown name -> fall back to auto-detect

    curve = _extract_curve(top, frequency=frequency)
    if curve is None or len(curve) < 2:
        return {"path": str(result_path), "error": "no_equity_curve"}
    resolved_frequency = curve.frequency

    if risk_free_rate is None:
        risk_free_rate = (top.get("strategy") or {}).get("risk_free_rate", 0.02)

    new_metrics = compute_metrics_from_curve(curve, risk_free_rate=risk_free_rate)
    new_port = new_metrics["portfolio"]

    old_port = top.get("summary", {})

    before = {k: old_port.get(k) for k in RECOMPUTED_FIELDS}
    after = {k: new_port.get(k) for k in RECOMPUTED_FIELDS}

    delta = {}
    for k in RECOMPUTED_FIELDS:
        b, a = before[k], after[k]
        if b is None or a is None:
            delta[k] = None
        else:
            try:
                delta[k] = a - b
            except TypeError:
                delta[k] = None

    return {
        "path": str(result_path),
        "strategy": (top.get("strategy") or {}).get("name"),
        "params": (top.get("strategy") or {}).get("params"),
        "is_sweep": is_sweep,
        "curve_len": len(curve),
        "curve_years": round(curve.years, 3),
        "frequency": resolved_frequency.name,
        "before": before,
        "after": after,
        "delta": delta,
        "_top_config": top,   # for --out-dir writer
        "_new_portfolio_metrics": new_metrics["portfolio"],
        "_new_benchmark_metrics": new_metrics["benchmark"],
        "_new_comparison": new_metrics["comparison"],
    }


def _print_report(report):
    if "error" in report:
        print(f"  [SKIP] {report['path']}: {report['error']}")
        return
    print(f"\n{report['strategy']} ({report['path']})")
    print(f"  curve: {report['curve_len']} points over {report['curve_years']}y, freq={report['frequency']}")
    print(f"  {'field':<25} {'before':>14} {'after':>14} {'delta':>14}")
    for k in RECOMPUTED_FIELDS:
        b = report["before"].get(k)
        a = report["after"].get(k)
        d = report["delta"].get(k)
        def _fmt(x):
            if x is None:
                return "None"
            return f"{x:.6f}" if abs(x) < 1000 else f"{x:.2f}"
        print(f"  {k:<25} {_fmt(b):>14} {_fmt(a):>14} {_fmt(d):>14}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", nargs="?", help="Path to result.json")
    p.add_argument("--dir", help="Recompute every result.json in directory (recursive)")
    p.add_argument("--frequency", default=None,
                   choices=[f.name for f in Frequency],
                   help="Override auto-detection of curve frequency")
    p.add_argument("--json", action="store_true", help="Emit JSON report instead of table")
    p.add_argument("--out-dir", help="Write migrated v2 result files to this directory "
                                     "(mirrors source tree structure). Original files untouched.")
    args = p.parse_args()

    freq = Frequency[args.frequency] if args.frequency else None
    reports = []
    if args.dir:
        root = Path(args.dir)
        for path in sorted(root.rglob("*.json")):
            if "_archive" in str(path) or "catalog" in path.name:
                continue
            reports.append(recompute_one(path, frequency=freq))
    elif args.path:
        reports.append(recompute_one(args.path, frequency=freq))
    else:
        p.print_help()
        return 1

    # Optional: write migrated v2 files
    if args.out_dir:
        src_root = Path(args.dir) if args.dir else Path(args.path).parent
        out_root = Path(args.out_dir)
        written = 0
        for r in reports:
            if "error" in r or "_new_portfolio_metrics" not in r:
                continue
            rel = Path(r["path"]).relative_to(src_root) if args.dir else Path(r["path"]).name
            out_path = out_root / rel
            report_slim = {k: v for k, v in r.items() if not k.startswith("_")}
            _write_migrated(
                r["path"], str(out_path), r["_top_config"],
                r["_new_portfolio_metrics"], r["_new_benchmark_metrics"],
                r["_new_comparison"], r["frequency"], report_slim,
            )
            written += 1
        print(f"\nWrote {written} migrated files to {out_root}")

    # Strip private fields before reporting
    for r in reports:
        for k in list(r.keys()):
            if k.startswith("_"):
                del r[k]

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for r in reports:
            _print_report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())

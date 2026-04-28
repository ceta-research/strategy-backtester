"""Audit drill runner — Phase 2e of inspection drill (2026-04-28).

Runs champion configs with `audit_mode=True`, captures the audit collector
populated by Phase 2b/2c hooks, and emits parquet artifacts under
``results/<strategy>/audit_drill_<UTC-iso>/`` along with a human-readable
README + machine-readable run_metadata.json.

Notes on schema:
- The strict ENTRY_AUDIT_SCHEMA / TRADE_LOG_SCHEMA / DAILY_SNAPSHOT_SCHEMA
  in ``lib/audit_io.py`` expect post-enrichment fields (e.g. ``regime_state``,
  ``ds_at_entry``, ``quantity``, ``hold_days``) that the raw hook output
  doesn't carry directly. This runner emits the raw collector DataFrames as
  parquet (with strategy/config_id tagging), so Phase 3 inspection can
  consume them without round-tripping a schema migration. Where the strict
  ``audit_io`` writers DO match (``compute_filter_marginals``,
  ``build_daily_snapshot``), they are used.

Usage:
    python3 scripts/run_audit_drill.py --strategy eod_breakout
    python3 scripts/run_audit_drill.py --strategy eod_technical
    python3 scripts/run_audit_drill.py --all          # both champions
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402

from engine import pipeline  # noqa: E402
from engine.signals import eod_breakout, eod_technical  # noqa: E402
from lib import audit_io  # noqa: E402


CHAMPIONS = {
    "eod_breakout": {
        "config": os.path.join(
            REPO_ROOT, "strategies", "eod_breakout", "config_champion.yaml"
        ),
        "baseline": os.path.join(
            REPO_ROOT, "results", "eod_breakout",
            "champion_pre_audit_baseline.json",
        ),
        "module": eod_breakout,
        "class_name": "EodBreakoutSignalGenerator",
    },
    "eod_technical": {
        "config": os.path.join(
            REPO_ROOT, "strategies", "eod_technical", "config_champion.yaml"
        ),
        "baseline": os.path.join(
            REPO_ROOT, "results", "eod_technical",
            "champion_pre_audit_baseline.json",
        ),
        "module": eod_technical,
        "class_name": "EodTechnicalSignalGenerator",
    },
}


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", REPO_ROOT, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_with_audit(strategy: str, config_path: str):
    """Run champion config with audit_mode=True; return (sweep, collector)."""
    spec = CHAMPIONS[strategy]
    gen_class = getattr(spec["module"], spec["class_name"])
    original = gen_class.generate_orders
    captured = {}

    def patched(self, context, df_tick_data):
        context = dict(context)
        context["audit_mode"] = True
        context["audit_collector"] = {}
        captured["c"] = context["audit_collector"]
        return original(self, context, df_tick_data)

    gen_class.generate_orders = patched
    try:
        sweep = pipeline.run_pipeline(config_path)
    finally:
        gen_class.generate_orders = original

    return sweep, captured.get("c", {})


def _concat_dfs(dfs: list[pl.DataFrame]) -> pl.DataFrame:
    """Concat a list of DataFrames; return empty DF if list is empty."""
    if not dfs:
        return pl.DataFrame()
    if len(dfs) == 1:
        return dfs[0]
    return pl.concat(dfs, how="vertical_relaxed")


def _emit_entry_audit(strategy: str, config_id: str, collector: dict,
                       out_dir: str) -> tuple[Optional[str], int]:
    """Concat entry_audits collector list, tag, write parquet."""
    audits = collector.get("entry_audits", [])
    df = _concat_dfs(audits)
    if df.is_empty():
        return None, 0
    df = df.with_columns([
        pl.lit(strategy).alias("strategy"),
        pl.lit(config_id).alias("config_id"),
    ])
    df = audit_io.add_period_column(df)
    path = os.path.join(out_dir, "entry_audit.parquet")
    df.write_parquet(path, compression="zstd")
    return path, df.height


def _emit_trade_log_audit(strategy: str, config_id: str, collector: dict,
                           out_dir: str) -> tuple[Optional[str], int]:
    """Convert trade_log_audits (list of dicts) to DataFrame, write parquet."""
    rows = collector.get("trade_log_audits", [])
    if not rows:
        return None, 0
    df = pl.DataFrame(rows)
    df = df.with_columns([
        pl.lit(strategy).alias("strategy"),
        pl.lit(config_id).alias("config_id"),
    ])
    df = audit_io.add_period_column(df, epoch_col="entry_epoch")
    path = os.path.join(out_dir, "trade_log_audit.parquet")
    df.write_parquet(path, compression="zstd")
    return path, df.height


def _emit_scanner_summary(strategy: str, config_id: str, collector: dict,
                           out_dir: str) -> tuple[Optional[str], int]:
    """Concat scanner reject/snapshot frames, write parquet.

    eod_breakout emits ``scanner_snapshots`` (per-row); eod_technical emits
    ``scanner_reject_summaries`` (per-day aggregate). Both are useful;
    handle either.
    """
    if "scanner_reject_summaries" in collector:
        df = _concat_dfs(collector["scanner_reject_summaries"])
        path = os.path.join(out_dir, "scanner_reject_summary.parquet")
    elif "scanner_snapshots" in collector:
        df = _concat_dfs(collector["scanner_snapshots"])
        path = os.path.join(out_dir, "scanner_snapshot.parquet")
    else:
        return None, 0
    if df.is_empty():
        return None, 0
    df = df.with_columns([
        pl.lit(strategy).alias("strategy"),
        pl.lit(config_id).alias("config_id"),
    ])
    df = audit_io.add_period_column(df)
    df.write_parquet(path, compression="zstd")
    return path, df.height


def _emit_filter_marginals(strategy: str, config_id: str,
                             entry_audit_path: Optional[str],
                             out_dir: str) -> tuple[Optional[str], int]:
    """Use audit_io.compute_filter_marginals if entry_audit was written.

    Note: the helper expects clause names like clause_close_gt_ma,
    clause_ds_gt_thr, etc. — our hook output uses the same names. Any clause
    not present in the entry_audit (e.g. clause_regime_pass for eod_t) is
    skipped by the helper.
    """
    if entry_audit_path is None:
        return None, 0
    df_ea = pl.read_parquet(entry_audit_path)
    # Use whichever clause cols actually exist; pass them explicitly.
    clause_cols = [c for c in df_ea.columns if c.startswith("clause_")]
    if not clause_cols or "all_clauses_pass" not in df_ea.columns:
        return None, 0
    df_marg = audit_io.compute_filter_marginals(
        df_ea, strategy=strategy, config_id=config_id, clause_columns=clause_cols
    )
    path = os.path.join(out_dir, "filter_marginals.parquet")
    df_marg.write_parquet(path, compression="zstd")
    return path, df_marg.height


def _emit_simulator_artifacts(sweep, strategy: str, config_id: str,
                                out_dir: str) -> tuple[
    tuple[Optional[str], int],
    tuple[Optional[str], int],
]:
    """Emit simulator-side trade_log + equity_curve parquets.

    The audit collector's trade_log_audits captures the *unconstrained*
    pre-simulator candidate set. The simulator post-applies capacity caps
    (max_positions etc.) and produces the actual fills/equity. Both views
    are useful: the audit one for entry-decision inspection, the simulator
    one for actual portfolio analysis.
    """
    if sweep.configs:
        _, result = sweep.configs[0]
        detailed = result.to_dict()
    else:
        detailed = {}
    trades = detailed.get("trades", [])
    eq = detailed.get("equity_curve", [])

    sim_trade_path = None
    sim_trade_rows = 0
    if trades:
        df_t = pl.DataFrame(trades).with_columns([
            pl.lit(strategy).alias("strategy"),
            pl.lit(config_id).alias("config_id"),
        ])
        # Best-effort partition tag (entry_epoch column may or may not exist).
        if "entry_epoch" in df_t.columns:
            df_t = audit_io.add_period_column(df_t, epoch_col="entry_epoch")
        sim_trade_path = os.path.join(out_dir, "simulator_trade_log.parquet")
        df_t.write_parquet(sim_trade_path, compression="zstd")
        sim_trade_rows = df_t.height

    eq_path = None
    eq_rows = 0
    if eq:
        df_eq = pl.DataFrame(eq).with_columns([
            pl.lit(strategy).alias("strategy"),
            pl.lit(config_id).alias("config_id"),
        ])
        # equity_curve dicts use 'epoch' typically; tag period if so.
        epoch_col = next(
            (c for c in ("epoch", "date_epoch") if c in df_eq.columns), None
        )
        if epoch_col:
            df_eq = audit_io.add_period_column(df_eq, epoch_col=epoch_col)
        eq_path = os.path.join(out_dir, "equity_curve.parquet")
        df_eq.write_parquet(eq_path, compression="zstd")
        eq_rows = df_eq.height

    return (sim_trade_path, sim_trade_rows), (eq_path, eq_rows)


def _summary_metrics(sweep) -> dict:
    if not sweep.configs:
        return {}
    _, result = sweep.configs[0]
    detailed = result.to_dict()
    s = detailed.get("summary", {})
    return {
        "cagr": s.get("cagr"),
        "max_drawdown": s.get("max_drawdown"),
        "sharpe_ratio": s.get("sharpe_ratio"),
        "calmar_ratio": s.get("calmar_ratio"),
        "total_trades": len(detailed.get("trades", [])),
    }


def run_one(strategy: str) -> dict:
    """Run one champion audit; return summary dict for printing/aggregation."""
    spec = CHAMPIONS[strategy]
    config_path = spec["config"]
    baseline_path = spec["baseline"]

    print(f"\n=== Audit drill: {strategy} ===")
    print(f"  config: {config_path}")
    print(f"  baseline: {baseline_path}")

    started = _now_iso()
    t0 = time.time()
    sweep, collector = _run_with_audit(strategy, config_path)
    elapsed = round(time.time() - t0, 1)
    print(f"  Pipeline+audit run: {elapsed}s")

    config_id = "champion"  # one config per champion run; tag uniformly
    out_dir = audit_io.make_audit_dir(strategy, base_dir=os.path.join(REPO_ROOT, "results"))
    os.makedirs(out_dir, exist_ok=True)

    ea_path, ea_rows = _emit_entry_audit(strategy, config_id, collector, out_dir)
    tla_path, tla_rows = _emit_trade_log_audit(strategy, config_id, collector, out_dir)
    sc_path, sc_rows = _emit_scanner_summary(strategy, config_id, collector, out_dir)
    fm_path, fm_rows = _emit_filter_marginals(strategy, config_id, ea_path, out_dir)
    (sim_t_path, sim_t_rows), (eq_path, eq_rows) = _emit_simulator_artifacts(
        sweep, strategy, config_id, out_dir
    )

    metrics = _summary_metrics(sweep)
    completed = _now_iso()

    metadata = audit_io.AuditRunMetadata(
        strategy=strategy,
        config_path=os.path.relpath(config_path, REPO_ROOT),
        config_id=config_id,
        engine_commit=_git_head(),
        run_started_utc=started,
        run_completed_utc=completed,
        pre_audit_baseline_path=os.path.relpath(baseline_path, REPO_ROOT),
        cagr=metrics.get("cagr"),
        max_drawdown=metrics.get("max_drawdown"),
        sharpe_ratio=metrics.get("sharpe_ratio"),
        calmar_ratio=metrics.get("calmar_ratio"),
        total_trades=metrics.get("total_trades"),
        entry_audit_rows=ea_rows,
        trade_log_rows=tla_rows,
        daily_snapshot_rows=None,  # not built in this minimal runner
        scanner_reject_summary_rows=sc_rows,
        filter_marginals_rows=fm_rows,
    )
    audit_io.write_audit_readme(out_dir, metadata)

    # `audit_io.write_audit_readme` hardcodes a generic artifact table that
    # always names the scanner artifact `scanner_reject_summary.parquet`.
    # The runner emits either `scanner_reject_summary.parquet` (eod_t) or
    # `scanner_snapshot.parquet` (eod_b) — different shapes. Append an
    # accurate file listing so a reader can identify what's actually on
    # disk without running `ls`.
    artifacts_for_readme = [
        ("entry_audit.parquet", ea_path, ea_rows),
        ("trade_log_audit.parquet", tla_path, tla_rows),
        (os.path.basename(sc_path) if sc_path else None, sc_path, sc_rows),
        ("filter_marginals.parquet", fm_path, fm_rows),
        ("simulator_trade_log.parquet", sim_t_path, sim_t_rows),
        ("equity_curve.parquet", eq_path, eq_rows),
    ]
    addendum = ["\n## Files actually written\n",
                "| File | Rows |\n", "|---|---:|\n"]
    for name, path, rows in artifacts_for_readme:
        if path is not None and name is not None:
            addendum.append(f"| `{name}` | {rows:,} |\n")
    addendum.append(
        "\nNote: the scanner artifact differs by strategy — "
        "`scanner_snapshot.parquet` (eod_breakout: per-row pass flag) vs "
        "`scanner_reject_summary.parquet` (eod_technical: per-day "
        "aggregate of rejects by clause).\n"
    )
    readme_path = os.path.join(out_dir, "README.md")
    with open(readme_path, "a") as f:
        f.write("".join(addendum))

    summary = {
        "strategy": strategy,
        "out_dir": os.path.relpath(out_dir, REPO_ROOT),
        "elapsed_s": elapsed,
        "metrics": metrics,
        "artifacts": {
            "entry_audit": (ea_path, ea_rows),
            "trade_log_audit": (tla_path, tla_rows),
            "scanner_summary": (sc_path, sc_rows),
            "filter_marginals": (fm_path, fm_rows),
            "simulator_trade_log": (sim_t_path, sim_t_rows),
            "equity_curve": (eq_path, eq_rows),
        },
    }
    print(f"  Output dir: {out_dir}")
    for name, (path, rows) in summary["artifacts"].items():
        if path is not None:
            rel = os.path.relpath(path, out_dir)
            print(f"    {rel}: {rows:,} rows")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run audit drill")
    parser.add_argument(
        "--strategy", choices=list(CHAMPIONS.keys()),
        help="Single strategy to audit",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run all champions (eod_breakout + eod_technical)",
    )
    args = parser.parse_args()

    if not args.all and not args.strategy:
        parser.print_help()
        sys.exit(1)

    targets = list(CHAMPIONS.keys()) if args.all else [args.strategy]

    summaries = []
    for s in targets:
        summaries.append(run_one(s))

    print("\n=== AUDIT DRILL DONE ===")
    for s in summaries:
        print(f"  {s['strategy']}: {s['out_dir']} ({s['elapsed_s']}s)")


if __name__ == "__main__":
    main()

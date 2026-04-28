"""Audit IO: schemas, writers, and post-processing helpers for the pipeline
inspection drill (started 2026-04-28 pt5).

This module is the SHARED writer layer for per-strategy audit collectors.
Collectors (in engine/signals/eod_breakout.py and the eod_technical legacy
path) emit decision-frame data; this module owns the schema validation,
parquet emission, and post-run enrichment helpers.

Design rules:
- Schemas are explicit dicts of {column: polars_dtype}. Writers validate
  before emit; failures raise loudly. No silent column drops.
- Writers are pure functions: take a DataFrame, return None (after writing).
- Post-processing helpers (enrichment, daily-snapshot construction, filter
  marginals) operate on already-emitted parquets so they can run offline.
- This module has no side effects on engine behavior. Importing it does not
  change anything; it's only invoked when audit_mode is on.

NOT protected. Sibling to lib/ensemble_curve.py / lib/equity_curve.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import polars as pl


# ---------------------------------------------------------------------------
# IS / OOS boundary (mandatory in every Phase 3 query per pt5 plan section 3d)
# ---------------------------------------------------------------------------

# eod_b champion was selected on a 2010-2024 holdout train; 2025+ is OOS.
# We use the same boundary for eod_t to keep the two strategies' queries
# uniformly partitioned, even though eod_t's champion was not holdout-selected.
IS_OOS_BOUNDARY_EPOCH = 1735689600  # 2025-01-01 UTC


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

ENTRY_AUDIT_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8,
    "config_id": pl.Utf8,
    "instrument": pl.Utf8,
    "date_epoch": pl.Int64,
    # Scanner outcome:
    "scanner_pass": pl.Boolean,
    # Per-clause flags (entry filter, all bool):
    "clause_close_gt_ma": pl.Boolean,
    "clause_close_ge_ndhigh": pl.Boolean,
    "clause_close_gt_open": pl.Boolean,
    "clause_ds_gt_thr": pl.Boolean,
    "clause_regime_pass": pl.Boolean,   # true when gate is disabled
    "clause_vol_pass": pl.Boolean,      # true when filter is disabled (eod_t always)
    "clause_next_data": pl.Boolean,
    "all_clauses_pass": pl.Boolean,
    # Indicator + at-entry context:
    "close": pl.Float64,
    "open": pl.Float64,
    "n_day_ma": pl.Float64,
    "n_day_high": pl.Float64,
    "direction_score": pl.Float64,
    "regime_state": pl.Utf8,            # 'bull' | 'bear' | null (gate off)
    "trailing_vol_annual": pl.Float64,  # null when vol filter off
    # Outcome (populated by post-run enrichment via trade_log join):
    "final_picked": pl.Boolean,         # null until enriched
    # IS/OOS partition (populated by add_period_column):
    "period": pl.Utf8,
}

TRADE_LOG_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8,
    "config_id": pl.Utf8,
    "instrument": pl.Utf8,
    "entry_epoch": pl.Int64,
    "exit_epoch": pl.Int64,
    "entry_date": pl.Utf8,              # ISO YYYY-MM-DD (UTC)
    "exit_date": pl.Utf8,
    "entry_price": pl.Float64,
    "exit_price": pl.Float64,
    "quantity": pl.Int64,
    "hold_days": pl.Int64,
    "pnl_pct": pl.Float64,              # (exit/entry - 1) * 100
    "pnl_inr": pl.Float64,              # (exit - entry) * quantity (excl charges)
    "charges": pl.Float64,
    "exit_reason": pl.Utf8,
    "max_runup_during_hold": pl.Float64,    # (max_close / entry_price - 1) * 100
    "max_drawdown_during_hold": pl.Float64, # (min_close_post_peak / max_close - 1) * 100
    # At-entry context (from entry_audit at signal day):
    "regime_state_at_entry": pl.Utf8,
    "ds_at_entry": pl.Float64,
    "n_day_high_at_entry": pl.Float64,
    "close_at_signal": pl.Float64,
    "trailing_vol_at_entry": pl.Float64,
    "period": pl.Utf8,
}

DAILY_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8,
    "config_id": pl.Utf8,
    "date_epoch": pl.Int64,
    "date": pl.Utf8,
    "nav": pl.Float64,
    "total_position_value": pl.Float64,
    "margin_available": pl.Float64,
    "cash_pct": pl.Float64,
    "open_positions": pl.Int32,
    "signals_seen": pl.Int32,
    "entries_taken": pl.Int32,
    "exits_taken": pl.Int32,
    "exits_by_trailing_stop": pl.Int32,
    "exits_by_anomalous_drop": pl.Int32,
    "exits_by_end_of_data": pl.Int32,
    "exits_by_regime_flip": pl.Int32,
    "exits_by_natural": pl.Int32,
    "drawdown_from_peak": pl.Float64,
    "period": pl.Utf8,
}

SCANNER_REJECT_SUMMARY_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8,
    "config_id": pl.Utf8,
    "scanner_config_id": pl.Utf8,
    "date_epoch": pl.Int64,
    "date": pl.Utf8,
    "universe_size_pre_filter": pl.Int32,    # rows after fill_missing + exchange filter, pre-clause
    "rejected_by_drop_nulls": pl.Int32,      # rows lost to drop_nulls (rolling/shift NaNs)
    "rejected_by_price": pl.Int32,
    "rejected_by_avg_txn": pl.Int32,
    "rejected_by_n_day_gain": pl.Int32,      # 0 for eod_b (rule not in run_scanner)
    "final_pass_count": pl.Int32,
    "period": pl.Utf8,
}

FILTER_MARGINALS_SCHEMA: dict[str, pl.DataType] = {
    "strategy": pl.Utf8,
    "config_id": pl.Utf8,
    "clause_name": pl.Utf8,
    "total_rows": pl.Int64,                 # post-scanner candidate count
    "passed_alone": pl.Int64,               # P(clause passes), no other constraint
    "passed_in_combination": pl.Int64,      # P(clause passes AND all others pass)
    "pass_rate_alone": pl.Float64,
    "pass_rate_in_combination": pl.Float64,
    # Binding test: P(clause_i fails | all others pass).
    # Higher = more binding (clause is the actual constraint).
    # Computed as: (others_pass_count - in_combo_count) / others_pass_count.
    "conditional_fail_rate": pl.Float64,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class AuditSchemaError(ValueError):
    """Raised when a writer receives a DataFrame that doesn't match the
    expected schema. Surfaces hook bugs immediately rather than corrupting
    the audit dataset.
    """


def _validate_schema(df: pl.DataFrame, expected: dict[str, pl.DataType],
                     name: str) -> None:
    """Strict-but-helpful schema check. Allow extra columns (collector may
    surface extra debug data) but require all expected columns with
    compatible types.
    """
    actual = {c: df.schema[c] for c in df.columns}
    missing = [c for c in expected if c not in actual]
    if missing:
        raise AuditSchemaError(
            f"{name}: missing columns: {missing}. "
            f"Got: {sorted(actual.keys())}"
        )
    type_mismatches = []
    for c, dtype in expected.items():
        # polars dtype equality is precise; we accept Null-typed columns as
        # compatible with any expected dtype because empty collectors emit
        # all-null columns that get dtype=Null.
        if actual[c] != dtype and actual[c] != pl.Null:
            type_mismatches.append(f"{c}: expected {dtype}, got {actual[c]}")
    if type_mismatches:
        raise AuditSchemaError(
            f"{name}: dtype mismatches: {type_mismatches}"
        )


def _coerce_to_schema(df: pl.DataFrame, expected: dict[str, pl.DataType]) -> pl.DataFrame:
    """Cast each expected column to its declared dtype. Useful when
    collectors emit dtypes that are 'close enough' (e.g. Int32 for what we
    declared Int64). Strict in-place would force every collector to be
    pixel-perfect.
    """
    casts = []
    for col, dtype in expected.items():
        if col in df.columns and df.schema[col] != dtype and df.schema[col] != pl.Null:
            casts.append(pl.col(col).cast(dtype))
    if casts:
        df = df.with_columns(casts)
    # Reorder to schema order, keep any extras at the end.
    schema_cols = [c for c in expected if c in df.columns]
    extras = [c for c in df.columns if c not in expected]
    return df.select(schema_cols + extras)


# ---------------------------------------------------------------------------
# IS/OOS partitioning
# ---------------------------------------------------------------------------

def add_period_column(df: pl.DataFrame, epoch_col: str = "date_epoch") -> pl.DataFrame:
    """Add 'period' column ('IS' or 'OOS') based on IS_OOS_BOUNDARY_EPOCH.
    Idempotent — safe to call on already-tagged DataFrames.
    No-op if the source epoch column is missing (schema validator will then
    surface the real problem).
    """
    if "period" in df.columns:
        return df
    if epoch_col not in df.columns:
        return df
    return df.with_columns(
        pl.when(pl.col(epoch_col) < IS_OOS_BOUNDARY_EPOCH)
        .then(pl.lit("IS"))
        .otherwise(pl.lit("OOS"))
        .alias("period")
    )


# ---------------------------------------------------------------------------
# Writers (one per schema)
# ---------------------------------------------------------------------------

def _ensure_dir(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)


def write_entry_audit(df: pl.DataFrame, out_dir: str) -> str:
    """Write per-(date, instrument) entry-decision audit. Returns path."""
    _ensure_dir(out_dir)
    df = add_period_column(df)
    df = _coerce_to_schema(df, ENTRY_AUDIT_SCHEMA)
    _validate_schema(df, ENTRY_AUDIT_SCHEMA, "entry_audit")
    path = os.path.join(out_dir, "entry_audit.parquet")
    df.write_parquet(path, compression="zstd")
    return path


def write_trade_log(df: pl.DataFrame, out_dir: str) -> str:
    """Write per-trade log with at-entry context + exit_reason."""
    _ensure_dir(out_dir)
    df = add_period_column(df, epoch_col="entry_epoch")
    df = _coerce_to_schema(df, TRADE_LOG_SCHEMA)
    _validate_schema(df, TRADE_LOG_SCHEMA, "trade_log")
    path = os.path.join(out_dir, "trade_log.parquet")
    df.write_parquet(path, compression="zstd")
    return path


def write_daily_snapshot(df: pl.DataFrame, out_dir: str) -> str:
    """Write per-simulation-date portfolio snapshot."""
    _ensure_dir(out_dir)
    df = add_period_column(df)
    df = _coerce_to_schema(df, DAILY_SNAPSHOT_SCHEMA)
    _validate_schema(df, DAILY_SNAPSHOT_SCHEMA, "daily_snapshot")
    path = os.path.join(out_dir, "daily_snapshot.parquet")
    df.write_parquet(path, compression="zstd")
    return path


def write_scanner_reject_summary(df: pl.DataFrame, out_dir: str) -> str:
    """Write per-(date, scanner_config) rejection counts by clause."""
    _ensure_dir(out_dir)
    df = add_period_column(df)
    df = _coerce_to_schema(df, SCANNER_REJECT_SUMMARY_SCHEMA)
    _validate_schema(df, SCANNER_REJECT_SUMMARY_SCHEMA, "scanner_reject_summary")
    path = os.path.join(out_dir, "scanner_reject_summary.parquet")
    df.write_parquet(path, compression="zstd")
    return path


def write_filter_marginals(df: pl.DataFrame, out_dir: str) -> str:
    """Write per-clause marginals (pass-rate alone, in combination, conditional
    fail-rate). One row per clause, not per (date, instrument).
    """
    _ensure_dir(out_dir)
    df = _coerce_to_schema(df, FILTER_MARGINALS_SCHEMA)
    _validate_schema(df, FILTER_MARGINALS_SCHEMA, "filter_marginals")
    path = os.path.join(out_dir, "filter_marginals.parquet")
    df.write_parquet(path, compression="zstd")
    return path


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def compute_filter_marginals(
    entry_audit: pl.DataFrame,
    strategy: str,
    config_id: str,
    clause_columns: Optional[list[str]] = None,
) -> pl.DataFrame:
    """Build filter_marginals DataFrame from entry_audit.

    For each clause column (boolean), compute:
        passed_alone           = sum(clause)
        passed_in_combination  = sum(clause AND all_clauses_pass)
        conditional_fail_rate  = P(clause fails | all OTHERS pass)
                               = (others_pass_count - in_combo_count) / others_pass_count

    The conditional_fail_rate is the binding-clause measure: high values mean
    this clause is the actual constraint when others have passed.
    """
    if clause_columns is None:
        clause_columns = [
            "clause_close_gt_ma",
            "clause_close_ge_ndhigh",
            "clause_close_gt_open",
            "clause_ds_gt_thr",
            "clause_regime_pass",
            "clause_vol_pass",
            "clause_next_data",
        ]

    total = entry_audit.height
    rows = []
    for clause in clause_columns:
        if clause not in entry_audit.columns:
            continue

        passed_alone = int(entry_audit.filter(pl.col(clause)).height)

        # passed_in_combination: clause passes AND all_clauses_pass
        in_combo = int(
            entry_audit.filter(pl.col(clause) & pl.col("all_clauses_pass")).height
        )

        # P(clause fails | all OTHERS pass). "All others pass" =
        # all_clauses_pass would be true if this clause were ignored.
        # Equivalent: (every other clause passes). Approximate as:
        #   others_pass = (all_clauses_pass) OR (~clause AND <all_others_pass>)
        # but the simpler observable proxy is:
        #   others_pass_count = #rows where all_clauses_pass = True for this
        #   row OR all clauses except this one are True.
        # We use a cheap upper bound: others_pass ≈ sum where (all_clauses_pass
        # | (~clause & every_other_clause_pass)). Computing every_other_clause
        # exactly requires re-evaluating the AND minus this clause — do it.
        other_clauses = [c for c in clause_columns if c != clause and c in entry_audit.columns]
        if other_clauses:
            others_expr = pl.lit(True)
            for c in other_clauses:
                others_expr = others_expr & pl.col(c)
            others_pass_count = int(entry_audit.filter(others_expr).height)
        else:
            others_pass_count = total

        cond_fail = (
            (others_pass_count - in_combo) / others_pass_count
            if others_pass_count > 0 else 0.0
        )

        rows.append({
            "strategy": strategy,
            "config_id": config_id,
            "clause_name": clause,
            "total_rows": total,
            "passed_alone": passed_alone,
            "passed_in_combination": in_combo,
            "pass_rate_alone": passed_alone / total if total > 0 else 0.0,
            "pass_rate_in_combination": in_combo / total if total > 0 else 0.0,
            "conditional_fail_rate": cond_fail,
        })

    return pl.DataFrame(rows, schema=FILTER_MARGINALS_SCHEMA)


def enrich_entry_audit_with_picked(
    entry_audit: pl.DataFrame,
    trade_log: pl.DataFrame,
) -> pl.DataFrame:
    """Set `final_picked` on entry_audit rows that match a (instrument,
    signal_date) actually entered in trade_log.

    Note: trade_log.entry_epoch is the *fill* epoch (next day open), while
    entry_audit.date_epoch is the *signal* epoch. They differ by ~1 trading
    day. We join on (instrument, entry_audit.date_epoch == trade_log_signal_epoch)
    where trade_log_signal_epoch is the latest entry_audit row in (entry_epoch - 7d, entry_epoch).
    Simpler approach: stamp entry_audit rows where
    `instrument` matches AND `date_epoch < entry_epoch <= date_epoch + 7d`.
    Even simpler: join on instrument AND `date_epoch == entry_epoch - 1*86400`
    works for non-weekend cases. We use the looser approach (within 7d) to
    handle weekends/holidays and missing-bar cases.
    """
    if trade_log.is_empty() or entry_audit.is_empty():
        return entry_audit.with_columns(pl.lit(False).alias("final_picked"))

    # Build set of (instrument, signal_epoch) pairs from trade_log.
    # The signal_epoch is the last bar where the entry condition fired,
    # which is entry_audit's date_epoch. Trade_log records entry_epoch =
    # next_open_epoch. We approximate the signal epoch as the largest
    # entry_audit.date_epoch < trade_log.entry_epoch, per (instrument).
    trade_keys = trade_log.select(["instrument", "entry_epoch"]).unique()

    # Cross-tag candidates within 7d before each trade entry_epoch:
    candidates = entry_audit.join(
        trade_keys.rename({"entry_epoch": "_trade_entry_epoch"}),
        on="instrument",
        how="inner",
    ).filter(
        (pl.col("date_epoch") < pl.col("_trade_entry_epoch"))
        & (pl.col("date_epoch") >= pl.col("_trade_entry_epoch") - 7 * 86400)
    )

    # Pick the latest candidate per (instrument, _trade_entry_epoch) — that's
    # the actual signal day for the trade.
    picked = (
        candidates.sort("date_epoch", descending=True)
        .group_by(["instrument", "_trade_entry_epoch"], maintain_order=False)
        .first()
        .select(["instrument", "date_epoch"])
        .with_columns(pl.lit(True).alias("_is_picked"))
    )

    enriched = entry_audit.join(
        picked, on=["instrument", "date_epoch"], how="left"
    ).with_columns(
        pl.col("_is_picked").fill_null(False).alias("final_picked")
    ).drop("_is_picked")

    return enriched


def build_daily_snapshot(
    trade_log: pl.DataFrame,
    equity_curve: list[tuple[int, float]],
    starting_capital: float,
    entry_audit: Optional[pl.DataFrame] = None,
    strategy: str = "",
    config_id: str = "",
) -> pl.DataFrame:
    """Construct daily_snapshot.parquet content from trade_log + equity curve.

    Args:
        trade_log: per-trade DataFrame (TRADE_LOG_SCHEMA-compatible).
        equity_curve: list of (epoch, nav) from BacktestResult.
        starting_capital: initial margin (for cash_pct calc).
        entry_audit: optional, used to count `signals_seen` per day.
        strategy: tag for output rows.
        config_id: tag for output rows.

    Returns:
        DataFrame matching DAILY_SNAPSHOT_SCHEMA.

    Note: `total_position_value` and `margin_available` aren't directly
    available from BacktestResult.equity_curve (which is nav only). We
    approximate `open_positions` from trade_log (count of trades with
    entry <= date < exit). `cash_pct` is computed as
    (nav - total_invested) / nav using a per-day reconstruction of
    invested_value from trade_log (entry_price * qty for currently open
    positions).
    """
    if not equity_curve:
        return pl.DataFrame(schema=DAILY_SNAPSHOT_SCHEMA)

    epochs = [e for e, _ in equity_curve]
    navs = [v for _, v in equity_curve]

    # Per-day open-position count + invested_value reconstruction.
    # For each equity-curve epoch, count trades where
    # entry_epoch <= epoch < exit_epoch.
    rows = []
    if not trade_log.is_empty():
        tl_entry = trade_log["entry_epoch"].to_list()
        tl_exit = trade_log["exit_epoch"].to_list()
        tl_entry_price = trade_log["entry_price"].to_list()
        tl_qty = trade_log["quantity"].to_list()
        tl_exit_reason = trade_log["exit_reason"].to_list() if "exit_reason" in trade_log.columns else [""] * trade_log.height
    else:
        tl_entry = tl_exit = tl_entry_price = tl_qty = tl_exit_reason = []

    # Pre-index trades by entry-day and exit-day for O(N+T) snapshot build.
    entry_day_count: dict[int, int] = {}
    exits_day: dict[int, dict[str, int]] = {}
    for ent, ex, reason in zip(tl_entry, tl_exit, tl_exit_reason):
        entry_day_count[ent] = entry_day_count.get(ent, 0) + 1
        d = exits_day.setdefault(ex, {})
        d[reason] = d.get(reason, 0) + 1

    # signals_seen lookup from entry_audit (count of all_clauses_pass per day).
    signals_seen_by_day: dict[int, int] = {}
    if entry_audit is not None and not entry_audit.is_empty():
        seen = (
            entry_audit.filter(pl.col("all_clauses_pass"))
            .group_by("date_epoch")
            .agg(pl.len().alias("n"))
        )
        for d, n in zip(seen["date_epoch"].to_list(), seen["n"].to_list()):
            signals_seen_by_day[d] = n

    peak_nav = navs[0] if navs else starting_capital
    for epoch, nav in zip(epochs, navs):
        # Count open positions and invested_value at this epoch.
        open_pos = 0
        invested = 0.0
        for ent, ex, ep, q in zip(tl_entry, tl_exit, tl_entry_price, tl_qty):
            if ent <= epoch < ex:
                open_pos += 1
                invested += ep * q
        margin = nav - invested
        cash_pct = (margin / nav * 100) if nav > 0 else 0.0
        peak_nav = max(peak_nav, nav)
        dd = (nav / peak_nav - 1.0) * 100 if peak_nav > 0 else 0.0

        ex_today = exits_day.get(epoch, {})
        rows.append({
            "strategy": strategy,
            "config_id": config_id,
            "date_epoch": epoch,
            "date": _epoch_to_iso(epoch),
            "nav": nav,
            "total_position_value": invested,
            "margin_available": margin,
            "cash_pct": cash_pct,
            "open_positions": open_pos,
            "signals_seen": signals_seen_by_day.get(epoch, 0),
            "entries_taken": entry_day_count.get(epoch, 0),
            "exits_taken": sum(ex_today.values()),
            "exits_by_trailing_stop": ex_today.get("trailing_stop", 0),
            "exits_by_anomalous_drop": ex_today.get("anomalous_drop", 0),
            "exits_by_end_of_data": ex_today.get("end_of_data", 0),
            "exits_by_regime_flip": ex_today.get("regime_flip", 0),
            "exits_by_natural": ex_today.get("natural", 0),
            "drawdown_from_peak": dd,
        })

    return pl.DataFrame(rows, schema={k: v for k, v in DAILY_SNAPSHOT_SCHEMA.items()
                                       if k != "period"})


def _epoch_to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Run metadata + README
# ---------------------------------------------------------------------------

@dataclass
class AuditRunMetadata:
    strategy: str
    config_path: str
    config_id: str
    engine_commit: str
    run_started_utc: str
    run_completed_utc: str
    pre_audit_baseline_path: str  # for byte-identical reference
    cagr: Optional[float] = None
    max_drawdown: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None
    total_trades: Optional[int] = None
    entry_audit_rows: Optional[int] = None
    trade_log_rows: Optional[int] = None
    daily_snapshot_rows: Optional[int] = None
    scanner_reject_summary_rows: Optional[int] = None
    filter_marginals_rows: Optional[int] = None


def write_audit_readme(out_dir: str, metadata: AuditRunMetadata) -> str:
    """Write a human-readable README.md + machine-readable run_metadata.json
    inside the audit_drill_<ts>/ directory.
    """
    _ensure_dir(out_dir)

    md = metadata
    readme = f"""# Audit drill run — {md.strategy}

**Run completed:** {md.run_completed_utc}
**Engine commit:** `{md.engine_commit}`
**Config:** `{md.config_path}` (config_id: `{md.config_id}`)
**Pre-audit baseline reference:** `{md.pre_audit_baseline_path}`

## Run metrics

| Metric | Value |
|---|---|
| CAGR | {_pct(md.cagr)} |
| Max Drawdown | {_pct(md.max_drawdown)} |
| Sharpe (canonical) | {md.sharpe_ratio if md.sharpe_ratio is not None else 'n/a'} |
| Calmar | {md.calmar_ratio if md.calmar_ratio is not None else 'n/a'} |
| Total trades | {md.total_trades if md.total_trades is not None else 'n/a'} |

## Audit artifact row counts

| File | Rows |
|---|---|
| `entry_audit.parquet` | {md.entry_audit_rows if md.entry_audit_rows is not None else 'n/a'} |
| `trade_log.parquet` | {md.trade_log_rows if md.trade_log_rows is not None else 'n/a'} |
| `daily_snapshot.parquet` | {md.daily_snapshot_rows if md.daily_snapshot_rows is not None else 'n/a'} |
| `scanner_reject_summary.parquet` | {md.scanner_reject_summary_rows if md.scanner_reject_summary_rows is not None else 'n/a'} |
| `filter_marginals.parquet` | {md.filter_marginals_rows if md.filter_marginals_rows is not None else 'n/a'} |

## Schema reference

See `lib/audit_io.py` for full schema definitions:
- `ENTRY_AUDIT_SCHEMA` — per (date, instrument) post-scanner candidate row
  with per-clause flags + at-entry context. `final_picked` populated by
  `enrich_entry_audit_with_picked`.
- `TRADE_LOG_SCHEMA` — per actual trade with at-entry context and
  exit_reason taxonomy.
- `DAILY_SNAPSHOT_SCHEMA` — per simulation date: NAV, positions, exits
  by reason, drawdown.
- `SCANNER_REJECT_SUMMARY_SCHEMA` — per (date, scanner_config) reject
  counts by clause.
- `FILTER_MARGINALS_SCHEMA` — per clause: pass-rate alone, in
  combination, and conditional_fail_rate (binding-clause measure).

## IS / OOS partition

`period` column = 'IS' for `date_epoch < {IS_OOS_BOUNDARY_EPOCH}` (2025-01-01 UTC),
'OOS' otherwise. Use this in every Phase 3 query per the inspection-drill plan.
"""

    readme_path = os.path.join(out_dir, "README.md")
    with open(readme_path, "w") as f:
        f.write(readme)

    meta_path = os.path.join(out_dir, "run_metadata.json")
    with open(meta_path, "w") as f:
        json.dump(asdict(md), f, indent=2)

    return readme_path


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.2f}%"


# ---------------------------------------------------------------------------
# Output directory naming
# ---------------------------------------------------------------------------

def make_audit_dir(strategy: str, base_dir: str = "results") -> str:
    """Return canonical audit-output dir: results/<strategy>/audit_drill_<UTC-iso>/"""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(base_dir, strategy, f"audit_drill_{ts}")

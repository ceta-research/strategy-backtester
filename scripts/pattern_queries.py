"""Pattern queries against audit artifacts — Phase 3d (2026-04-28).

Ten descriptive queries per strategy, IS vs OOS split, run against
``results/<strategy>/audit_drill_*/``. Output is a markdown report
written to ``docs/inspection/PATTERNS_<strategy>.md``.

Queries:
  Q1. Top-10 instruments by simulator trade count.
  Q2. Top-10 instruments by net pnl contribution (sim).
  Q3. Day-of-week distribution of entries (audit + sim).
  Q4. Hold-time distribution by exit_reason (sim).
  Q5. Direction-score histogram at entry (audit, all_clauses_pass).
  Q6. Regime-bullish split at entry (audit; eod_b only).
  Q7. Scanner-pass count by month (eod_b: per-row True; eod_t: pass_count).
  Q8. Trade-pnl distribution by exit_reason (sim).
  Q9. Year-over-year trade volume + hit rate (sim).
  Q10. Capacity-blocked: count + top-10 instruments most often blocked.

Run:
    python3 scripts/pattern_queries.py \
        --eod-breakout-dir results/eod_breakout/audit_drill_20260428T124754Z \
        --eod-technical-dir results/eod_technical/audit_drill_20260428T124832Z
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402


def _md_table(df: pl.DataFrame, max_rows: int = 50) -> str:
    if df.is_empty():
        return "_(empty)_"
    df = df.head(max_rows)
    headers = list(df.columns)
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in df.iter_rows(named=True):
        cells = []
        for h in headers:
            v = row[h]
            if isinstance(v, float):
                cells.append(f"{v:.4f}")
            else:
                cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _add_period_filter(df: pl.DataFrame, period: str) -> pl.DataFrame:
    return df.filter(pl.col("period") == period)


def _to_dt_expr(epoch_col: str = "entry_epoch") -> pl.Expr:
    return pl.from_epoch(pl.col(epoch_col), time_unit="s")


def q1_top_by_trades(sim: pl.DataFrame) -> pl.DataFrame:
    return (
        sim.group_by(["period", "symbol"])
           .agg(pl.len().alias("trades"))
           .sort(["period", "trades"], descending=[False, True])
           .group_by("period", maintain_order=True)
           .head(10)
    )


def q2_top_by_pnl(sim: pl.DataFrame) -> pl.DataFrame:
    return (
        sim.group_by(["period", "symbol"])
           .agg([pl.col("net_pnl").sum().alias("net_pnl_sum"),
                 pl.len().alias("trades")])
           .sort(["period", "net_pnl_sum"], descending=[False, True])
           .group_by("period", maintain_order=True)
           .head(10)
    )


def q3_dow(audit_traded: pl.DataFrame, sim: pl.DataFrame) -> pl.DataFrame:
    a = audit_traded.with_columns(
        _to_dt_expr().dt.weekday().alias("dow")
    ).group_by(["period", "dow"]).agg(pl.len().alias("audit_count"))
    s = sim.with_columns(
        _to_dt_expr().dt.weekday().alias("dow")
    ).group_by(["period", "dow"]).agg(pl.len().alias("sim_count"))
    return a.join(s, on=["period", "dow"], how="full", coalesce=True).sort(["period", "dow"])


def q4_holdtime_by_exit(sim: pl.DataFrame) -> pl.DataFrame:
    return (
        sim.group_by(["period", "exit_reason"])
           .agg([
               pl.len().alias("trades"),
               pl.col("hold_days").mean().alias("hold_days_mean"),
               pl.col("hold_days").quantile(0.5).alias("hold_days_p50"),
               pl.col("hold_days").quantile(0.9).alias("hold_days_p90"),
               pl.col("hold_days").max().alias("hold_days_max"),
           ])
           .sort(["period", "exit_reason"])
    )


def q5_direction_score_hist(audit: pl.DataFrame) -> pl.DataFrame:
    a = audit.with_columns(
        ((pl.col("entry_direction_score") * 10).floor() / 10.0)
        .alias("ds_bucket")
    )
    return (
        a.group_by(["period", "ds_bucket"])
         .agg(pl.len().alias("audit_entries"))
         .sort(["period", "ds_bucket"])
    )


def q6_regime_split(audit: pl.DataFrame) -> pl.DataFrame:
    if "entry_regime_bullish" not in audit.columns:
        return pl.DataFrame()
    return (
        audit.group_by(["period", "entry_regime_bullish"])
             .agg(pl.len().alias("audit_entries"))
             .sort(["period", "entry_regime_bullish"])
    )


def q7_scanner_by_month_eod_b(snap: pl.DataFrame) -> pl.DataFrame:
    return (
        snap.with_columns(
            pl.from_epoch(pl.col("date_epoch"), time_unit="s")
              .dt.strftime("%Y-%m").alias("year_month"),
            pl.col("scanner_pass").cast(pl.Int64).alias("pass_int"),
        )
        .group_by(["period", "year_month"])
        .agg([
            pl.len().alias("rows"),
            pl.col("pass_int").sum().alias("scanner_passes"),
        ])
        .with_columns(
            (pl.col("scanner_passes") / pl.col("rows")).alias("pass_rate")
        )
        .sort(["period", "year_month"])
    )


def q7_scanner_by_month_eod_t(rs: pl.DataFrame) -> pl.DataFrame:
    return (
        rs.with_columns(
            pl.from_epoch(pl.col("date_epoch"), time_unit="s")
              .dt.strftime("%Y-%m").alias("year_month"),
        )
        .group_by(["period", "year_month"])
        .agg([
            pl.col("candidate_count").sum().alias("candidates"),
            pl.col("pass_count").sum().alias("passes"),
        ])
        .with_columns(
            (pl.col("passes") / pl.col("candidates")).alias("pass_rate")
        )
        .sort(["period", "year_month"])
    )


def q8_pnl_by_exit(sim: pl.DataFrame) -> pl.DataFrame:
    return (
        sim.group_by(["period", "exit_reason"])
           .agg([
               pl.len().alias("trades"),
               pl.col("pnl_pct").mean().alias("mean_pnl_pct"),
               pl.col("pnl_pct").quantile(0.5).alias("p50_pnl_pct"),
               (pl.col("pnl_pct") > 0).cast(pl.Float64).mean().alias("hit_rate"),
               pl.col("net_pnl").sum().alias("total_net_pnl"),
           ])
           .sort(["period", "exit_reason"])
    )


def q9_yearly(sim: pl.DataFrame) -> pl.DataFrame:
    return (
        sim.with_columns(
            _to_dt_expr().dt.year().alias("year")
        )
        .group_by(["period", "year"])
        .agg([
            pl.len().alias("trades"),
            (pl.col("pnl_pct") > 0).cast(pl.Float64).mean().alias("hit_rate"),
            pl.col("pnl_pct").mean().alias("mean_pnl_pct"),
            pl.col("net_pnl").sum().alias("total_net_pnl"),
        ])
        .sort(["period", "year"])
    )


def q10_capacity_blocked(
    audit: pl.DataFrame, sim: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    sim_keys = (
        sim.select([pl.col("symbol").alias("instrument"), "entry_epoch"])
           .unique()
    )
    audit_keys = audit.select(["instrument", "entry_epoch", "period"]).unique()
    blocked = audit_keys.join(
        sim_keys, on=["instrument", "entry_epoch"], how="anti"
    )
    summary = (
        blocked.group_by("period")
               .agg(pl.len().alias("blocked_entries"))
               .sort("period")
    )
    top_blocked = (
        blocked.group_by(["period", "instrument"])
               .agg(pl.len().alias("blocked_count"))
               .sort(["period", "blocked_count"], descending=[False, True])
               .group_by("period", maintain_order=True)
               .head(10)
    )
    return summary, top_blocked


def render_strategy(name: str, drill_dir: str) -> str:
    audit = pl.read_parquet(os.path.join(drill_dir, "trade_log_audit.parquet"))
    sim = pl.read_parquet(os.path.join(drill_dir, "simulator_trade_log.parquet"))

    snap_path = os.path.join(drill_dir, "scanner_snapshot.parquet")
    rs_path = os.path.join(drill_dir, "scanner_reject_summary.parquet")

    out: list[str] = []
    out.append(f"# Pattern queries — {name}\n")
    out.append(f"_Source: `{drill_dir}`_\n")
    out.append("All queries split by `period` (IS = pre-2025-01-01, "
               "OOS = 2025-01-01+). Tables truncated to top-50 rows.\n")

    out.append("## Q1 — Top-10 instruments by trade count (sim)\n")
    out.append(_md_table(q1_top_by_trades(sim)))

    out.append("\n## Q2 — Top-10 instruments by net_pnl (sim)\n")
    out.append(_md_table(q2_top_by_pnl(sim)))

    out.append("\n## Q3 — Day-of-week distribution (audit traded vs sim)\n")
    out.append("dow: 1=Mon … 7=Sun.\n")
    out.append(_md_table(q3_dow(audit, sim)))

    out.append("\n## Q4 — Hold-time distribution by exit_reason (sim)\n")
    out.append(_md_table(q4_holdtime_by_exit(sim)))

    out.append("\n## Q5 — Direction-score histogram at entry (audit, "
               "0.1-wide buckets)\n")
    out.append(_md_table(q5_direction_score_hist(audit)))

    if "entry_regime_bullish" in audit.columns:
        out.append("\n## Q6 — Regime-bullish split at entry (audit)\n")
        out.append(_md_table(q6_regime_split(audit)))
    else:
        out.append("\n## Q6 — Regime-bullish split at entry (audit)\n")
        out.append("_skipped — strategy has no regime gate_\n")

    out.append("\n## Q7 — Scanner pass-rate by month\n")
    if os.path.exists(snap_path):
        snap = pl.read_parquet(snap_path)
        out.append(_md_table(q7_scanner_by_month_eod_b(snap), max_rows=200))
    elif os.path.exists(rs_path):
        rs = pl.read_parquet(rs_path)
        out.append(_md_table(q7_scanner_by_month_eod_t(rs), max_rows=200))
    else:
        out.append("_no scanner artifact found_")

    out.append("\n## Q8 — Trade pnl distribution by exit_reason (sim)\n")
    out.append(_md_table(q8_pnl_by_exit(sim)))

    out.append("\n## Q9 — Year-over-year trade volume & hit-rate (sim)\n")
    out.append(_md_table(q9_yearly(sim), max_rows=50))

    out.append("\n## Q10 — Capacity-blocked entries (audit but not in sim)\n")
    summary, top_blocked = q10_capacity_blocked(audit, sim)
    out.append("### Counts by period\n")
    out.append(_md_table(summary))
    out.append("\n### Top-10 most-blocked instruments per period\n")
    out.append(_md_table(top_blocked))

    return "\n".join(out) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--eod-breakout-dir", required=True)
    p.add_argument("--eod-technical-dir", required=True)
    p.add_argument("--out-dir", default="docs/inspection")
    args = p.parse_args()

    out_dir = (
        os.path.join(REPO_ROOT, args.out_dir)
        if not os.path.isabs(args.out_dir) else args.out_dir
    )
    os.makedirs(out_dir, exist_ok=True)

    for strat, ddir in [
        ("eod_breakout", args.eod_breakout_dir),
        ("eod_technical", args.eod_technical_dir),
    ]:
        md = render_strategy(strat, ddir)
        path = os.path.join(out_dir, f"PATTERNS_{strat}.md")
        with open(path, "w") as f:
            f.write(md)
        print(f"# wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

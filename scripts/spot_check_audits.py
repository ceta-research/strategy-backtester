"""Audit spot-check generator — Phase 3c of inspection drill (2026-04-28).

For each strategy, samples:
  - 20 entries stratified by (period, exit_reason) — joins audit ↔
    simulator on (instrument, entry_epoch) and tabulates diffs.
  - 5 capacity-blocked entries — audit rows whose (instrument, entry_epoch)
    pair has no counterpart in the simulator trade log. These are
    candidates the strategy WANTED to take but the position-cap rejected.

Emits a markdown report with:
  - The two tables
  - A field-by-field reconciliation note
  - Hand-runnable ``fetch_ohlcv_window.py`` commands for the first 2
    sampled entries per strategy (a "recipe" for manual deep-dive).

Sampling is deterministic: ``random.seed(0)`` + sort-on-keys.

Usage:
    python3 scripts/spot_check_audits.py \
        --eod-breakout-dir results/eod_breakout/audit_drill_20260428T124754Z \
        --eod-technical-dir results/eod_technical/audit_drill_20260428T124832Z \
        --out docs/inspection/SPOT_CHECKS.md
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402


def _ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _stratified_sample(
    df: pl.DataFrame,
    by: list[str],
    n_total: int,
    seed: int = 0,
) -> pl.DataFrame:
    """Pick ~n_total rows split proportionally across (by) groups.

    Each group gets ceil(n_total * group_share) rows; deterministic
    selection picks rows whose hash-by-row-index puts them at the front
    of a sorted shuffle.
    """
    if df.is_empty():
        return df
    df = df.with_row_index("_idx")
    counts = df.group_by(by).agg(pl.len().alias("_n"))
    total = int(counts["_n"].sum())
    quotas = counts.with_columns(
        ((pl.col("_n") / total) * n_total).ceil().cast(pl.Int64).alias("_q")
    )
    rng = random.Random(seed)
    picks: list[int] = []
    for row in quotas.iter_rows(named=True):
        key = {c: row[c] for c in by}
        sub = df
        for c, v in key.items():
            sub = sub.filter(pl.col(c) == v)
        if sub.is_empty():
            continue
        ids = sub["_idx"].to_list()
        rng.shuffle(ids)
        picks.extend(ids[: row["_q"]])

    out = df.filter(pl.col("_idx").is_in(picks)).head(n_total).drop("_idx")
    return out


def _join_audit_sim(
    audit: pl.DataFrame, sim: pl.DataFrame
) -> pl.DataFrame:
    """Inner-join audit↔simulator on (instrument, entry_epoch)."""
    sim_renamed = sim.rename({
        "symbol": "instrument",
        "entry_price": "sim_entry_price",
        "exit_price": "sim_exit_price",
        "exit_epoch": "sim_exit_epoch",
        "exit_reason": "sim_exit_reason",
        "pnl_pct": "sim_pnl_pct",
        "hold_days": "sim_hold_days",
        "quantity": "sim_quantity",
    }).select([
        "instrument", "entry_epoch", "sim_entry_price", "sim_exit_price",
        "sim_exit_epoch", "sim_exit_reason", "sim_pnl_pct",
        "sim_hold_days", "sim_quantity",
    ])
    audit_renamed = audit.rename({
        "entry_price": "audit_entry_price",
        "exit_price": "audit_exit_price",
        "exit_epoch": "audit_exit_epoch",
        "exit_reason": "audit_exit_reason",
    })
    return audit_renamed.join(
        sim_renamed, on=["instrument", "entry_epoch"], how="inner"
    )


def _md_table(df: pl.DataFrame, cols: list[str]) -> str:
    if df.is_empty():
        return "_(empty)_"
    df = df.select([c for c in cols if c in df.columns])
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


def _diff_summary(joined: pl.DataFrame) -> dict:
    """Compute audit↔sim agreement stats."""
    if joined.is_empty():
        return {}
    # Simulator truncates prices at 4 decimals; audit retains full float.
    # 1e-3 tolerance filters out display-rounding noise without masking any
    # real disagreement (real fills differ by at least 0.01).
    eps = 1e-3
    matches_entry = (
        (pl.col("audit_entry_price") - pl.col("sim_entry_price")).abs() < eps
    )
    matches_exit_price = (
        (pl.col("audit_exit_price") - pl.col("sim_exit_price")).abs() < eps
    )
    matches_exit_epoch = pl.col("audit_exit_epoch") == pl.col("sim_exit_epoch")
    return {
        "n": joined.height,
        "entry_price_match": joined.filter(matches_entry).height,
        "exit_price_match": joined.filter(matches_exit_price).height,
        "exit_epoch_match": joined.filter(matches_exit_epoch).height,
    }


def _strategy_section(strategy: str, drill_dir: str) -> str:
    audit = pl.read_parquet(os.path.join(drill_dir, "trade_log_audit.parquet"))
    sim = pl.read_parquet(os.path.join(drill_dir, "simulator_trade_log.parquet"))

    # Join audit ↔ simulator on (instrument, entry_epoch).
    sim_for_join = sim.rename({"symbol": "instrument"})
    joined_all = _join_audit_sim(audit, sim)
    diff_summary = _diff_summary(joined_all)

    # Stratified sample of 20 by (period, audit_exit_reason).
    sampled = _stratified_sample(
        joined_all, by=["period", "audit_exit_reason"], n_total=20
    )

    # Capacity-blocked: audit rows whose (instrument, entry_epoch) pair
    # has NO simulator counterpart. Take 5 stratified by period.
    audit_keys = audit.select(["instrument", "entry_epoch"]).unique()
    sim_keys = sim_for_join.select(["instrument", "entry_epoch"]).unique()
    blocked_keys = audit_keys.join(
        sim_keys, on=["instrument", "entry_epoch"], how="anti"
    )
    blocked = audit.join(blocked_keys, on=["instrument", "entry_epoch"],
                         how="inner")
    blocked_sample = _stratified_sample(
        blocked, by=["period"], n_total=5
    )

    # Format dates for human readability.
    sampled = sampled.with_columns([
        pl.col("entry_epoch").map_elements(_ts, return_dtype=pl.Utf8).alias("entry_date"),
        pl.col("audit_exit_epoch").map_elements(_ts, return_dtype=pl.Utf8).alias("audit_exit_date"),
        pl.col("sim_exit_epoch").map_elements(_ts, return_dtype=pl.Utf8).alias("sim_exit_date"),
    ])
    blocked_sample = blocked_sample.with_columns([
        pl.col("entry_epoch").map_elements(_ts, return_dtype=pl.Utf8).alias("entry_date"),
        pl.col("exit_epoch").map_elements(_ts, return_dtype=pl.Utf8).alias("exit_date"),
    ])

    # Pick 2 trades for OHLCV recipe (most/least profitable in sample).
    if not sampled.is_empty():
        recipe_picks = (
            sampled
            .with_columns(
                ((pl.col("sim_exit_price") - pl.col("sim_entry_price"))
                 / pl.col("sim_entry_price")).alias("_ret")
            )
            .sort("_ret")
            .head(1)
            .vstack(
                sampled.with_columns(
                    ((pl.col("sim_exit_price") - pl.col("sim_entry_price"))
                     / pl.col("sim_entry_price")).alias("_ret")
                ).sort("_ret", descending=True).head(1)
            )
        )
    else:
        recipe_picks = sampled

    # Build markdown.
    out: list[str] = []
    out.append(f"## {strategy}\n")
    out.append(f"_Source: `{drill_dir}`_\n")

    out.append("### Audit↔Simulator agreement (full join)\n")
    out.append(f"- joined rows: **{diff_summary.get('n', 0):,}**")
    if diff_summary:
        out.append(
            f"- entry_price match (|Δ|<1e-3, ignores 4-decimal sim truncation): **{diff_summary['entry_price_match']:,}** "
            f"({100.0*diff_summary['entry_price_match']/diff_summary['n']:.2f}%)"
        )
        out.append(
            f"- exit_price match: **{diff_summary['exit_price_match']:,}** "
            f"({100.0*diff_summary['exit_price_match']/diff_summary['n']:.2f}%)"
        )
        out.append(
            f"- exit_epoch match: **{diff_summary['exit_epoch_match']:,}** "
            f"({100.0*diff_summary['exit_epoch_match']/diff_summary['n']:.2f}%)"
        )
    out.append("")

    out.append("### 20 stratified entries (audit vs simulator)\n")
    sample_cols = [
        "instrument", "period", "entry_date",
        "audit_entry_price", "sim_entry_price",
        "audit_exit_date", "sim_exit_date",
        "audit_exit_price", "sim_exit_price",
        "audit_exit_reason", "sim_exit_reason",
        "sim_pnl_pct", "sim_hold_days",
    ]
    out.append(_md_table(sampled, sample_cols))
    out.append("")

    out.append("### 5 capacity-blocked entries (no simulator counterpart)\n")
    blocked_cols = [
        "instrument", "period", "entry_date", "exit_date",
        "entry_price", "exit_price", "exit_reason",
        "entry_close_signal", "entry_n_day_high", "entry_direction_score",
    ]
    out.append(_md_table(blocked_sample, blocked_cols))
    out.append("")

    out.append("### Manual OHLCV deep-dive recipe\n")
    out.append("Run these to fetch ±5 bars around each entry epoch:\n")
    out.append("```bash")
    for row in recipe_picks.iter_rows(named=True):
        out.append(
            f"python3 scripts/fetch_ohlcv_window.py "
            f"--instrument {row['instrument']} "
            f"--epoch {row['entry_epoch']} --window-days 5"
        )
    out.append("```\n")

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--eod-breakout-dir", required=True)
    p.add_argument("--eod-technical-dir", required=True)
    p.add_argument("--out", default="docs/inspection/SPOT_CHECKS.md")
    args = p.parse_args()

    md_parts = ["# Audit spot-checks — Phase 3c\n",
                "Sampled 20 traded entries + 5 capacity-blocked entries per "
                "strategy. The full join of audit↔simulator (rows that made "
                "it to the simulator) is also summarised. Sampling is "
                "deterministic (`random.seed(0)`).\n"]

    md_parts.append(_strategy_section(
        "eod_breakout", args.eod_breakout_dir,
    ))
    md_parts.append(_strategy_section(
        "eod_technical", args.eod_technical_dir,
    ))

    md = "\n".join(md_parts) + "\n"
    out_path = (os.path.join(REPO_ROOT, args.out)
                if not os.path.isabs(args.out) else args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

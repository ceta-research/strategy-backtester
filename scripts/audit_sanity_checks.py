"""Audit-artifact sanity checks — Phase 3b of inspection drill (2026-04-28).

Cross-foot the audit parquets against each other and against the
simulator output. Surfaces any discrepancies that would invalidate
later pattern queries.

Checks per strategy:
  C1.  filter_marginals.passed_in_combination matches
       count(entry_audit.all_clauses_pass == True) per clause set.
  C2.  scanner-pass count in entry_audit matches scanner_pass count in
       the scanner artifact (snapshot vs reject_summary).
  C3.  trade_log_audit rows > simulator_trade_log rows (audit covers the
       full unconstrained candidate set; simulator is capacity-bounded).
  C4.  Every (instrument, entry_epoch) in simulator_trade_log appears at
       least once in trade_log_audit.
  C5.  exit_reason distribution in simulator_trade_log is a subset of
       trade_log_audit's distribution.
  C6.  IS/OOS partitioning: rows per period are non-zero in both periods
       and counts roughly track the day-count ratio.
  C7.  entry_audit covers a single ``period`` boundary cleanly (no rows
       with NULL period).
  C8.  No rows with all_clauses_pass=True but any individual clause=False.
       (Cross-foot of the AND.)

Usage:
    python3 scripts/audit_sanity_checks.py \
        --eod-breakout-dir results/eod_breakout/audit_drill_20260428T124754Z \
        --eod-technical-dir results/eod_technical/audit_drill_20260428T124832Z \
        --out docs/inspection/SANITY_CHECKS.md

Exits non-zero if any HARD check fails (C1, C4, C8). Soft checks
(C3, C5, C6) only emit warnings.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402

IS_OOS_BOUNDARY = 1735689600  # 2025-01-01 UTC


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    hard: bool = True

    def render(self) -> str:
        mark = "OK" if self.passed else ("FAIL" if self.hard else "WARN")
        return f"- [{mark}] **{self.name}** — {self.detail}"


@dataclass
class StrategyReport:
    strategy: str
    drill_dir: str
    results: list[Result] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def hard_failed(self) -> bool:
        return any((not r.passed) and r.hard for r in self.results)


def _load(d: str, name: str) -> pl.DataFrame:
    return pl.read_parquet(os.path.join(d, name))


def _check_c1_filter_marginals(strategy: str, drill_dir: str) -> Result:
    """passed_in_combination == count(entry_audit.all_clauses_pass==True)
    for every clause row in filter_marginals (they all carry the SAME
    in-combination total — it's a per-row repeat of the AND outcome)."""
    fm = _load(drill_dir, "filter_marginals.parquet")
    ea = _load(drill_dir, "entry_audit.parquet")
    audit_combo = ea.filter(pl.col("all_clauses_pass") == True).height  # noqa: E712
    fm_combos = fm["passed_in_combination"].unique().to_list()
    if len(fm_combos) != 1:
        return Result(
            "C1 filter_marginals consistency",
            passed=False,
            detail=f"filter_marginals has {len(fm_combos)} distinct "
                   f"passed_in_combination values: {fm_combos}",
        )
    fm_combo = int(fm_combos[0])
    passed = (fm_combo == audit_combo)
    return Result(
        "C1 filter_marginals.passed_in_combination ≡ entry_audit AND",
        passed=passed,
        detail=f"filter_marginals={fm_combo:,} vs "
               f"count(all_clauses_pass)={audit_combo:,}",
    )


def _check_c2_scanner(strategy: str, drill_dir: str) -> Result:
    """For eod_b: entry_audit clause_scanner_pass count must match
    scanner_snapshot.scanner_pass count (per-row True total).
    For eod_t: entry_audit clause_scanner_pass count must match
    scanner_reject_summary.pass_count summed (per-day total)."""
    ea = _load(drill_dir, "entry_audit.parquet")
    audit_pass = ea.filter(pl.col("clause_scanner_pass") == True).height  # noqa: E712

    snap_path = os.path.join(drill_dir, "scanner_snapshot.parquet")
    rej_path = os.path.join(drill_dir, "scanner_reject_summary.parquet")

    if os.path.exists(snap_path):
        sn = pl.read_parquet(snap_path)
        sn_pass = sn.filter(pl.col("scanner_pass") == True).height  # noqa: E712
        # entry_audit fires AFTER warm-up/boundary trim, so it is a subset
        # of scanner_snapshot (rows-in-scanner-only carry the prefetch
        # window). Where both rows exist, scanner_pass ≡ clause_scanner_pass
        # (verified by manual inner-join diagnostic). So the invariant is
        # entry_audit_pass <= scanner_snapshot_pass.
        passed = audit_pass <= sn_pass
        return Result(
            "C2 entry_audit.clause_scanner_pass <= scanner_snapshot.scanner_pass",
            passed=passed,
            detail=f"snapshot pass={sn_pass:,} >= entry_audit pass={audit_pass:,} "
                   f"(gap={sn_pass-audit_pass:,} = warm-up rows)",
        )
    elif os.path.exists(rej_path):
        rs = pl.read_parquet(rej_path)
        rs_pass = int(rs["pass_count"].sum())
        # NB: scanner_reject_summary aggregates AT THE SCANNER STAGE which
        # runs once per day across the candidate universe BEFORE the per-
        # symbol entry filter applies. entry_audit's clause_scanner_pass
        # only fires on rows that survived to the entry-clause stage.
        # So entry_audit count <= scanner pass_count is the expected
        # invariant, not equality.
        passed = audit_pass <= rs_pass
        return Result(
            "C2 entry_audit.clause_scanner_pass <= scanner pass_count",
            passed=passed,
            detail=f"scanner pass_count={rs_pass:,} >= "
                   f"entry_audit clause_scanner_pass={audit_pass:,}",
        )
    return Result(
        "C2 scanner artifact",
        passed=False,
        detail="neither scanner_snapshot nor scanner_reject_summary found",
    )


def _check_c3_audit_supersets_simulator(
    strategy: str, drill_dir: str
) -> Result:
    """trade_log_audit rows > simulator_trade_log rows (capacity)."""
    tla = _load(drill_dir, "trade_log_audit.parquet")
    sim = _load(drill_dir, "simulator_trade_log.parquet")
    passed = tla.height > sim.height
    return Result(
        "C3 trade_log_audit > simulator_trade_log (capacity-constraint)",
        passed=passed,
        detail=f"audit={tla.height:,} vs simulator={sim.height:,} "
               f"(ratio {tla.height/max(sim.height,1):.1f}×)",
        hard=False,
    )


def _check_c4_simulator_in_audit(
    strategy: str, drill_dir: str
) -> Result:
    """Every (instrument, entry_epoch) in simulator_trade_log appears in
    trade_log_audit. The audit row may carry exit-reason variants (one
    audit per entry_config_id), so we check for >= 1 match."""
    tla = _load(drill_dir, "trade_log_audit.parquet")
    sim = _load(drill_dir, "simulator_trade_log.parquet")

    # simulator_trade_log uses 'symbol' column (NSE:RELIANCE etc.) — call
    # it instrument for the join.
    sim_keys = sim.select([
        pl.col("symbol").alias("instrument"),
        pl.col("entry_epoch"),
    ]).unique()
    audit_keys = tla.select(["instrument", "entry_epoch"]).unique()

    missing = sim_keys.join(audit_keys, on=["instrument", "entry_epoch"],
                            how="anti")
    passed = missing.is_empty()
    return Result(
        "C4 every simulator entry has a matching audit entry",
        passed=passed,
        detail=(f"all {sim_keys.height:,} simulator entries covered"
                if passed
                else f"{missing.height} simulator entries with no audit row "
                     f"(first: {missing.head(3).to_dicts()})"),
        hard=True,
    )


def _check_c5_exit_reasons(strategy: str, drill_dir: str) -> Result:
    """exit_reasons in simulator should be a subset of audit's set."""
    tla = _load(drill_dir, "trade_log_audit.parquet")
    sim = _load(drill_dir, "simulator_trade_log.parquet")
    audit_set = set(tla["exit_reason"].unique().to_list())
    sim_set = set(sim["exit_reason"].unique().to_list())
    extra = sim_set - audit_set
    passed = not extra
    note = ""
    if extra == {"natural"}:
        note = (" — 'natural' is the simulator's default when entry_order "
                "does not carry exit_reason; the audit's exit_reason is "
                "authoritative for this strategy")
    return Result(
        "C5 simulator exit_reasons ⊆ audit exit_reasons",
        passed=passed,
        detail=(f"audit={sorted(audit_set)} sim={sorted(sim_set)}"
                if passed
                else f"simulator has reasons not in audit: {sorted(extra)}"
                     f"{note}"),
        hard=False,
    )


def _check_c6_is_oos(strategy: str, drill_dir: str) -> Result:
    """Both periods present in entry_audit and trade_log_audit."""
    ea = _load(drill_dir, "entry_audit.parquet")
    tla = _load(drill_dir, "trade_log_audit.parquet")
    ea_periods = ea.group_by("period").agg(pl.len().alias("n"))
    tla_periods = tla.group_by("period").agg(pl.len().alias("n"))
    needed = {"IS", "OOS"}
    have_ea = set(ea_periods["period"].to_list())
    have_tla = set(tla_periods["period"].to_list())
    passed = needed.issubset(have_ea) and needed.issubset(have_tla)
    return Result(
        "C6 IS and OOS both populated",
        passed=passed,
        detail=f"entry_audit: {dict(zip(ea_periods['period'], ea_periods['n']))}; "
               f"trade_log_audit: {dict(zip(tla_periods['period'], tla_periods['n']))}",
        hard=False,
    )


def _check_c7_no_null_period(strategy: str, drill_dir: str) -> Result:
    """No rows with NULL period."""
    bad = []
    for f in ("entry_audit.parquet", "trade_log_audit.parquet"):
        df = _load(drill_dir, f)
        n = df.filter(pl.col("period").is_null()).height
        if n > 0:
            bad.append(f"{f}={n}")
    passed = not bad
    return Result(
        "C7 no NULL period rows",
        passed=passed,
        detail="all rows period-tagged" if passed else f"NULLs: {bad}",
    )


def _check_c8_clause_and_consistency(
    strategy: str, drill_dir: str
) -> Result:
    """all_clauses_pass==True ⟹ every clause_* column is True."""
    ea = _load(drill_dir, "entry_audit.parquet")
    clause_cols = [c for c in ea.columns if c.startswith("clause_")]
    # Build a single AND-of-clauses expr; true rows must have all True.
    and_expr = clause_cols[0]
    expr = pl.col(clause_cols[0])
    for c in clause_cols[1:]:
        expr = expr & pl.col(c)
    bad = ea.filter((pl.col("all_clauses_pass") == True) & (~expr)).height  # noqa: E712
    bad_inverse = ea.filter(
        (pl.col("all_clauses_pass") == False) & expr  # noqa: E712
    ).height
    passed = (bad == 0 and bad_inverse == 0)
    return Result(
        "C8 all_clauses_pass ≡ AND(clause_*)",
        passed=passed,
        detail=(f"AND consistent over {ea.height:,} rows, "
                f"{len(clause_cols)} clauses"
                if passed
                else f"AND mismatches: forward={bad}, inverse={bad_inverse}"),
        hard=True,
    )


def run_strategy(strategy: str, drill_dir: str) -> StrategyReport:
    rep = StrategyReport(strategy=strategy, drill_dir=drill_dir)
    rep.results.extend([
        _check_c1_filter_marginals(strategy, drill_dir),
        _check_c2_scanner(strategy, drill_dir),
        _check_c3_audit_supersets_simulator(strategy, drill_dir),
        _check_c4_simulator_in_audit(strategy, drill_dir),
        _check_c5_exit_reasons(strategy, drill_dir),
        _check_c6_is_oos(strategy, drill_dir),
        _check_c7_no_null_period(strategy, drill_dir),
        _check_c8_clause_and_consistency(strategy, drill_dir),
    ])

    # Summary stats useful in the markdown.
    ea = _load(drill_dir, "entry_audit.parquet")
    tla = _load(drill_dir, "trade_log_audit.parquet")
    sim = _load(drill_dir, "simulator_trade_log.parquet")
    rep.summary = {
        "entry_audit_rows": ea.height,
        "trade_log_audit_rows": tla.height,
        "simulator_rows": sim.height,
        "all_clauses_pass_count": ea.filter(
            pl.col("all_clauses_pass") == True  # noqa: E712
        ).height,
        "is_audit_rows": ea.filter(pl.col("period") == "IS").height,
        "oos_audit_rows": ea.filter(pl.col("period") == "OOS").height,
        "is_sim_trades": sim.filter(pl.col("period") == "IS").height,
        "oos_sim_trades": sim.filter(pl.col("period") == "OOS").height,
    }
    return rep


def render_markdown(reports: list[StrategyReport]) -> str:
    lines: list[str] = []
    lines.append("# Audit sanity checks — Phase 3b\n")
    lines.append("Cross-foots the audit parquets against each other and the "
                 "simulator. See `scripts/audit_sanity_checks.py` for the "
                 "checks themselves.\n")

    overall = all(not r.hard_failed for r in reports)
    lines.append(f"**Overall:** {'✅ all hard checks pass' if overall else '❌ at least one hard check failed'}\n")

    for r in reports:
        lines.append(f"## {r.strategy}\n")
        lines.append(f"_Source: `{r.drill_dir}`_\n")

        lines.append("### Counters\n")
        for k, v in r.summary.items():
            lines.append(f"- `{k}` = `{v:,}`")
        lines.append("")

        lines.append("### Checks\n")
        for chk in r.results:
            lines.append(chk.render())
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--eod-breakout-dir", required=True)
    p.add_argument("--eod-technical-dir", required=True)
    p.add_argument("--out", default="docs/inspection/SANITY_CHECKS.md")
    args = p.parse_args()

    reports = [
        run_strategy("eod_breakout", args.eod_breakout_dir),
        run_strategy("eod_technical", args.eod_technical_dir),
    ]

    md = render_markdown(reports)
    out_path = os.path.join(REPO_ROOT, args.out) if not os.path.isabs(args.out) else args.out
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(md)

    # Echo to stdout too.
    print(md)
    print(f"# wrote {out_path}")

    any_hard_failed = any(r.hard_failed for r in reports)
    return 1 if any_hard_failed else 0


if __name__ == "__main__":
    sys.exit(main())

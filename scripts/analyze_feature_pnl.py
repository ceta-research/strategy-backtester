"""Univariate + bivariate feature→pnl analysis — Phase 5b/5c (2026-04-28).

Reads trade_features.parquet and produces per-strategy markdown showing:
  - Quintile breakdown of mean pnl per feature
  - Feature leaderboard (spread, monotonicity, IS↔OOS consistency)
  - Loss concentration: which features flag big losers?
  - Top-3 candidate veto rules

Usage:
    python3 scripts/analyze_feature_pnl.py --strategy eod_breakout
    python3 scripts/analyze_feature_pnl.py --all
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402
import numpy as np  # noqa: E402

DRILL_DIRS = {
    "eod_breakout": "results/eod_breakout/audit_drill_20260428T124754Z",
    "eod_technical": "results/eod_technical/audit_drill_20260428T124832Z",
}

# Features to analyze (continuous, numeric — skip categoricals).
FEATURES = [
    "entry_direction_score", "breakout_extension", "entry_gap",
    "ret_1d", "ret_5d", "ret_20d", "ret_60d",
    "dist_ma20", "dist_ma50", "dist_ma200",
    "ma20_slope_5d",
    "vol_5d", "vol_20d", "vol_60d", "atr14_pct",
    "vol_spike_20d", "volume_zscore_60d",
    "body_ratio", "close_in_range", "upper_wick_ratio",
    "up_days_5",
    "nifty_ret_1d", "nifty_ret_5d", "nifty_ret_20d",
    "nifty_dist_sma100",
    "mfe_pct", "mae_pct", "give_back", "tsl_efficiency",
]


def quintile_analysis(
    df: pl.DataFrame, feature: str, period: str
) -> dict | None:
    """Compute quintile breakdown for one feature in one period."""
    sub = df.filter(pl.col("period") == period).select([feature, "pnl_pct", "is_loser"])
    sub = sub.filter(pl.col(feature).is_not_null() & pl.col(feature).is_not_nan())
    if sub.height < 100:
        return None

    # Compute quintile boundaries
    vals = sub[feature].to_numpy()
    try:
        boundaries = np.nanpercentile(vals, [20, 40, 60, 80])
    except Exception:
        return None

    # Assign quintile
    sub = sub.with_columns(
        pl.when(pl.col(feature) <= boundaries[0]).then(pl.lit(1))
          .when(pl.col(feature) <= boundaries[1]).then(pl.lit(2))
          .when(pl.col(feature) <= boundaries[2]).then(pl.lit(3))
          .when(pl.col(feature) <= boundaries[3]).then(pl.lit(4))
          .otherwise(pl.lit(5))
          .alias("quintile")
    )

    agg = (
        sub.group_by("quintile")
        .agg([
            pl.len().alias("count"),
            pl.col("pnl_pct").mean().alias("mean_pnl"),
            pl.col("pnl_pct").median().alias("median_pnl"),
            pl.col("is_loser").mean().alias("loss_rate"),
            pl.col(feature).min().alias("feat_min"),
            pl.col(feature).max().alias("feat_max"),
        ])
        .sort("quintile")
    )

    rows = agg.to_dicts()
    if not rows:
        return None

    mean_pnls = [r["mean_pnl"] for r in rows]
    spread = mean_pnls[-1] - mean_pnls[0]  # Q5 - Q1
    # Monotonicity: rank-correlation of quintile vs mean_pnl
    mono = np.corrcoef(range(len(mean_pnls)), mean_pnls)[0, 1]

    return {
        "feature": feature,
        "period": period,
        "quintiles": rows,
        "spread": spread,
        "mono": mono,
        "q1_mean": mean_pnls[0],
        "q5_mean": mean_pnls[-1],
        "q1_loss_rate": rows[0]["loss_rate"],
        "q5_loss_rate": rows[-1]["loss_rate"],
    }


def loss_concentration(df: pl.DataFrame, feature: str, period: str) -> dict | None:
    """What fraction of big losers are in bottom quintile of this feature?"""
    sub = df.filter(
        (pl.col("period") == period)
        & pl.col(feature).is_not_null()
        & ~pl.col(feature).is_nan()
    )
    if sub.height < 100:
        return None
    vals = sub[feature].to_numpy()
    try:
        q20 = np.nanpercentile(vals, 20)
    except Exception:
        return None

    total_losers = sub.filter(pl.col("is_big_loser")).height
    if total_losers == 0:
        return None
    losers_in_q1 = sub.filter(
        (pl.col("is_big_loser")) & (pl.col(feature) <= q20)
    ).height
    concentration = losers_in_q1 / total_losers
    # Also: what fraction of Q1 are big losers?
    q1_total = sub.filter(pl.col(feature) <= q20).height
    q1_loser_rate = losers_in_q1 / max(q1_total, 1)

    return {
        "feature": feature,
        "period": period,
        "total_big_losers": total_losers,
        "losers_in_q1": losers_in_q1,
        "concentration_pct": concentration * 100,
        "q1_big_loser_rate": q1_loser_rate * 100,
    }


def render_strategy(strategy: str) -> str:
    drill_dir = os.path.join(REPO_ROOT, DRILL_DIRS[strategy])
    feat_path = os.path.join(drill_dir, "trade_features.parquet")
    df = pl.read_parquet(feat_path)

    lines: list[str] = []
    lines.append(f"# Feature → PnL analysis: {strategy}\n")
    lines.append(f"_Source: `{feat_path}`_\n")
    lines.append(f"Total rows: {df.height:,}. "
                 f"IS: {df.filter(pl.col('period')=='IS').height:,}. "
                 f"OOS: {df.filter(pl.col('period')=='OOS').height:,}.\n")

    # Phase 5b: Univariate leaderboard
    results = []
    for feat in FEATURES:
        if feat not in df.columns:
            continue
        for period in ["IS", "OOS"]:
            r = quintile_analysis(df, feat, period)
            if r:
                results.append(r)

    # Build leaderboard: sort by abs(spread) on IS, check OOS consistency.
    is_results = {r["feature"]: r for r in results if r["period"] == "IS"}
    oos_results = {r["feature"]: r for r in results if r["period"] == "OOS"}

    leaderboard = []
    for feat, is_r in is_results.items():
        oos_r = oos_results.get(feat)
        oos_spread = oos_r["spread"] if oos_r else None
        consistent = (oos_spread is not None and
                      (is_r["spread"] > 0) == (oos_spread > 0))
        leaderboard.append({
            "feature": feat,
            "is_spread": is_r["spread"],
            "oos_spread": oos_spread,
            "is_mono": is_r["mono"],
            "is_q1_mean": is_r["q1_mean"],
            "is_q5_mean": is_r["q5_mean"],
            "is_q1_loss_rate": is_r["q1_loss_rate"],
            "consistent": consistent,
        })
    leaderboard.sort(key=lambda x: abs(x["is_spread"]), reverse=True)

    lines.append("## Feature leaderboard (sorted by |IS spread|)\n")
    lines.append("Spread = Q5_mean_pnl − Q1_mean_pnl. Positive = high feature → "
                 "high pnl. Mono = rank correlation.\n")
    lines.append("| Feature | IS spread | OOS spread | IS mono | Q1 mean | "
                 "Q5 mean | Q1 loss% | IS↔OOS? |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for row in leaderboard[:30]:
        oos_s = f"{row['oos_spread']:.4f}" if row['oos_spread'] is not None else "n/a"
        cons = "✓" if row["consistent"] else "✗"
        lines.append(
            f"| `{row['feature']}` | {row['is_spread']:.4f} | {oos_s} | "
            f"{row['is_mono']:.3f} | {row['is_q1_mean']*100:.2f}% | "
            f"{row['is_q5_mean']*100:.2f}% | {row['is_q1_loss_rate']*100:.1f}% | {cons} |"
        )
    lines.append("")

    # Detailed quintile tables for top 8
    lines.append("## Top features — quintile detail\n")
    for row in leaderboard[:8]:
        feat = row["feature"]
        lines.append(f"### `{feat}` (IS spread {row['is_spread']*100:.2f}%)\n")
        for period in ["IS", "OOS"]:
            r = is_results[feat] if period == "IS" else oos_results.get(feat)
            if not r:
                continue
            lines.append(f"**{period}:**\n")
            lines.append("| Q | count | feat_min | feat_max | mean_pnl% | "
                         "median_pnl% | loss_rate |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for q in r["quintiles"]:
                lines.append(
                    f"| Q{q['quintile']} | {q['count']:,} | {q['feat_min']:.4f} | "
                    f"{q['feat_max']:.4f} | {q['mean_pnl']*100:.2f}% | "
                    f"{q['median_pnl']*100:.2f}% | {q['loss_rate']*100:.1f}% |"
                )
            lines.append("")

    # Phase 5c: Loss concentration
    lines.append("## Loss concentration (big losers in feature Q1)\n")
    lines.append("For each feature: what % of big losers (pnl < −10%) fall "
                 "in the bottom quintile of that feature?\n")
    lines.append("| Feature | Period | Total big losers | In Q1 | "
                 "Concentration | Q1 big-loser rate |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for feat in [r["feature"] for r in leaderboard[:15]]:
        if feat not in df.columns:
            continue
        for period in ["IS", "OOS"]:
            lc = loss_concentration(df, feat, period)
            if lc:
                lines.append(
                    f"| `{feat}` | {period} | {lc['total_big_losers']:,} | "
                    f"{lc['losers_in_q1']:,} | {lc['concentration_pct']:.1f}% | "
                    f"{lc['q1_big_loser_rate']:.1f}% |"
                )
    lines.append("")

    # Exit quality summary (MFE/MAE/give_back)
    lines.append("## Exit quality summary (MFE / give_back)\n")
    for period in ["IS", "OOS"]:
        sub = df.filter(pl.col("period") == period)
        lines.append(f"**{period}** (n={sub.height:,}):\n")
        for col in ["mfe_pct", "mae_pct", "give_back", "tsl_efficiency"]:
            if col not in sub.columns:
                continue
            s = sub[col].drop_nulls().drop_nans()
            if s.len() == 0:
                continue
            lines.append(
                f"- `{col}`: mean={s.mean():.4f}, median={s.median():.4f}, "
                f"p10={s.quantile(0.1):.4f}, p90={s.quantile(0.9):.4f}"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", choices=list(DRILL_DIRS.keys()))
    p.add_argument("--all", action="store_true")
    args = p.parse_args()

    strategies = list(DRILL_DIRS.keys()) if args.all else [args.strategy]
    if not strategies or strategies == [None]:
        p.error("Provide --strategy or --all")

    out_dir = os.path.join(REPO_ROOT, "docs", "inspection")
    os.makedirs(out_dir, exist_ok=True)

    for s in strategies:
        md = render_strategy(s)
        out_path = os.path.join(out_dir, f"FEATURE_PNL_{s}.md")
        with open(out_path, "w") as f:
            f.write(md)
        print(f"# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

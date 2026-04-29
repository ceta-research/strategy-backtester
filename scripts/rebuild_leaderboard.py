"""Rebuild LEADERBOARD.md from cached results — no pipeline re-runs.

Reads the best available result JSON for each strategy (champion_verify,
champion, champion_pre_audit_baseline, or latest round file) and emits
a ranked markdown table.

Usage:
    python3 scripts/rebuild_leaderboard.py

Output:
    docs/LEADERBOARD.md
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Map of strategy → (config_path, result_json_path, status, notes)
# status: ACTIVE (has champion, tracked), VARIANT (experimental improvement),
#         RETIRED (audit_retired, not worth running)
STRATEGIES = [
    # === ACTIVE champions ===
    {
        "name": "eod_technical",
        "config": "strategies/eod_technical/config_champion.yaml",
        "result": "results/eod_technical/champion_pre_audit_baseline.json",
        "status": "ACTIVE",
        "notes": "NSE breakout, DS=0.54, no regime, TSL 10%",
    },
    {
        "name": "eod_technical (adaptive TSL 30/3)",
        "config": "strategies/eod_technical/config_adaptive_tsl_30_3.yaml",
        "result": "results/eod_technical/adaptive_tsl_sweep.json",
        "result_config_id": "1_1_18_1",
        "status": "VARIANT",
        "notes": "Champion + once MFE>30%, TSL tightens to 3%. Trades bull years for DD control.",
    },
    {
        "name": "eod_breakout",
        "config": "strategies/eod_breakout/config_champion.yaml",
        "result": "results/eod_breakout/champion_pre_audit_baseline.json",
        "status": "ACTIVE",
        "notes": "NSE breakout, DS=0.40, NIFTYBEES regime+force-exit, TSL 8%",
    },
    {
        "name": "eod_breakout (no DS + regime)",
        "config": "strategies/eod_breakout/config_no_ds_regime.yaml",
        "result": None,
        "status": "VARIANT",
        "notes": "DS disabled, regime-only. Contrarian breakouts are stronger.",
        # Verified 2026-04-28 from temp run. Re-run config to regenerate.
        "manual_stats": {
            "cagr": 0.1973, "max_drawdown": -0.2752,
            "sharpe_ratio": 1.320, "calmar_ratio": 0.717,
            "total_trades": 1861,
        },
    },
    {
        "name": "quality_dip_tiered",
        "config": "strategies/quality_dip_tiered/config_champion.yaml",
        "result": "results/quality_dip_tiered/champion_verify.json",
        "status": "ACTIVE",
        "notes": "Dip-buy quality-screened stocks, tiered sizing, TSL 10%",
    },
    {
        "name": "trending_value",
        "config": "strategies/trending_value/config_champion.yaml",
        "result": "results/trending_value/champion_verify.json",
        "status": "ACTIVE",
        "notes": "Value + momentum composite, monthly rebalance",
    },
    {
        "name": "enhanced_breakout",
        "config": "strategies/enhanced_breakout/config_champion.yaml",
        "result": "results/enhanced_breakout/round3_perturbation.json",
        "status": "ACTIVE",
        "notes": "eod_breakout variant with quality/momentum overlay",
    },
    {
        "name": "low_pe",
        "config": "strategies/low_pe/config_champion.yaml",
        "result": "results/low_pe/champion.json",
        "status": "ACTIVE",
        "notes": "Low P/E value, modern 2018+ only. Best Calmar.",
    },
    {
        "name": "factor_composite",
        "config": "strategies/factor_composite/config_champion.yaml",
        "result": "results/factor_composite/champion_verify.json",
        "status": "ACTIVE",
        "notes": "Multi-factor (value+quality+momentum), deep MDD",
    },
    {
        "name": "momentum_cascade",
        "config": "strategies/momentum_cascade/config_champion.yaml",
        "result": "results/momentum_cascade/champion_2005_2026.json",
        "status": "ACTIVE",
        "notes": "Dual-timeframe momentum with acceleration filter",
    },
    {
        "name": "ml_supertrend",
        "config": "strategies/ml_supertrend/config_champion.yaml",
        "result": "results/ml_supertrend/champion.json",
        "status": "ACTIVE",
        "notes": "Supertrend + ML-style feature screen",
    },
    {
        "name": "momentum_top_gainers",
        "config": "strategies/momentum_top_gainers/config_champion.yaml",
        "result": "results/momentum_top_gainers/round2_full.json",
        "status": "ACTIVE",
        "notes": "Top-N momentum with periodic rebalance",
    },
    {
        "name": "quality_dip_buy",
        "config": "strategies/quality_dip_buy/config_champion.yaml",
        "result": "results/quality_dip_buy/round3_fine_grid.json",
        "status": "ACTIVE",
        "notes": "Quality dip-buy (predecessor of tiered version)",
    },
    {
        "name": "forced_selling_dip",
        "config": "strategies/forced_selling_dip/config_champion.yaml",
        "result": "results/forced_selling_dip/round3_fine_grid.json",
        "status": "ACTIVE",
        "notes": "Buy forced-selling dips in quality stocks",
    },
    # === RETIRED (not worth tracking — low CAGR or broken) ===
    {"name": "bb_mean_reversion", "result": "results/bb_mean_reversion/round1.json", "status": "RETIRED", "notes": "Bollinger band MR"},
    {"name": "connors_rsi", "result": "results/connors_rsi/round1.json", "status": "RETIRED", "notes": "ConnorsRSI MR"},
    {"name": "darvas_box", "result": "results/darvas_box/round1.json", "status": "RETIRED", "notes": "Darvas box breakout"},
    {"name": "extended_ibs", "result": "results/extended_ibs/round1.json", "status": "RETIRED", "notes": "IBS extended"},
    {"name": "gap_fill", "result": "results/gap_fill/round0_baseline.json", "status": "RETIRED", "notes": "Gap fill"},
    {"name": "holp_lohp", "result": "results/holp_lohp/round1_sweep.json", "status": "RETIRED", "notes": "High of low period"},
    {"name": "ibs_mean_reversion", "result": "results/ibs_mean_reversion/round1.json", "status": "RETIRED", "notes": "IBS MR"},
    {"name": "index_breakout", "result": "results/index_breakout/round1_sweep.json", "status": "RETIRED", "notes": "Index-level breakout"},
    {"name": "index_dip_buy", "result": "results/index_dip_buy/round1_sweep.json", "status": "RETIRED", "notes": "Index dip-buy"},
    {"name": "index_green_candle", "result": "results/index_green_candle/round1_sweep.json", "status": "RETIRED", "notes": "Index green candle"},
    {"name": "index_sma_crossover", "result": "results/index_sma_crossover/round1_sweep.json", "status": "RETIRED", "notes": "SMA crossover"},
    {"name": "momentum_dip", "result": "results/momentum_dip/round2.json", "status": "RETIRED", "notes": "Momentum dip (superseded by quality variant)"},
    {"name": "momentum_dip_quality", "result": "results/momentum_dip_quality/round3_corrected.json", "status": "RETIRED", "notes": "MDP corrected (superseded by tiered)"},
    {"name": "overnight_hold", "result": "results/overnight_hold/round1_filters.json", "status": "RETIRED", "notes": "Overnight gap strategy"},
    {"name": "squeeze", "result": "results/squeeze/round1.json", "status": "RETIRED", "notes": "Squeeze breakout"},
    {"name": "swing_master", "result": "results/swing_master/round1.json", "status": "RETIRED", "notes": "Swing trading composite"},
    {"name": "momentum_rebalance", "result": None, "status": "RETIRED", "notes": "Monthly rebalance (no results)"},
]


def extract_summary(entry: dict) -> dict | None:
    """Extract CAGR/MDD/Sharpe/Calmar/trades from a result JSON."""
    # Manual stats override (for variants verified from temp runs)
    if entry.get("manual_stats"):
        return entry["manual_stats"]

    result_path = entry.get("result")
    if not result_path:
        return None

    full_path = os.path.join(REPO_ROOT, result_path)
    if not os.path.exists(full_path):
        return None

    with open(full_path) as f:
        data = json.load(f)

    # Handle sweep results (e.g., adaptive_tsl_sweep.json)
    config_id = entry.get("result_config_id")
    if config_id and "rows" in data:
        for row in data["rows"]:
            if row.get("config_id") == config_id:
                return row
        return None

    # Standard format: detailed[0].summary
    if "detailed" in data:
        det = data["detailed"]
        if isinstance(det, list) and det:
            return det[0].get("summary", {})
        elif isinstance(det, dict):
            return det.get("summary", {})

    # Fallback: top-level summary
    if "summary" in data:
        return data["summary"]

    return None


def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    active_rows = []
    retired_rows = []

    for entry in STRATEGIES:
        summary = extract_summary(entry)
        row = {
            "name": entry["name"],
            "status": entry["status"],
            "notes": entry.get("notes", ""),
            "config": entry.get("config", ""),
            "cagr": None, "max_drawdown": None,
            "sharpe_ratio": None, "calmar_ratio": None,
            "total_trades": None,
        }
        if summary:
            row["cagr"] = summary.get("cagr")
            row["max_drawdown"] = summary.get("max_drawdown")
            row["sharpe_ratio"] = summary.get("sharpe_ratio")
            row["calmar_ratio"] = summary.get("calmar_ratio")
            row["total_trades"] = summary.get("total_trades")

        if entry["status"] == "RETIRED":
            retired_rows.append(row)
        else:
            active_rows.append(row)

    # Sort active by Calmar descending
    active_rows.sort(key=lambda r: r["calmar_ratio"] or -1e9, reverse=True)
    retired_rows.sort(key=lambda r: r["calmar_ratio"] or -1e9, reverse=True)

    lines = []
    lines.append("# Strategy Leaderboard\n")
    lines.append(f"_Auto-generated by `scripts/rebuild_leaderboard.py` on {now}._\n")
    lines.append("Rebuild: `python3 scripts/rebuild_leaderboard.py`\n")
    lines.append("---\n")

    # Active table
    lines.append("## Active strategies (ranked by Calmar)\n")
    lines.append("| Rank | Strategy | CAGR | MDD | Sharpe | Calmar | Trades | Config | Notes |")
    lines.append("|---:|---|---:|---:|---:|---:|---:|---|---|")
    for i, r in enumerate(active_rows, 1):
        cagr = f"{r['cagr']*100:.2f}%" if r['cagr'] else "—"
        mdd = f"{r['max_drawdown']*100:.2f}%" if r['max_drawdown'] else "—"
        sharpe = f"{r['sharpe_ratio']:.3f}" if r['sharpe_ratio'] else "—"
        calmar = f"{r['calmar_ratio']:.3f}" if r['calmar_ratio'] else "—"
        trades = str(r['total_trades']) if r['total_trades'] else "—"
        cfg = f"`{r['config']}`" if r['config'] else "—"
        status_tag = " **[V]**" if r['status'] == "VARIANT" else ""
        lines.append(
            f"| {i} | {r['name']}{status_tag} | {cagr} | {mdd} | "
            f"{sharpe} | {calmar} | {trades} | {cfg} | {r['notes']} |"
        )
    lines.append("")

    # Retired table
    lines.append("## Retired strategies\n")
    lines.append("These were tested and found unviable (low CAGR, broken logic, "
                 "or superseded by a better variant).\n")
    lines.append("| Strategy | CAGR | MDD | Calmar | Notes |")
    lines.append("|---|---:|---:|---:|---|")
    for r in retired_rows:
        cagr = f"{r['cagr']*100:.2f}%" if r['cagr'] else "—"
        mdd = f"{r['max_drawdown']*100:.2f}%" if r['max_drawdown'] else "—"
        calmar = f"{r['calmar_ratio']:.3f}" if r['calmar_ratio'] else "—"
        lines.append(f"| {r['name']} | {cagr} | {mdd} | {calmar} | {r['notes']} |")
    lines.append("")

    md = "\n".join(lines) + "\n"
    out_path = os.path.join(REPO_ROOT, "docs", "LEADERBOARD.md")
    with open(out_path, "w") as f:
        f.write(md)
    print(f"# wrote {out_path}")
    print(f"  Active: {len(active_rows)}, Retired: {len(retired_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

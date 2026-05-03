#!/usr/bin/env python3
"""Ensemble runner — combine N strategy equity curves into one portfolio.

Phase 1: weighted (fixed) combination, intersection alignment, no rebalancing.
Phases 2+ (rebalancing, risk-parity, drawdown attribution) live in this same
runner under future iterations; the core math is in lib/ensemble_curve.py.

Usage:
    python scripts/run_ensemble.py \\
        --ensemble strategies/ensembles/eod_lowpe_5050/config.yaml \\
        --output results/ensembles/eod_lowpe_5050.json

Ensemble YAML schema (Phase 1):

    ensemble:
      name: <ensemble_name>
      description: <free text>
      starting_capital: 10000000
      alignment: intersection      # only mode supported in Phase 1
      rebalance: none              # phase 2+: monthly | quarterly | annual
      weighting: fixed             # phase 3+: inverse_vol | risk_parity
      legs:
        - name: <leg name>
          weight: 0.5
          # one of:
          result_path: results/.../something.json
          rank: 1                  # default 1 (top config in sweep)
          # or:
          # params_match: {pe_max: 10, ...}
          # or:
          # config_path: strategies/.../config.yaml   # (rerun mode, future)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from lib.equity_curve import EquityCurve
from lib.metrics import compute_metrics_from_curve
from lib.ensemble_curve import (
    CONVEX_MODES,
    DEFAULT_MAX_WEIGHT_PER_LEG,
    DEFAULT_TARGET_VOL_ANNUAL,
    REBALANCE_PERIODS,
    WEIGHTING_MODES,
    align_curves,
    attribute_drawdown,
    build_ensemble_curve,
    compute_correlation_matrix,
    compute_leg_navs,
    load_equity_curve_from_result,
    resolve_weights,
    sharpe_sensitivity_2leg,
)


# ── Config loading + validation ───────────────────────────────────────────

def load_ensemble_config(path: str) -> dict:
    """Load and minimally validate an ensemble YAML."""
    with open(path) as f:
        raw = yaml.safe_load(f)
    if "ensemble" not in raw:
        raise ValueError(f"{path}: missing top-level 'ensemble' key")
    cfg = raw["ensemble"]

    required = ["name", "starting_capital", "legs"]
    for k in required:
        if k not in cfg:
            raise ValueError(f"{path}: missing required field 'ensemble.{k}'")

    cfg.setdefault("description", "")
    cfg.setdefault("alignment", "intersection")
    cfg.setdefault("rebalance", "none")
    cfg.setdefault("weighting", "fixed")
    cfg.setdefault("weight_lookback_days", None)
    cfg.setdefault("convex_max_weight_per_leg", DEFAULT_MAX_WEIGHT_PER_LEG)
    cfg.setdefault("convex_target_vol_annual", DEFAULT_TARGET_VOL_ANNUAL)

    if cfg["alignment"] != "intersection":
        raise NotImplementedError(
            f"{path}: alignment={cfg['alignment']!r} not supported "
            f"(only 'intersection')"
        )
    if cfg["rebalance"] not in REBALANCE_PERIODS:
        raise ValueError(
            f"{path}: rebalance={cfg['rebalance']!r} must be one of "
            f"{REBALANCE_PERIODS}"
        )
    if cfg["weighting"] not in WEIGHTING_MODES:
        raise ValueError(
            f"{path}: weighting={cfg['weighting']!r} must be one of "
            f"{WEIGHTING_MODES}"
        )

    legs = cfg["legs"]
    if not legs or len(legs) < 2:
        raise ValueError(f"{path}: need >= 2 legs (got {len(legs) if legs else 0})")
    weighting = cfg["weighting"]
    for i, leg in enumerate(legs):
        if "name" not in leg:
            raise ValueError(f"{path}: legs[{i}] missing 'name'")
        if weighting == "fixed" and "weight" not in leg:
            raise ValueError(
                f"{path}: legs[{i}] missing 'weight' (required when "
                f"weighting=fixed)"
            )
        if "result_path" not in leg and "config_path" not in leg:
            raise ValueError(
                f"{path}: legs[{i}] needs 'result_path' or 'config_path'"
            )
        if "config_path" in leg and "result_path" not in leg:
            raise NotImplementedError(
                f"{path}: legs[{i}] 'config_path' (rerun mode) is Phase 2+; "
                f"specify 'result_path' to a pre-computed result JSON"
            )

    if weighting == "fixed":
        total_w = sum(leg["weight"] for leg in legs)
        if abs(total_w - 1.0) > 1e-6:
            raise ValueError(
                f"{path}: leg weights sum to {total_w:.6f}, expected 1.0"
            )

    return cfg


def resolve_leg_curve(leg: dict) -> tuple[EquityCurve, dict]:
    """Load the equity curve + summary for one leg."""
    rp = leg["result_path"]
    if not os.path.isabs(rp):
        rp = os.path.join(PROJECT_ROOT, rp)
    rank = int(leg.get("rank", 1))
    params_match = leg.get("params_match")
    return load_equity_curve_from_result(rp, rank=rank, params_match=params_match)


# ── Output formatting ─────────────────────────────────────────────────────

def _pct(v, d=2):
    return f"{v * 100:>+8.{d}f}%" if v is not None else "    N/A "


def _num(v, d=3):
    return f"{v:>8.{d}f}" if v is not None else "    N/A"


def print_summary(cfg: dict, leg_meta: list[dict], weights: list[float],
                  ensemble_summary: dict, window_start: str, window_end: str,
                  n_points: int) -> None:
    print()
    print("=" * 78)
    print(f"  Ensemble: {cfg['name']}")
    if cfg.get("description"):
        print(f"  {cfg['description']}")
    weighting_note = (
        f"weighting={cfg['weighting']}"
        + (f" (lookback={cfg['weight_lookback_days']}d)"
           if cfg.get("weight_lookback_days") is not None else "")
    )
    print(f"  Window: {window_start} -> {window_end}  ({n_points} points, "
          f"alignment={cfg['alignment']}, rebalance={cfg['rebalance']}, "
          f"{weighting_note})")
    print("=" * 78)
    print(f"  {'Leg':<28} {'Wgt':>5} {'CAGR':>9} {'MDD':>9} "
          f"{'Cal':>7} {'Sharpe':>8}")
    print("  " + "-" * 70)
    for leg, meta, w in zip(cfg["legs"], leg_meta, weights):
        s = meta["summary"]
        print(f"  {leg['name'][:28]:<28} {w:>5.2f} "
              f"{_pct(s.get('cagr'))} {_pct(s.get('max_drawdown'))} "
              f"{_num(s.get('calmar_ratio'), 3)} "
              f"{_num(s.get('sharpe_ratio'), 3)}")
    print("  " + "-" * 70)
    es = ensemble_summary
    print(f"  {'ENSEMBLE':<28} {'1.00':>5} "
          f"{_pct(es.get('cagr'))} {_pct(es.get('max_drawdown'))} "
          f"{_num(es.get('calmar_ratio'), 3)} "
          f"{_num(es.get('sharpe_ratio'), 3)}")
    print("=" * 78)
    vol = es.get("annualized_volatility")
    sh_arith = es.get("sharpe_ratio_arithmetic")
    print(f"  Vol: {vol*100:.2f}%   "
          f"Sharpe(arith): {_num(sh_arith, 3).strip()}   "
          f"Sortino: {_num(es.get('sortino_ratio'), 3).strip()}   "
          f"WorstYear: {_pct(es.get('worst_year'))}".rstrip())
    print()


# ── Result JSON shape (ensemble) ──────────────────────────────────────────

def print_drawdown_attribution(attr: dict) -> None:
    if not attr or not attr.get("legs"):
        return
    print(f"  Worst drawdown: {attr['ensemble_drawdown']*100:+.2f}%  "
          f"({attr['peak_date']} -> {attr['trough_date']}, "
          f"{attr['duration_days']}d)")
    print(f"  {'Leg':<28} {'Peak NAV':>14} {'Trough NAV':>14} "
          f"{'Return':>9} {'DD share':>10}")
    print("  " + "-" * 78)
    for leg in attr["legs"]:
        print(f"  {leg['name'][:28]:<28} {leg['nav_at_peak']:>14,.0f} "
              f"{leg['nav_at_trough']:>14,.0f} "
              f"{leg['leg_return']*100:>+8.2f}% "
              f"{leg['contribution_to_dd']*100:>+9.2f}%")
    print()


def print_diagnostics(diag: dict, leg_names: list[str]) -> None:
    corr = diag.get("correlation_matrix", {}).get("matrix", [])
    if corr:
        print("  Correlation matrix (daily returns):")
        # Header
        header = "  " + " " * 28 + "  ".join(f"{n[:10]:>10}" for n in leg_names)
        print(header)
        for i, row in enumerate(corr):
            cells = "  ".join(f"{c:>+10.3f}" for c in row)
            print(f"  {leg_names[i][:28]:<28}{cells}")
        print()

    sens = diag.get("sharpe_sensitivity")
    if sens:
        peak = sens["peak_weights"]
        iv = sens["inverse_vol_weights"]
        print(f"  Sharpe sensitivity (2-leg sweep, n={len(sens['grid'])}):")
        print(f"    Max Sharpe       = {sens['peak_sharpe']:.3f}  at w={peak}")
        print(f"    Inverse-vol      = {sens['inverse_vol_sharpe']:.3f}  at w={iv}")
        # Print a small ASCII chart of the grid
        print(f"    {'w1':>5}  {'CAGR':>7}  {'Vol':>7}  {'Sharpe':>7}")
        for row in sens["grid"]:
            cagr = row.get("cagr") or 0
            vol = row.get("vol") or 0
            sh = row.get("sharpe") or 0
            marker = " <- peak" if [row["w1"], row["w2"]] == peak else ""
            print(f"    {row['w1']:>5.2f}  {cagr*100:>+6.2f}%  "
                  f"{vol*100:>6.2f}%  {sh:>+7.3f}{marker}")
        print()


def build_output(cfg: dict, ensemble_curve: EquityCurve,
                 leg_meta: list[dict], weights: list[float],
                 metrics: dict, dd_attribution: dict, diagnostics: dict) -> dict:
    """Build a result JSON for the ensemble.

    Shape is intentionally distinct from BacktestResult.to_dict() (no per-trade
    list, no instrument), but uses identical field names where they map. The
    `type: "ensemble"` discriminator lets downstream tooling branch on it.
    """
    epochs = ensemble_curve.epochs
    values = ensemble_curve.values
    eq_series = [
        {"epoch": int(e), "value": round(v, 2)}
        for e, v in zip(epochs, values)
    ]

    summary = dict(metrics["portfolio"])
    summary["final_value"] = round(values[-1], 2)
    summary["peak_value"] = round(max(values), 2)

    return {
        "version": "1.0",
        "type": "ensemble",
        "ensemble": {
            "name": cfg["name"],
            "description": cfg.get("description", ""),
            "starting_capital": cfg["starting_capital"],
            "alignment": cfg["alignment"],
            "rebalance": cfg["rebalance"],
            "weighting": cfg["weighting"],
            "weight_lookback_days": cfg.get("weight_lookback_days"),
            "convex_max_weight_per_leg": cfg.get("convex_max_weight_per_leg"),
            "convex_target_vol_annual": cfg.get("convex_target_vol_annual"),
            "legs": [
                {
                    "name": leg["name"],
                    "weight": w,
                    "config_weight": leg.get("weight"),
                    "result_path": leg.get("result_path"),
                    "rank": leg.get("rank", 1),
                    "params_match": leg.get("params_match"),
                    "leg_summary": meta["summary"],
                }
                for leg, meta, w in zip(cfg["legs"], leg_meta, weights)
            ],
        },
        "equity_curve_frequency": ensemble_curve.frequency.name,
        "summary": summary,
        "drawdown_attribution": dd_attribution,
        "diagnostics": diagnostics,
        "equity_curve": eq_series,
        "warnings": _warnings(cfg),
    }


def _warnings(cfg: dict) -> list[str]:
    out = [
        "Ensemble starting_capital is purely notional. Each leg is rescaled "
        "as a return stream; the underlying leg backtest's own capital is "
        "ignored.",
    ]
    if cfg["rebalance"] == "none":
        out.append(
            "rebalance=none: set-and-forget combination. Winning leg's "
            "effective weight grows over time. Use monthly|quarterly|annual "
            "for honest live-deployment numbers."
        )
    else:
        out.append(
            f"rebalance={cfg['rebalance']}: rebalancing is FRICTIONLESS in "
            f"this runner. Real-world rebalance adds ~5-10bps per turn (not "
            f"modeled). Subtract ~{_friction_bps(cfg['rebalance'])}bps/yr "
            f"from realized CAGR for live-deployment estimates."
        )
    if cfg["weighting"] in CONVEX_MODES:
        out.append(
            f"weighting={cfg['weighting']}: weights are recomputed at each "
            f"rebalance from the trailing {cfg.get('weight_lookback_days', 252)}d "
            f"window via convex QP. The first ~{(cfg.get('weight_lookback_days') or 252)//252}+ "
            f"year(s) are warm-up (equal weights) — exclude that period for "
            f"honest forward metrics. Per-leg drawdown attribution below uses "
            f"the INITIAL weights (equal), not the time-varying convex weights, "
            f"so per-leg DD shares are approximate."
        )
    if cfg["weighting"] in ("inverse_vol", "risk_parity"):
        if cfg.get("weight_lookback_days") is None:
            out.append(
                f"weighting={cfg['weighting']} with full-window lookback: "
                f"weights are IN-SAMPLE (computed from the full backtest "
                f"vol). For honest forward estimates, set "
                f"weight_lookback_days (e.g. 365) and exclude the lookback "
                f"period from evaluation, or build per-rebalance adaptive "
                f"weights (Phase 3.5)."
            )
        else:
            out.append(
                f"weighting={cfg['weighting']} with lookback="
                f"{cfg['weight_lookback_days']}d: weights are computed once "
                f"from the trailing window, then held fixed for the entire "
                f"backtest. Per-rebalance adaptive weights are Phase 3.5."
            )
    return out


def _friction_bps(period: str) -> int:
    """Rough friction estimate in bps/yr for each rebalance frequency."""
    return {"monthly": 70, "quarterly": 25, "annual": 7}.get(period, 0)


# ── Main ──────────────────────────────────────────────────────────────────

def run_ensemble(cfg_path: str, output_path: str | None = None) -> dict:
    cfg = load_ensemble_config(cfg_path)

    # Resolve every leg curve
    curves: list[EquityCurve] = []
    leg_meta: list[dict] = []
    for leg in cfg["legs"]:
        curve, summary = resolve_leg_curve(leg)
        curves.append(curve)
        leg_meta.append({"summary": summary})

    # Resolve weights (fixed or computed)
    weights = resolve_weights(
        cfg["legs"], curves,
        weighting=cfg["weighting"],
        lookback_days=cfg.get("weight_lookback_days"),
    )

    # Build ensemble (with optional adaptive or convex per-rebalance weighting)
    is_adaptive = cfg["weighting"] == "inverse_vol_adaptive"
    is_convex = cfg["weighting"] in CONVEX_MODES
    if (is_adaptive or is_convex) and cfg["rebalance"] == "none":
        raise ValueError(
            f"{cfg_path}: weighting={cfg['weighting']} requires "
            f"rebalance != 'none' (got 'none')"
        )
    adaptive_lookback = cfg.get("weight_lookback_days") or 252
    ensemble_curve = build_ensemble_curve(
        curves, weights, cfg["starting_capital"],
        mode=cfg["alignment"],
        rebalance=cfg["rebalance"],
        adaptive=is_adaptive,
        adaptive_lookback_days=adaptive_lookback,
        convex_mode=cfg["weighting"] if is_convex else None,
        convex_max_weight_per_leg=float(cfg.get("convex_max_weight_per_leg",
                                                DEFAULT_MAX_WEIGHT_PER_LEG)),
        convex_target_vol_annual=float(cfg.get("convex_target_vol_annual",
                                               DEFAULT_TARGET_VOL_ANNUAL)),
    )

    # Drawdown attribution: re-walk to recover per-leg NAVs
    common_epochs, aligned = align_curves(curves, mode=cfg["alignment"])
    leg_navs = compute_leg_navs(
        common_epochs, aligned, weights, cfg["starting_capital"],
        period=cfg["rebalance"],
    )
    leg_names = [leg["name"] for leg in cfg["legs"]]
    dd_attribution = attribute_drawdown(common_epochs, leg_navs, leg_names)

    # Diagnostics: correlation matrix + (2-leg only) Sharpe sensitivity
    corr_matrix = compute_correlation_matrix(curves, mode=cfg["alignment"])
    diagnostics: dict = {
        "correlation_matrix": {
            "labels": leg_names,
            "matrix": corr_matrix,
        },
    }
    if len(curves) == 2:
        diagnostics["sharpe_sensitivity"] = sharpe_sensitivity_2leg(
            curves, cfg["starting_capital"], rebalance=cfg["rebalance"],
        )

    # Metrics (no benchmark in Phase 1)
    metrics = compute_metrics_from_curve(ensemble_curve)

    # Compute window labels for stdout
    from datetime import datetime, timezone
    window_start = datetime.fromtimestamp(
        ensemble_curve.epochs[0], tz=timezone.utc
    ).strftime("%Y-%m-%d")
    window_end = datetime.fromtimestamp(
        ensemble_curve.epochs[-1], tz=timezone.utc
    ).strftime("%Y-%m-%d")

    # Augment summary with worst_year (compute_metrics_from_curve doesn't
    # include it; we approximate from yearly returns)
    metrics["portfolio"]["worst_year"] = _worst_year(ensemble_curve)

    print_summary(cfg, leg_meta, weights, metrics["portfolio"],
                  window_start, window_end, len(ensemble_curve))
    print_drawdown_attribution(dd_attribution)
    print_diagnostics(diagnostics, leg_names)

    output = build_output(cfg, ensemble_curve, leg_meta, weights, metrics,
                          dd_attribution, diagnostics)

    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        size_kb = os.path.getsize(output_path) / 1024
        print(f"  Saved {output_path} ({size_kb:.0f} KB)")

    return output


def _worst_year(curve: EquityCurve) -> float | None:
    """Compute worst calendar-year return from an ensemble curve."""
    from datetime import datetime, timezone
    if len(curve) < 2:
        return None
    yearly: dict[int, dict] = {}
    for epoch, value in zip(curve.epochs, curve.values):
        yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": value, "last": value}
        yearly[yr]["last"] = value
    sorted_years = sorted(yearly.keys())
    rets = []
    for i, yr in enumerate(sorted_years):
        if i == 0:
            base = yearly[yr]["first"]
        else:
            base = yearly[sorted_years[i - 1]]["last"]
        if base > 0:
            rets.append(yearly[yr]["last"] / base - 1)
    return min(rets) if rets else None


def main():
    parser = argparse.ArgumentParser(description="Run a strategy ensemble")
    parser.add_argument("--ensemble", required=True,
                        help="Path to ensemble YAML config")
    parser.add_argument("--output", default=None,
                        help="Path to write result JSON (default: skip)")
    args = parser.parse_args()
    run_ensemble(args.ensemble, args.output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Phase 8A — bias impact measurement harness.

Runs a single strategy TWICE against the same data:
  - Pass 1: legacy flag (biased, default)
  - Pass 2: honest flag (bias-fixed, opt-in)

Emits a delta report: CAGR, Calmar, MDD, total_trades, total_return.

Usage:
  python scripts/measure_bias_impact.py <strategy> <config_path> [--provider parquet|cr]

The `strategy` argument must be one of:
  - momentum_rebalance   (flag: moc_signal_lag_days 0 vs 1)
  - momentum_top_gainers (flag: universe_mode full_period vs point_in_time)
  - momentum_dip_quality (flag: universe_mode full_period vs point_in_time)

With `--provider parquet` (default), uses ~/ATO_DATA/tick_data local kite
parquet (NSE only, 2019-2021 in the dev fixture). For a full run, use
`--provider cr` which hits the Ceta Research API (requires CR_API_KEY).

Output: docs/archive/audit-2026-04/audit_phase_8a/{strategy}.md with a delta table.
"""

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from engine.config_loader import load_config
from engine.pipeline import run_pipeline


FLAG_BY_STRATEGY = {
    "momentum_rebalance": {
        "path": ("entry", "moc_signal_lag_days"),
        "legacy": [0],
        "honest": [1],
    },
    "momentum_top_gainers": {
        "path": ("entry", "universe_mode"),
        "legacy": ["full_period"],
        "honest": ["point_in_time"],
    },
    "momentum_dip_quality": {
        "path": ("entry", "universe_mode"),
        "legacy": ["full_period"],
        "honest": ["point_in_time"],
    },
}


def _set_nested(raw_yaml: dict, path: tuple, value):
    """Set raw_yaml[path[0]][path[1]] = value, creating intermediate keys."""
    d = raw_yaml
    for k in path[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[path[-1]] = value


def _build_config_with_flag(config_path: str, flag_path: tuple, flag_value) -> str:
    """Copy the YAML config, set the flag at flag_path to flag_value, and
    write to a temp file. Return the temp path."""
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    _set_nested(raw, flag_path, flag_value)

    tmp_dir = Path("/tmp/phase_8a_configs")
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{Path(config_path).stem}_{flag_path[-1]}_{flag_value}.yaml"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f)
    return str(tmp_path)


def _summarize_result(result: dict) -> dict:
    """Extract the headline metrics from a single-config result dict."""
    summary = result.get("summary", {})
    return {
        "cagr": summary.get("cagr"),
        "total_return": summary.get("total_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "calmar_ratio": summary.get("calmar_ratio"),
        "sharpe_ratio": summary.get("sharpe_ratio"),
        "total_trades": summary.get("total_trades"),
        "win_rate": summary.get("win_rate"),
    }


def _make_provider(kind: str):
    if kind == "parquet":
        from engine.data_provider import ParquetDataProvider
        return ParquetDataProvider(base_path=os.path.expanduser("~/ATO_DATA/tick_data"))
    if kind == "cr":
        from engine.data_provider import CRDataProvider
        # format="parquet" is required — default JSON hits a 100MB
        # artifact limit on full-universe / multi-year queries.
        return CRDataProvider(format="parquet")
    if kind == "nse_charting":
        from engine.data_provider import NseChartingDataProvider
        return NseChartingDataProvider()
    raise ValueError(f"Unknown provider kind: {kind}")


def measure(strategy: str, config_path: str, provider_kind: str) -> dict:
    if strategy not in FLAG_BY_STRATEGY:
        raise ValueError(f"Unknown strategy: {strategy}. Choices: {list(FLAG_BY_STRATEGY)}")
    spec = FLAG_BY_STRATEGY[strategy]

    provider = _make_provider(provider_kind)

    # Pass 1 — legacy
    t0 = time.time()
    legacy_cfg = _build_config_with_flag(config_path, spec["path"], spec["legacy"])
    print(f"\n=== LEGACY pass: {spec['path'][-1]}={spec['legacy']} ===")
    legacy_results = run_pipeline(legacy_cfg, data_provider=provider)
    legacy_elapsed = time.time() - t0

    # Pass 2 — honest
    t0 = time.time()
    honest_cfg = _build_config_with_flag(config_path, spec["path"], spec["honest"])
    print(f"\n=== HONEST pass: {spec['path'][-1]}={spec['honest']} ===")
    honest_results = run_pipeline(honest_cfg, data_provider=provider)
    honest_elapsed = time.time() - t0

    # Pick the TOP config from each pass by CAGR for a single apples-to-apples
    # comparison. SweepResult.configs is list of (config_dict, BacktestResult).
    def _top(sweep):
        tuples = getattr(sweep, "configs", None) or []
        if not tuples:
            return None
        materialized = []
        for cfg_dict, br in tuples:
            d = br.to_dict() if hasattr(br, "to_dict") else {}
            materialized.append(d)
        materialized.sort(
            key=lambda r: (r.get("summary", {}).get("cagr") or float("-inf")),
            reverse=True,
        )
        return materialized[0]

    legacy_top = _top(legacy_results)
    honest_top = _top(honest_results)

    return {
        "strategy": strategy,
        "config": config_path,
        "provider": provider_kind,
        "flag_path": ".".join(spec["path"]),
        "legacy": {
            "value": spec["legacy"][0],
            "elapsed_sec": round(legacy_elapsed, 1),
            "summary": _summarize_result(legacy_top) if legacy_top else None,
        },
        "honest": {
            "value": spec["honest"][0],
            "elapsed_sec": round(honest_elapsed, 1),
            "summary": _summarize_result(honest_top) if honest_top else None,
        },
    }


def write_report(report: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{report['strategy']}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    md_path = os.path.join(out_dir, f"{report['strategy']}.md")
    leg = report["legacy"]["summary"] or {}
    hon = report["honest"]["summary"] or {}

    def _fmt(x, pct=False):
        if x is None:
            return "N/A"
        if pct:
            return f"{x * 100:+.2f}%"
        return f"{x:+.4f}"

    def _delta(key, pct=False):
        lv = leg.get(key)
        hv = hon.get(key)
        if lv is None or hv is None:
            return "N/A"
        d = hv - lv
        if pct:
            return f"{d * 100:+.2f}pp"
        return f"{d:+.4f}"

    lines = [
        f"# Phase 8A Bias Impact — {report['strategy']}",
        "",
        f"- **Config:** `{report['config']}`",
        f"- **Data provider:** `{report['provider']}`",
        f"- **Flag:** `{report['flag_path']}` (legacy={report['legacy']['value']}, honest={report['honest']['value']})",
        f"- **Pipeline times:** legacy {report['legacy']['elapsed_sec']}s · honest {report['honest']['elapsed_sec']}s",
        "",
        "| Metric | Legacy | Honest | Delta |",
        "|--------|-------:|-------:|------:|",
        f"| CAGR | {_fmt(leg.get('cagr'), pct=True)} | {_fmt(hon.get('cagr'), pct=True)} | {_delta('cagr', pct=True)} |",
        f"| Total Return | {_fmt(leg.get('total_return'), pct=True)} | {_fmt(hon.get('total_return'), pct=True)} | {_delta('total_return', pct=True)} |",
        f"| Max Drawdown | {_fmt(leg.get('max_drawdown'), pct=True)} | {_fmt(hon.get('max_drawdown'), pct=True)} | {_delta('max_drawdown', pct=True)} |",
        f"| Calmar | {_fmt(leg.get('calmar_ratio'))} | {_fmt(hon.get('calmar_ratio'))} | {_delta('calmar_ratio')} |",
        f"| Sharpe | {_fmt(leg.get('sharpe_ratio'))} | {_fmt(hon.get('sharpe_ratio'))} | {_delta('sharpe_ratio')} |",
        f"| Total trades | {leg.get('total_trades')} | {hon.get('total_trades')} | — |",
        f"| Win rate | {_fmt(leg.get('win_rate'), pct=True)} | {_fmt(hon.get('win_rate'), pct=True)} | {_delta('win_rate', pct=True)} |",
        "",
        "## Decision guide",
        "",
        "- `|ΔCAGR| < 2pp`: bias is cosmetic. Flip default to `honest` and",
        "  re-run optimization once; move on.",
        "- `|ΔCAGR| 2-5pp`: meaningful. Fix + re-run optimization (Rounds 2+3).",
        "- `|ΔCAGR| > 5pp`: the strategy was mostly bias. Retire or invert.",
    ]
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return md_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("strategy", choices=list(FLAG_BY_STRATEGY.keys()))
    p.add_argument("config_path")
    p.add_argument("--provider", default="parquet",
                   choices=["parquet", "cr", "nse_charting"])
    p.add_argument("--out-dir", default="docs/archive/audit-2026-04/audit_phase_8a")
    args = p.parse_args()

    report = measure(args.strategy, args.config_path, args.provider)
    out_path = write_report(report, args.out_dir)
    print(f"\n✓ Report written: {out_path}")


if __name__ == "__main__":
    main()

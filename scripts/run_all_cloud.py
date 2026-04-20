#!/usr/bin/env python3
"""Run ALL EOD strategies on CR cloud compute, one after another.

Optimized for rate limits: uploads all code + configs once at startup,
then per strategy only uploads a tiny wrapper (1 API call) + submits run.

Usage:
    python scripts/run_all_cloud.py
    python scripts/run_all_cloud.py --resume
    python scripts/run_all_cloud.py --strategy earnings_dip
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch
from lib.cloud_orchestrator import CloudOrchestrator

# One representative config per strategy
STRATEGY_CONFIGS = [
    # --- Priority 1: Newly ported strategies ---
    ("momentum_dip_quality", "strategies/momentum_dip_quality/config_nse_sweep.yaml", 64),
    ("forced_selling_dip", "strategies/forced_selling_dip/config_nse.yaml", 36),
    ("earnings_dip", "strategies/earnings_dip/config_nse.yaml", 54),
    ("quality_dip_tiered", "strategies/quality_dip_tiered/config_nse.yaml", 24),
    ("momentum_rebalance", "strategies/momentum_rebalance/config_nse.yaml", 27),
    ("index_breakout", "strategies/index_breakout/config_nse.yaml", 16),

    # --- Priority 2: Existing dip-buy / quality strategies ---
    ("quality_dip_buy", "strategies/quality_dip_buy/config_nse.yaml", 96),
    ("low_pe", "strategies/low_pe/config_nse.yaml", 1),

    # --- Priority 3: EOD technical + mean-reversion ---
    ("eod_technical", "strategies/eod_technical/config.yaml", 4),
    ("bb_mean_reversion", "strategies/bb_mean_reversion/config_nse_fmp.yaml", 3),
    ("extended_ibs", "strategies/extended_ibs/config_nse_fmp.yaml", 12),
    ("connors_rsi", "strategies/connors_rsi/config.yaml", 8),
    ("ibs_mean_reversion", "strategies/ibs_mean_reversion/config.yaml", 8),

    # --- Priority 4: Trend following / breakout ---
    ("darvas_box", "strategies/darvas_box/config.yaml", 16),
    ("momentum_dip", "strategies/momentum_dip/config_nse_native.yaml", 16),
    ("momentum_cascade", "strategies/momentum_cascade/config_no_ranking.yaml", 48),
    ("squeeze", "strategies/squeeze/config.yaml", 8),
    ("swing_master", "strategies/swing_master/config.yaml", 4),

    # --- Priority 5: Index-level strategies ---
    ("index_dip_buy", "strategies/index_dip_buy/config_nse_native.yaml", 24),
    ("index_green_candle", "strategies/index_green_candle/config_nse_native.yaml", 1),
    ("index_sma_crossover", "strategies/index_sma_crossover/config_nse_native.yaml", 18),

    # --- Priority 6: Other ---
    ("gap_fill", "strategies/gap_fill/config.yaml", 4),
    ("holp_lohp", "strategies/holp_lohp/config.yaml", 4),
    ("overnight_hold", "strategies/overnight_hold/config.yaml", 8),
    ("trending_value", "strategies/trending_value/config.yaml", 16),
    ("factor_composite", "strategies/factor_composite/config.yaml", 4),
]


def run_strategy(orch, project_id, strategy_name, cpu, ram, timeout):
    """Upload wrapper pointing to pre-uploaded config, run, poll, download."""
    wrapper = orch.make_wrapper(
        "cloud_main_eod.py",
        config_file=f"config_{strategy_name}.yaml",
    )
    orch.upsert_with_retry(project_id, "_run_1.py", wrapper)

    print(f"  Submitting (cpu={cpu}, ram={ram}MB, timeout={timeout}s)...")
    run_id = orch.submit_run(project_id, "_run_1.py", cpu=cpu, ram_mb=ram, timeout=timeout)

    def progress(elapsed, status, last_line):
        print(f"  [{elapsed}s] {status} | {last_line[:120]}")

    result = orch.poll_run(project_id, run_id, timeout=timeout, poll_interval=60,
                           on_progress=progress)

    status = result.get("status", "unknown")
    if status != "completed":
        stderr = result.get("stderr", "")
        print(f"  FAILED ({status})")
        if stderr:
            print(f"  Stderr: {stderr[-500:]}")
        return None, status

    try:
        results = orch.download_results(run_id)
        return results, "completed"
    except Exception as e:
        print(f"  Download failed: {e}")
        return None, "download_failed"


def save_results(strategy_name, results, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(output_dir, f"engine_{strategy_name}_{date_str}.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    return path


def print_leaderboard(all_strategy_results):
    print(f"\n{'='*120}")
    print(f"MASTER LEADERBOARD (best config per strategy, ranked by Calmar)")
    print(f"{'='*120}")
    print(f"{'Strategy':<25} {'Config':<45} {'CAGR':>7} {'MaxDD':>7} {'Calmar':>7} {'Sharpe':>7} {'Trades':>7}")
    print(f"{'-'*25} {'-'*45} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")

    rows = []
    for strategy_name, results in all_strategy_results.items():
        if not results:
            continue
        sorted_r = sorted(results, key=lambda r: r.get("calmar_ratio") or 0, reverse=True)
        best = sorted_r[0]
        rows.append((
            strategy_name,
            best.get("config_id", "?")[:45],
            (best.get("cagr") or 0) * 100,
            (best.get("max_drawdown") or 0) * 100,
            best.get("calmar_ratio") or 0,
            best.get("sharpe_ratio") or 0,
            best.get("total_trades") or 0,
        ))

    rows.sort(key=lambda r: r[4], reverse=True)  # sort by calmar

    for name, config, cagr, dd, calmar, sharpe, trades in rows:
        print(f"{name:<25} {config:<45} {cagr:>6.1f}% {dd:>6.1f}% {calmar:>7.2f} {sharpe:>7.2f} {trades:>7}")


def main():
    parser = argparse.ArgumentParser(description="Run all EOD strategies on CR cloud")
    parser.add_argument("--resume", action="store_true",
                        help="Skip strategies with existing results from today")
    parser.add_argument("--strategy", type=str, help="Run only this strategy")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="Per-strategy timeout in seconds (default: 7200)")
    parser.add_argument("--cpu", type=int, default=8, help="CPU count (default: 8)")
    parser.add_argument("--ram", type=int, default=61440, help="RAM in MB (default: 61440)")
    parser.add_argument("--skip-code-upload", action="store_true",
                        help="Skip uploading engine code (if already uploaded)")
    args = parser.parse_args()

    output_dir = os.path.join(ROOT, "results", "engine_sweep")
    date_str = datetime.now().strftime("%Y-%m-%d")

    configs = list(STRATEGY_CONFIGS)
    if args.strategy:
        configs = [(n, p, c) for n, p, c in configs if n == args.strategy]
        if not configs:
            print(f"Strategy '{args.strategy}' not found. Available:")
            for n, _, _ in STRATEGY_CONFIGS:
                print(f"  {n}")
            sys.exit(1)

    if args.resume:
        remaining = []
        for name, path, count in configs:
            result_path = os.path.join(output_dir, f"engine_{name}_{date_str}.json")
            if os.path.exists(result_path):
                print(f"  SKIP (done): {name}")
            else:
                remaining.append((name, path, count))
        configs = remaining

    total_configs = sum(c for _, _, c in configs)
    print(f"\n{'='*80}")
    print(f"STRATEGY SWEEP: {len(configs)} strategies, ~{total_configs} total configs")
    print(f"Resources: {args.cpu} CPU, {args.ram}MB RAM, {args.timeout}s timeout")
    print(f"Output: {output_dir}")
    print(f"{'='*80}\n")

    cr = CetaResearch()
    orch = CloudOrchestrator(
        cr, project_name="sb-eod-sweep-v2",
        dependencies=["requests", "pyyaml", "polars==1.37.1", "pyarrow"],
    )

    print("Setting up cloud project...")
    project = orch.find_or_create_project(
        entrypoint="cloud_main_eod.py",
        description="EOD strategy sweep (all strategies)",
    )
    project_id = project["id"]

    if not args.skip_code_upload:
        file_paths = orch.discover_files("eod")
        orch.sync_files(project_id, file_paths, force=True)

        # Upload cloud entry point
        orch.upsert_with_retry(
            project_id, "cloud_main_eod.py",
            open(os.path.join(ROOT, "scripts/cloud_main_eod.py")).read(),
        )

    # Upload ALL strategy configs with unique names
    print(f"  Uploading {len(configs)} strategy configs...")
    for name, config_path, _ in configs:
        full = os.path.join(ROOT, config_path)
        if os.path.exists(full):
            orch.upsert_with_retry(project_id, f"config_{name}.yaml", open(full).read())
    print(f"  Configs: {len(configs)} uploaded")
    print()

    all_strategy_results = {}
    completed = 0
    failed = 0

    for i, (strategy_name, config_path, expected_configs) in enumerate(configs, 1):
        print(f"\n{'='*80}")
        print(f"[{i}/{len(configs)}] {strategy_name} (~{expected_configs} configs)")
        print(f"{'='*80}")

        start = time.time()
        results, status = run_strategy(
            orch, project_id, strategy_name,
            cpu=args.cpu, ram=args.ram, timeout=args.timeout,
        )
        elapsed = round(time.time() - start, 1)

        if results is not None and len(results) > 0:
            result_path = save_results(strategy_name, results, output_dir)
            all_strategy_results[strategy_name] = results

            sorted_r = sorted(results, key=lambda r: r.get("calmar_ratio") or 0, reverse=True)
            print(f"\n  Done: {len(results)} configs in {elapsed}s -> {result_path}")
            for r in sorted_r[:5]:
                cagr = (r.get("cagr") or 0) * 100
                dd = (r.get("max_drawdown") or 0) * 100
                calmar = r.get("calmar_ratio") or 0
                print(f"    {r.get('config_id','?')[:55]}: {cagr:.1f}% / {dd:.1f}% / {calmar:.2f}")
            completed += 1
        else:
            print(f"\n  FAILED ({status}) after {elapsed}s")
            failed += 1

    if all_strategy_results:
        print_leaderboard(all_strategy_results)

    summary_path = os.path.join(output_dir, f"master_leaderboard_{date_str}.json")
    summary = {}
    for name, results in all_strategy_results.items():
        if results:
            best = sorted(results, key=lambda r: r.get("calmar_ratio") or 0, reverse=True)[0]
            summary[name] = {
                "best_config": best.get("config_id"),
                "cagr": best.get("cagr"),
                "max_drawdown": best.get("max_drawdown"),
                "calmar_ratio": best.get("calmar_ratio"),
                "sharpe_ratio": best.get("sharpe_ratio"),
                "total_trades": best.get("total_trades"),
                "total_configs_tested": len(results),
            }
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nSummary: {summary_path}")
    print(f"\nDONE: {completed} completed, {failed} failed")


if __name__ == "__main__":
    main()

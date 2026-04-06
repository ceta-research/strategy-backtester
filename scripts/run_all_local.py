#!/usr/bin/env python3
"""Run ALL engine strategies locally with bhavcopy data, one after another.

Saves results per strategy and builds a master leaderboard at the end.

Usage:
    python scripts/run_all_local.py
    python scripts/run_all_local.py --resume
    python scripts/run_all_local.py --strategy earnings_dip
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.pipeline import run_pipeline
from engine.data_provider import BhavcopyDataProvider
from lib.backtest_result import SweepResult

# One representative config per strategy
# Format: (strategy_name, config_path, expected_configs)
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

OUTPUT_DIR = os.path.join(ROOT, "results", "engine_sweep")


def save_results(strategy_name, sweep_result):
    """Save SweepResult to JSON and return path."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(OUTPUT_DIR, f"engine_{strategy_name}_{date_str}.json")
    sweep_result.save(path)
    return path


def run_one_strategy(strategy_name, config_path):
    """Run a single strategy and return (SweepResult, elapsed_seconds)."""
    full_path = os.path.join(ROOT, config_path)
    if not os.path.exists(full_path):
        print(f"  ERROR: Config not found: {full_path}")
        return None, 0

    provider = BhavcopyDataProvider(
        turnover_threshold=70_000_000,
        price_threshold=50,
    )

    start = time.time()
    try:
        result = run_pipeline(full_path, data_provider=provider)
    except Exception as e:
        print(f"  ERROR: {e}")
        return None, round(time.time() - start, 1)

    elapsed = round(time.time() - start, 1)
    return result, elapsed


def print_leaderboard(all_results):
    """Print master leaderboard across all strategies."""
    print(f"\n{'='*120}")
    print(f"MASTER LEADERBOARD (best config per strategy, ranked by Calmar)")
    print(f"{'='*120}")
    print(f"{'Strategy':<25} {'Config':<45} {'CAGR':>7} {'MaxDD':>7} {'Calmar':>7} {'Sharpe':>7} {'Trades':>7}")
    print(f"{'-'*25} {'-'*45} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")

    rows = []
    for strategy_name, sweep in all_results.items():
        if not sweep or not sweep.configs:
            continue
        best_params, best_br = sweep._sorted("calmar_ratio")[0]
        s = best_br.to_dict().get("summary", {})
        rows.append((
            strategy_name,
            best_params.get("config_id", "?")[:45],
            (s.get("cagr") or 0) * 100,
            (s.get("max_drawdown") or 0) * 100,
            s.get("calmar_ratio") or 0,
            s.get("sharpe_ratio") or 0,
            s.get("total_trades") or 0,
        ))

    rows.sort(key=lambda r: r[4], reverse=True)

    for name, config, cagr, dd, calmar, sharpe, trades in rows:
        print(f"{name:<25} {config:<45} {cagr:>6.1f}% {dd:>6.1f}% {calmar:>7.2f} {sharpe:>7.2f} {trades:>7}")


def main():
    parser = argparse.ArgumentParser(description="Run all strategies locally with bhavcopy")
    parser.add_argument("--resume", action="store_true",
                        help="Skip strategies with existing results from today")
    parser.add_argument("--strategy", type=str, help="Run only this strategy")
    args = parser.parse_args()

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
            result_path = os.path.join(OUTPUT_DIR, f"engine_{name}_{date_str}.json")
            if os.path.exists(result_path):
                print(f"  SKIP (done): {name}")
            else:
                remaining.append((name, path, count))
        configs = remaining

    total_configs = sum(c for _, _, c in configs)
    print(f"\n{'='*80}")
    print(f"LOCAL SWEEP: {len(configs)} strategies, ~{total_configs} total configs")
    print(f"Data: BhavcopyDataProvider (native NSE, survivorship-bias-free)")
    print(f"Output: {OUTPUT_DIR}")
    print(f"{'='*80}\n")

    all_results = {}
    completed = 0
    failed = 0

    for i, (strategy_name, config_path, expected_configs) in enumerate(configs, 1):
        print(f"\n{'='*80}")
        print(f"[{i}/{len(configs)}] {strategy_name} (~{expected_configs} configs)")
        print(f"  Config: {config_path}")
        print(f"{'='*80}")

        sweep, elapsed = run_one_strategy(strategy_name, config_path)

        if sweep and isinstance(sweep, SweepResult) and sweep.configs:
            result_path = save_results(strategy_name, sweep)
            all_results[strategy_name] = sweep

            print(f"\n  Done: {len(sweep.configs)} configs in {elapsed}s")
            print(f"  Saved: {result_path}")
            sweep.print_leaderboard(top_n=5)
            completed += 1
        else:
            print(f"\n  FAILED after {elapsed}s")
            failed += 1

    if all_results:
        print_leaderboard(all_results)

    # Save master summary
    summary = {}
    for name, sweep in all_results.items():
        if sweep and sweep.configs:
            best_params, best_br = sweep._sorted("calmar_ratio")[0]
            s = best_br.to_dict().get("summary", {})
            summary[name] = {
                "best_config": best_params.get("config_id"),
                "cagr": s.get("cagr"),
                "max_drawdown": s.get("max_drawdown"),
                "calmar_ratio": s.get("calmar_ratio"),
                "sharpe_ratio": s.get("sharpe_ratio"),
                "total_trades": s.get("total_trades"),
                "total_configs_tested": len(sweep.configs),
            }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary_path = os.path.join(OUTPUT_DIR, f"master_leaderboard_{date_str}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nSummary: {summary_path}")
    print(f"\nDONE: {completed} completed, {failed} failed")


if __name__ == "__main__":
    main()

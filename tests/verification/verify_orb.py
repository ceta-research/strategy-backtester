#!/usr/bin/env python3
"""Cross-validate ORB intraday pipeline against nse_arena implementation.

Requires CR API key. Runs both implementations with the same default config
and compares trade count, CAGR, MaxDD, and Calmar ratio.

Usage:
    python tests/verification/verify_orb.py
"""

import os
import sys

# Add both repos to path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
backtests_root = os.path.join(os.path.dirname(project_root), "backtests")
sys.path.insert(0, project_root)
sys.path.insert(0, backtests_root)


def run_nse_arena():
    """Run ORB via nse_arena framework."""
    from cr_client import CetaResearch
    from nse_arena.orb import OpeningRangeBreakout
    from nse_arena.framework import run_strategy

    client = CetaResearch()
    strategy = OpeningRangeBreakout()
    result = run_strategy(client, strategy, verbose=True)
    return result


def run_strategy_backtester():
    """Run ORB via strategy-backtester intraday pipeline."""
    from engine.intraday_sql_builder import build_orb_sql
    from engine.intraday_simulator import simulate_intraday
    from lib.cr_client import CetaResearch
    from lib.metrics import compute_metrics

    client = CetaResearch()

    # Use same default config as nse_arena ORB
    cfg = {
        "start_date": "2020-01-06",
        "end_date": "2026-03-09",
        "min_volume": 5000000,
        "min_price": 100,
        "min_range_pct": 0.01,
        "or_window": 15,
        "max_entry_bar": 120,
        "target_pct": 0.015,
        "stop_pct": 0.01,
        "max_hold_bars": 60,
    }

    sql = build_orb_sql(cfg)
    trades = client.query(sql, memory_mb=16384, threads=6, timeout=600)
    sql_row_count = len(trades)

    sim_cfg = {
        "initial_capital": 500000,
        "max_positions": 5,
        "order_value": 50000,
    }
    sim_result = simulate_intraday(trades, sim_cfg)

    metrics = compute_metrics(
        sim_result["daily_returns"],
        sim_result["bench_returns"],
        periods_per_year=252,
        risk_free_rate=0.065,
    )

    port = metrics["portfolio"]
    return {
        "trades": sql_row_count,  # Raw SQL rows, same as nse_arena's len(trades)
        "active_days": len(sim_result["daily_returns"]),
        "cagr": port.get("cagr"),
        "max_dd": port.get("max_drawdown"),
        "calmar": port.get("calmar_ratio"),
        "sharpe": port.get("sharpe_ratio"),
    }


def compare(arena, sb):
    """Compare results from both implementations."""
    print("\n" + "=" * 70)
    print("  ORB Verification: nse_arena vs strategy-backtester")
    print("=" * 70)

    fields = [
        ("Trades", "trades", None),
        ("Active Days", "active_days", None),
        ("CAGR", "cagr", lambda x: f"{x * 100:.2f}%" if x else "N/A"),
        ("Max DD", "max_dd", lambda x: f"{x * 100:.2f}%" if x else "N/A"),
        ("Calmar", "calmar", lambda x: f"{x:.3f}" if x else "N/A"),
        ("Sharpe", "sharpe", lambda x: f"{x:.3f}" if x else "N/A"),
    ]

    print(f"  {'Metric':<15} {'nse_arena':>15} {'strategy-bt':>15} {'Match':>10}")
    print(f"  {'-' * 57}")

    all_match = True
    for label, key, fmt in fields:
        a_val = arena.get(key)
        s_val = sb.get(key)

        if fmt:
            a_str = fmt(a_val)
            s_str = fmt(s_val)
        else:
            a_str = str(a_val)
            s_str = str(s_val)

        if a_val is not None and s_val is not None:
            if isinstance(a_val, (int, float)) and isinstance(s_val, (int, float)):
                if a_val == 0 and s_val == 0:
                    match = True
                elif a_val != 0:
                    match = abs(a_val - s_val) / abs(a_val) < 0.001  # 0.1% tolerance
                else:
                    match = abs(s_val) < 0.001
            else:
                match = a_val == s_val
        else:
            match = a_val == s_val

        status = "OK" if match else "MISMATCH"
        if not match:
            all_match = False

        print(f"  {label:<15} {a_str:>15} {s_str:>15} {status:>10}")

    print("=" * 70)
    if all_match:
        print("  PASS: All metrics match within tolerance")
    else:
        print("  FAIL: Some metrics differ")
    print()
    return all_match


def main():
    print("Running nse_arena ORB (default config)...")
    arena_result = run_nse_arena()

    # Extract comparable fields from arena result
    arena = {
        "trades": arena_result.get("trades"),
        "active_days": arena_result.get("active_days"),
        "cagr": arena_result.get("cagr", 0) / 100 if arena_result.get("cagr") else None,
        "max_dd": arena_result.get("max_dd", 0) / 100 if arena_result.get("max_dd") else None,
        "calmar": arena_result.get("calmar"),
        "sharpe": arena_result.get("sharpe"),
    }

    print("\nRunning strategy-backtester ORB (default config)...")
    sb = run_strategy_backtester()

    success = compare(arena, sb)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

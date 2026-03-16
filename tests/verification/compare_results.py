#!/usr/bin/env python3
"""Compare strategy-backtester vs ATO_Simulator day_wise_log outputs.

Loads sb_results.json and ato_results.json, compares per config_id:
- Day count match
- Account values at each epoch (invested_value + margin_available)
- Summary metrics (CAGR, MaxDD, Calmar)
- Invariant checks (margin >= 0, etc.)

Pass criteria: account values match within 0.01% relative at every epoch.
"""

import json
import os
import sys

VERIFICATION_DIR = os.path.dirname(os.path.abspath(__file__))

# Tolerance levels
STRICT_TOL = 0.0001    # 0.01% relative
SOFT_TOL = 0.01         # 1% relative


def load_results(filename):
    path = os.path.join(VERIFICATION_DIR, filename)
    if not os.path.isfile(path):
        print(f"ERROR: {path} not found. Run the corresponding runner first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def compute_metrics(day_wise_log):
    """Compute CAGR, MaxDD, Calmar from day_wise_log."""
    if len(day_wise_log) < 2:
        return {"cagr": None, "max_dd": None, "calmar": None}

    account_values = [d["invested_value"] + d["margin_available"] for d in day_wise_log]
    start_val = account_values[0]
    end_val = account_values[-1]

    # Number of years
    start_epoch = day_wise_log[0]["log_date_epoch"]
    end_epoch = day_wise_log[-1]["log_date_epoch"]
    years = (end_epoch - start_epoch) / (365.25 * 86400)

    if years <= 0 or start_val <= 0:
        return {"cagr": None, "max_dd": None, "calmar": None}

    cagr = (end_val / start_val) ** (1 / years) - 1

    # Max drawdown
    peak = account_values[0]
    max_dd = 0
    for val in account_values:
        if val > peak:
            peak = val
        dd = (val - peak) / peak
        if dd < max_dd:
            max_dd = dd

    calmar = cagr / abs(max_dd) if max_dd != 0 else float("inf")

    return {"cagr": cagr, "max_dd": max_dd, "calmar": calmar}


def check_invariants(day_wise_log, label):
    """Check that margin_available >= 0 at all epochs."""
    violations = []
    for d in day_wise_log:
        if d["margin_available"] < -0.01:  # small tolerance for float
            violations.append({
                "epoch": d["log_date_epoch"],
                "margin_available": d["margin_available"],
            })
    return violations


def compare_config(config_id, sb_log, ato_log):
    """Compare a single config's day_wise_log between SB and ATO."""
    result = {
        "config_id": config_id,
        "sb_days": len(sb_log),
        "ato_days": len(ato_log),
        "pass": False,
        "soft_pass": False,
        "max_abs_diff": 0,
        "max_rel_diff": 0,
        "max_diff_epoch": None,
        "issues": [],
    }

    # Day count comparison
    if len(sb_log) != len(ato_log):
        result["issues"].append(
            f"Day count mismatch: SB={len(sb_log)}, ATO={len(ato_log)}"
        )

    # Build epoch -> values maps
    sb_map = {d["log_date_epoch"]: d for d in sb_log}
    ato_map = {d["log_date_epoch"]: d for d in ato_log}

    common_epochs = sorted(set(sb_map.keys()) & set(ato_map.keys()))
    sb_only = set(sb_map.keys()) - set(ato_map.keys())
    ato_only = set(ato_map.keys()) - set(sb_map.keys())

    if sb_only:
        result["issues"].append(f"SB-only epochs: {len(sb_only)}")
    if ato_only:
        result["issues"].append(f"ATO-only epochs: {len(ato_only)}")

    if not common_epochs:
        result["issues"].append("No common epochs to compare")
        return result

    # Compare account values at common epochs
    max_abs_diff = 0
    max_rel_diff = 0
    max_diff_epoch = None

    for epoch in common_epochs:
        sb_val = sb_map[epoch]["invested_value"] + sb_map[epoch]["margin_available"]
        ato_val = ato_map[epoch]["invested_value"] + ato_map[epoch]["margin_available"]

        abs_diff = abs(sb_val - ato_val)
        rel_diff = abs_diff / max(abs(ato_val), 1e-10)

        if rel_diff > max_rel_diff:
            max_rel_diff = rel_diff
            max_abs_diff = abs_diff
            max_diff_epoch = epoch

    result["max_abs_diff"] = max_abs_diff
    result["max_rel_diff"] = max_rel_diff
    result["max_diff_epoch"] = max_diff_epoch
    result["common_epochs"] = len(common_epochs)

    # Invariant checks
    sb_violations = check_invariants(sb_log, "SB")
    ato_violations = check_invariants(ato_log, "ATO")
    if sb_violations:
        result["issues"].append(f"SB margin violations: {len(sb_violations)}")
    if ato_violations:
        result["issues"].append(f"ATO margin violations: {len(ato_violations)}")

    # Pass/fail
    result["pass"] = max_rel_diff <= STRICT_TOL and not result["issues"]
    result["soft_pass"] = max_rel_diff <= SOFT_TOL

    return result


def main():
    print("=" * 60)
    print("  P2 Verification Report: strategy-backtester vs ATO_Simulator")
    print("=" * 60)
    print()

    sb_results = load_results("sb_results.json")
    ato_results = load_results("ato_results.json")

    print(f"Strategy-backtester configs: {len(sb_results)}")
    print(f"ATO_Simulator configs:       {len(ato_results)}")
    print()

    sb_only = set(sb_results.keys()) - set(ato_results.keys())
    ato_only = set(ato_results.keys()) - set(sb_results.keys())

    if sb_only:
        print(f"WARNING: SB-only configs: {sorted(sb_only)}")
    if ato_only:
        print(f"WARNING: ATO-only configs: {sorted(ato_only)}")

    common_configs = sorted(set(sb_results.keys()) & set(ato_results.keys()))

    if not common_configs:
        print("ERROR: No common config IDs to compare!")
        sys.exit(1)

    # Compare each config
    pass_count = 0
    soft_pass_count = 0
    fail_count = 0
    comparisons = []

    print(f"\n{'Config':<12} {'SB Days':>8} {'ATO Days':>9} {'MaxRelDiff':>11} {'Status':>8}")
    print("-" * 56)

    for config_id in common_configs:
        result = compare_config(
            config_id,
            sb_results[config_id],
            ato_results[config_id],
        )
        comparisons.append(result)

        if result["pass"]:
            status = "PASS"
            pass_count += 1
        elif result["soft_pass"]:
            status = "SOFT"
            soft_pass_count += 1
        else:
            status = "FAIL"
            fail_count += 1

        max_rel_pct = result["max_rel_diff"] * 100
        print(f"{config_id:<12} {result['sb_days']:>8} {result['ato_days']:>9} {max_rel_pct:>10.4f}% {status:>8}")

    # Detailed per-config metrics
    print(f"\n{'Config':<12} {'SB CAGR':>10} {'ATO CAGR':>10} {'SB MaxDD':>10} {'ATO MaxDD':>10}")
    print("-" * 56)

    for config_id in common_configs:
        sb_m = compute_metrics(sb_results[config_id])
        ato_m = compute_metrics(ato_results[config_id])

        sb_cagr = f"{sb_m['cagr']*100:.1f}%" if sb_m["cagr"] is not None else "N/A"
        ato_cagr = f"{ato_m['cagr']*100:.1f}%" if ato_m["cagr"] is not None else "N/A"
        sb_dd = f"{sb_m['max_dd']*100:.1f}%" if sb_m["max_dd"] is not None else "N/A"
        ato_dd = f"{ato_m['max_dd']*100:.1f}%" if ato_m["max_dd"] is not None else "N/A"

        print(f"{config_id:<12} {sb_cagr:>10} {ato_cagr:>10} {sb_dd:>10} {ato_dd:>10}")

    # Issues summary
    any_issues = [c for c in comparisons if c["issues"]]
    if any_issues:
        print("\nIssues:")
        for c in any_issues:
            for issue in c["issues"]:
                print(f"  {c['config_id']}: {issue}")

    # Overall summary
    total = len(common_configs)
    print(f"\n{'=' * 56}")
    print(f"Summary: {pass_count}/{total} PASS, {soft_pass_count}/{total} SOFT PASS, {fail_count}/{total} FAIL")

    if pass_count == total:
        print("RESULT: ALL PASS (within 0.01% tolerance)")
    elif pass_count + soft_pass_count == total:
        print("RESULT: ALL SOFT PASS (within 1% tolerance)")
    else:
        print("RESULT: FAILURES DETECTED")

    # Save detailed report
    report_path = os.path.join(VERIFICATION_DIR, "verification_report.json")
    with open(report_path, "w") as f:
        json.dump({
            "summary": {
                "total_configs": total,
                "pass": pass_count,
                "soft_pass": soft_pass_count,
                "fail": fail_count,
                "strict_tolerance": STRICT_TOL,
                "soft_tolerance": SOFT_TOL,
            },
            "comparisons": comparisons,
        }, f, indent=2)
    print(f"\nDetailed report saved to: {report_path}")


if __name__ == "__main__":
    main()

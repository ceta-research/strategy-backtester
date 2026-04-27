"""Ensemble equity-curve composition helpers.

Combines N strategy equity curves into a single ensemble curve that can be
fed to `lib.metrics.compute_metrics_from_curve` for normal metric computation.

Design contract:
  1. Each leg is treated as a *return stream* (v[t] / v[0]). This makes the
     ensemble starting capital purely notional: legs can have any underlying
     start_margin, the ensemble rescales them.
  2. Phase 1 supports `intersection` alignment only: the common epoch set
     across all leg curves. `union_ffill` is a Phase 2+ extension.
  3. Phase 1 is set-and-forget (no rebalancing). Each leg's notional NAV
     drifts with its returns; the winning leg's effective weight grows over
     time. Periodic rebalancing is Phase 2.
  4. All legs must share `frequency` (e.g. DAILY_CALENDAR). Mismatch raises.

This module is a NEW addition; it does not modify any protected lib/ file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence

from lib.equity_curve import EquityCurve, Frequency

REBALANCE_PERIODS = ("none", "monthly", "quarterly", "annual")


# ── Loaders ──────────────────────────────────────────────────────────────

def load_equity_curve_from_result(
    path: str,
    rank: int = 1,
    params_match: dict | None = None,
) -> tuple[EquityCurve, dict]:
    """Load an equity curve from a single-config or sweep result JSON.

    Args:
        path: Path to a result JSON produced by BacktestResult.save() or
            SweepResult.save().
        rank: 1-based rank into `detailed[]` (default 1 = top config). Used
            when params_match is None.
        params_match: If given, search `detailed[]` for the config whose
            params dict equals (or is a superset of) this match. Overrides
            rank when both are given.

    Returns:
        (EquityCurve, summary_dict)

    Raises:
        FileNotFoundError, KeyError, ValueError on malformed input.
    """
    with open(path) as f:
        data = json.load(f)

    rtype = data.get("type")

    # Determine source: single result vs. sweep
    if rtype == "single":
        eq = data.get("equity_curve", [])
        summary = data.get("summary", {})
        freq_name = data.get("equity_curve_frequency", "DAILY_CALENDAR")
    elif rtype in ("sweep", "multi_sweep"):
        if rtype == "multi_sweep":
            raise ValueError(
                f"{path}: multi_sweep results not supported in Phase 1; "
                f"specify a single sub-sweep manually."
            )
        detailed = data.get("detailed", [])
        if not detailed:
            raise ValueError(
                f"{path}: sweep has no 'detailed' configs (likely compacted). "
                f"Re-run the source config to regenerate equity_curve, or use "
                f"a different result file."
            )
        chosen = _select_detailed(detailed, rank, params_match, path)
        eq = chosen.get("equity_curve", [])
        summary = chosen.get("summary", {})
        # Sweep JSONs (v1.0) lack equity_curve_frequency; default to
        # DAILY_CALENDAR which matches the engine's forward-filled output.
        freq_name = data.get("equity_curve_frequency", "DAILY_CALENDAR")
    else:
        raise ValueError(f"{path}: unknown result type {rtype!r}")

    if not eq:
        raise ValueError(
            f"{path}: equity_curve is empty (likely compacted). Re-run the "
            f"source config to regenerate it."
        )

    try:
        freq = Frequency[freq_name]
    except KeyError as e:
        raise ValueError(f"{path}: unknown frequency {freq_name!r}") from e

    pairs = [(int(e["epoch"]), float(e["value"])) for e in eq]
    curve = EquityCurve.from_pairs(pairs, freq)
    return curve, summary


def _select_detailed(
    detailed: list[dict],
    rank: int,
    params_match: dict | None,
    path: str,
) -> dict:
    if params_match is not None:
        for entry in detailed:
            params = entry.get("params", {})
            if all(params.get(k) == v for k, v in params_match.items()):
                return entry
        raise KeyError(
            f"{path}: no detailed config matches params {params_match}. "
            f"Available: {[e.get('params') for e in detailed[:5]]}..."
        )
    if rank < 1 or rank > len(detailed):
        raise IndexError(
            f"{path}: rank {rank} out of range (1..{len(detailed)})"
        )
    return detailed[rank - 1]


# ── Alignment ────────────────────────────────────────────────────────────

def align_curves(
    curves: Sequence[EquityCurve],
    mode: str = "intersection",
) -> tuple[tuple[int, ...], list[tuple[float, ...]]]:
    """Align N equity curves to a common epoch axis.

    Args:
        curves: List of EquityCurve. All must share `frequency`.
        mode: "intersection" (default) — take only epochs present in all
            curves. "union_ffill" is reserved for Phase 2+.

    Returns:
        (common_epochs, [aligned_values_per_curve])
        aligned_values_per_curve[i] is a tuple of length len(common_epochs)
        with curve i's values at those epochs.

    Raises:
        ValueError on frequency mismatch, empty intersection, or unsupported mode.
    """
    if not curves:
        raise ValueError("align_curves: at least one curve required")
    freqs = {c.frequency for c in curves}
    if len(freqs) > 1:
        raise ValueError(
            f"align_curves: all curves must share frequency; got {freqs}"
        )

    if mode == "intersection":
        common = set(curves[0].epochs)
        for c in curves[1:]:
            common &= set(c.epochs)
        if not common:
            raise ValueError(
                "align_curves: empty intersection — curves do not overlap"
            )
        common_epochs = tuple(sorted(common))
        aligned: list[tuple[float, ...]] = []
        for c in curves:
            idx = {e: i for i, e in enumerate(c.epochs)}
            aligned.append(tuple(c.values[idx[e]] for e in common_epochs))
        return common_epochs, aligned

    if mode == "union_ffill":
        raise NotImplementedError(
            "union_ffill alignment is Phase 2+; use intersection for now"
        )

    raise ValueError(f"align_curves: unknown mode {mode!r}")


# ── Combination ──────────────────────────────────────────────────────────

def combine_curves(
    aligned_values: Sequence[Sequence[float]],
    weights: Sequence[float],
    starting_capital: float,
    weight_tolerance: float = 1e-6,
) -> list[float]:
    """Combine aligned leg values into a single ensemble equity series.

    Each leg is treated as a return stream `v[t] / v[0]`. The ensemble value
    at time t is `starting_capital * sum_i(weight_i * v_i[t] / v_i[0])`.

    This is mathematically equivalent to:
      "Allocate `weight_i * starting_capital` to leg i on day 0; never
       rebalance; sum the leg NAVs."

    Args:
        aligned_values: One value series per leg, all the same length.
        weights: Per-leg weights, must sum to 1.0 (within `weight_tolerance`)
            and all be non-negative.
        starting_capital: Notional ensemble starting NAV.
        weight_tolerance: Allowed absolute deviation of sum(weights) from 1.

    Returns:
        list[float] of length len(aligned_values[0]) — ensemble NAV per epoch.

    Raises:
        ValueError on weight/length/starting-value validation failures.
    """
    if not aligned_values:
        raise ValueError("combine_curves: at least one leg required")
    n_legs = len(aligned_values)
    if len(weights) != n_legs:
        raise ValueError(
            f"combine_curves: weights length {len(weights)} != legs {n_legs}"
        )
    if any(w < 0 for w in weights):
        raise ValueError(f"combine_curves: weights must be non-negative; got {list(weights)}")
    s = sum(weights)
    if abs(s - 1.0) > weight_tolerance:
        raise ValueError(
            f"combine_curves: weights must sum to 1.0 (got {s:.6f})"
        )
    if starting_capital <= 0:
        raise ValueError(
            f"combine_curves: starting_capital must be > 0; got {starting_capital}"
        )

    n_points = len(aligned_values[0])
    for i, vs in enumerate(aligned_values):
        if len(vs) != n_points:
            raise ValueError(
                f"combine_curves: leg {i} length {len(vs)} != {n_points}"
            )
        if vs[0] <= 0:
            raise ValueError(
                f"combine_curves: leg {i} starts at {vs[0]} (must be > 0)"
            )

    out: list[float] = []
    starts = [vs[0] for vs in aligned_values]
    for t in range(n_points):
        ensemble = 0.0
        for i in range(n_legs):
            ensemble += weights[i] * (aligned_values[i][t] / starts[i])
        out.append(starting_capital * ensemble)
    return out


def rebalance_combined_curve(
    epochs: Sequence[int],
    aligned_values: Sequence[Sequence[float]],
    weights: Sequence[float],
    starting_capital: float,
    period: str,
    weight_tolerance: float = 1e-6,
) -> list[float]:
    """Combine aligned legs with periodic rebalancing to target weights.

    At each rebalance boundary, leg NAVs are reset so leg_i = weight_i *
    combined_NAV. Between boundaries, each leg compounds independently at its
    own per-period return v_i[t]/v_i[t-1].

    Convention: rebalance fires at the FIRST observation of each new period
    (e.g. for monthly rebalance, the first observation in February resets to
    target weights based on the close-of-January combined NAV).

    `period="none"` is equivalent to combine_curves (set-and-forget). For
    pure no-rebalance, prefer combine_curves directly — this function is
    correct but slightly slower.

    Args:
        epochs: Common epoch axis (length T), strictly increasing.
        aligned_values: Per-leg value series, each length T.
        weights: Target weights summing to 1.0.
        starting_capital: Notional ensemble starting NAV.
        period: One of REBALANCE_PERIODS.
        weight_tolerance: As in combine_curves.

    Returns:
        list[float] of length T — ensemble NAV per epoch.

    Caveats:
        Rebalancing is FRICTIONLESS in Phase 2. Real-world quarterly rebalance
        adds ~5-10bps per turn; not modeled. For multi-asset live deployments,
        annualize that drag and subtract from realized CAGR.
    """
    if period not in REBALANCE_PERIODS:
        raise ValueError(
            f"rebalance_combined_curve: period must be one of {REBALANCE_PERIODS}, "
            f"got {period!r}"
        )

    n_legs = len(aligned_values)
    if len(weights) != n_legs:
        raise ValueError(
            f"rebalance_combined_curve: weights length {len(weights)} "
            f"!= legs {n_legs}"
        )
    if any(w < 0 for w in weights):
        raise ValueError(
            f"rebalance_combined_curve: weights must be non-negative; got {list(weights)}"
        )
    s = sum(weights)
    if abs(s - 1.0) > weight_tolerance:
        raise ValueError(
            f"rebalance_combined_curve: weights must sum to 1.0 (got {s:.6f})"
        )
    if starting_capital <= 0:
        raise ValueError(
            f"rebalance_combined_curve: starting_capital must be > 0; got {starting_capital}"
        )

    n_points = len(epochs)
    if n_points == 0:
        return []
    for i, vs in enumerate(aligned_values):
        if len(vs) != n_points:
            raise ValueError(
                f"rebalance_combined_curve: leg {i} length {len(vs)} != {n_points}"
            )
        if vs[0] <= 0:
            raise ValueError(
                f"rebalance_combined_curve: leg {i} starts at {vs[0]} (must be > 0)"
            )

    # Each leg starts at weight_i * starting_capital
    leg_nav = [weights[i] * starting_capital for i in range(n_legs)]
    combined: list[float] = [sum(leg_nav)]
    prev_key = _period_key(epochs[0], period)

    for t in range(1, n_points):
        # Compound each leg by its own return between t-1 and t
        for i in range(n_legs):
            prev = aligned_values[i][t - 1]
            cur = aligned_values[i][t]
            if prev > 0:
                leg_nav[i] *= cur / prev
            # If a leg's prior value is 0 we leave its NAV unchanged (rare;
            # a properly-validated start_value > 0 makes this near-impossible).

        cur_key = _period_key(epochs[t], period)
        if period != "none" and cur_key != prev_key:
            # Period boundary crossed → rebalance
            total = sum(leg_nav)
            leg_nav = [weights[i] * total for i in range(n_legs)]
        prev_key = cur_key

        combined.append(sum(leg_nav))

    return combined


def _period_key(epoch: int, period: str):
    """Return a comparable key that changes when `period` rolls over."""
    if period == "none":
        return None
    dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    if period == "monthly":
        return (dt.year, dt.month)
    if period == "quarterly":
        return (dt.year, (dt.month - 1) // 3)
    if period == "annual":
        return dt.year
    raise ValueError(f"_period_key: unknown period {period!r}")


def build_ensemble_curve(
    curves: Sequence[EquityCurve],
    weights: Sequence[float],
    starting_capital: float,
    mode: str = "intersection",
    rebalance: str = "none",
) -> EquityCurve:
    """End-to-end: align N curves and produce a single combined EquityCurve.

    Args:
        curves: Per-leg EquityCurve objects (must share frequency).
        weights: Per-leg weights summing to 1.0.
        starting_capital: Notional ensemble starting NAV.
        mode: Alignment mode passed to `align_curves`.
        rebalance: One of REBALANCE_PERIODS. "none" uses fast combine_curves;
            others use rebalance_combined_curve.

    Returns:
        EquityCurve of the ensemble, ready for compute_metrics_from_curve().
    """
    common_epochs, aligned = align_curves(curves, mode=mode)
    if rebalance == "none":
        combined_values = combine_curves(aligned, weights, starting_capital)
    else:
        combined_values = rebalance_combined_curve(
            common_epochs, aligned, weights, starting_capital, rebalance
        )
    return EquityCurve.from_pairs(
        list(zip(common_epochs, combined_values)),
        curves[0].frequency,
    )

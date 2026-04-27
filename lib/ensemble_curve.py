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
import math
from datetime import datetime, timezone
from typing import Sequence

from lib.equity_curve import EquityCurve, Frequency

REBALANCE_PERIODS = ("none", "monthly", "quarterly", "annual")
WEIGHTING_MODES = ("fixed", "inverse_vol", "risk_parity")


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


def compute_leg_navs(
    epochs: Sequence[int],
    aligned_values: Sequence[Sequence[float]],
    weights: Sequence[float],
    starting_capital: float,
    period: str,
    weight_tolerance: float = 1e-6,
) -> list[list[float]]:
    """Walk the per-leg NAV trajectories with optional periodic rebalance.

    Same compounding/rebalance logic as `rebalance_combined_curve` but
    returns the per-leg NAV series instead of just their sum. The combined
    curve at time t is `sum(leg_navs[i][t] for i in range(n_legs))`.

    Used by drawdown_attribution() and any other diagnostic that needs to
    know each leg's NAV at specific timesteps.

    Args:
        Same as rebalance_combined_curve.

    Returns:
        list[list[float]] of shape (n_legs, n_points). leg_navs[i][t] is
        leg i's NAV at epoch t.
    """
    if period not in REBALANCE_PERIODS:
        raise ValueError(
            f"compute_leg_navs: period must be one of {REBALANCE_PERIODS}, "
            f"got {period!r}"
        )
    n_legs = len(aligned_values)
    if len(weights) != n_legs:
        raise ValueError(
            f"compute_leg_navs: weights length {len(weights)} != legs {n_legs}"
        )
    if any(w < 0 for w in weights):
        raise ValueError(f"compute_leg_navs: weights must be non-negative")
    if abs(sum(weights) - 1.0) > weight_tolerance:
        raise ValueError(
            f"compute_leg_navs: weights must sum to 1.0 (got {sum(weights):.6f})"
        )
    if starting_capital <= 0:
        raise ValueError(f"compute_leg_navs: starting_capital must be > 0")

    n_points = len(epochs)
    if n_points == 0:
        return [[] for _ in range(n_legs)]
    for i, vs in enumerate(aligned_values):
        if len(vs) != n_points:
            raise ValueError(
                f"compute_leg_navs: leg {i} length {len(vs)} != {n_points}"
            )
        if vs[0] <= 0:
            raise ValueError(f"compute_leg_navs: leg {i} starts at {vs[0]}")

    # Per-leg NAV series, all initialized to weight_i * starting_capital
    leg_series: list[list[float]] = [
        [weights[i] * starting_capital] for i in range(n_legs)
    ]
    leg_nav = [weights[i] * starting_capital for i in range(n_legs)]
    prev_key = _period_key(epochs[0], period)

    for t in range(1, n_points):
        for i in range(n_legs):
            prev = aligned_values[i][t - 1]
            cur = aligned_values[i][t]
            if prev > 0:
                leg_nav[i] *= cur / prev
        cur_key = _period_key(epochs[t], period)
        if period != "none" and cur_key != prev_key:
            total = sum(leg_nav)
            leg_nav = [weights[i] * total for i in range(n_legs)]
        prev_key = cur_key
        for i in range(n_legs):
            leg_series[i].append(leg_nav[i])

    return leg_series


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


# ── Weighting ────────────────────────────────────────────────────────────

def compute_inverse_vol_weights(
    curves: Sequence[EquityCurve],
    lookback_days: int | None = None,
) -> list[float]:
    """Inverse-volatility weights: w_i ∝ 1 / vol_i, normalized to sum to 1.

    Equivalent to risk-parity (equal risk contribution) when leg-pair
    correlations are equal — a near-identical approximation for the 2-leg
    case and a reasonable starting point for N>2.

    Args:
        curves: Per-leg EquityCurve objects.
        lookback_days: If None (default), use the full curve to compute vol.
            If int, use only the last N samples (assumes one sample per
            calendar day, matching DAILY_CALENDAR semantics). Same fixed
            weights are then applied for the entire backtest — this does
            NOT do per-rebalance adaptive weighting.

    Returns:
        Normalized weights summing to 1.0.

    Caveats:
        Full-window vol introduces in-sample bias: weights "know" the entire
        backtest's variance. For honest forward-looking estimates, use
        lookback_days and evaluate only the post-lookback portion of the
        curve, or build per-rebalance adaptive weights (future Phase 3.5).
    """
    if not curves:
        raise ValueError("compute_inverse_vol_weights: at least one curve required")

    vols: list[float] = []
    for i, c in enumerate(curves):
        if len(c) < 2:
            raise ValueError(
                f"compute_inverse_vol_weights: leg {i} has < 2 points"
            )
        if lookback_days is None:
            returns = c.period_returns()
        else:
            n = min(lookback_days, len(c) - 1)
            if n < 1:
                raise ValueError(
                    f"compute_inverse_vol_weights: lookback_days={lookback_days} "
                    f"yields < 1 return for leg {i} (curve length {len(c)})"
                )
            window_values = c.values[-(n + 1):]
            returns = []
            for j in range(1, len(window_values)):
                prev = window_values[j - 1]
                if prev > 0:
                    returns.append(window_values[j] / prev - 1)
                else:
                    returns.append(0.0)

        if len(returns) < 2:
            raise ValueError(
                f"compute_inverse_vol_weights: leg {i} produced < 2 returns"
            )
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        ppy = c.frequency.periods_per_year
        vols.append(math.sqrt(var) * math.sqrt(ppy))

    if any(v <= 0 for v in vols):
        raise ValueError(
            f"compute_inverse_vol_weights: zero/negative vol on a leg "
            f"({vols}); cannot invert"
        )

    inv = [1.0 / v for v in vols]
    total = sum(inv)
    return [w / total for w in inv]


def resolve_weights(
    cfg_legs: Sequence[dict],
    curves: Sequence[EquityCurve],
    weighting: str,
    lookback_days: int | None = None,
) -> list[float]:
    """Resolve final per-leg weights based on weighting mode.

    Args:
        cfg_legs: Leg config dicts (each may have a 'weight' field used in
            mode 'fixed').
        curves: Per-leg EquityCurves (used in computed modes).
        weighting: One of WEIGHTING_MODES.
        lookback_days: Passed to compute_inverse_vol_weights when applicable.

    Returns:
        list[float] of length len(cfg_legs), summing to 1.0.

    Raises:
        ValueError on unknown mode, NotImplementedError for risk_parity
        (use inverse_vol as the practical approximation).
    """
    if weighting not in WEIGHTING_MODES:
        raise ValueError(
            f"resolve_weights: weighting must be one of {WEIGHTING_MODES}, "
            f"got {weighting!r}"
        )
    if weighting == "fixed":
        return [float(leg["weight"]) for leg in cfg_legs]
    if weighting == "inverse_vol":
        return compute_inverse_vol_weights(curves, lookback_days=lookback_days)
    if weighting == "risk_parity":
        # Equal-risk-contribution requires iterative solver on the leg
        # covariance matrix. For 2 legs, identical to inverse_vol. For N>2,
        # inverse_vol is a one-step approximation. Full ERC is Phase 3.5.
        raise NotImplementedError(
            "risk_parity weighting requires an iterative ERC solver "
            "(Phase 3.5). Use 'inverse_vol' as the practical approximation."
        )


# ── Diagnostics: correlation + sensitivity ───────────────────────────────

def compute_correlation_matrix(
    curves: Sequence[EquityCurve],
    mode: str = "intersection",
) -> list[list[float]]:
    """Pairwise Pearson correlation of per-period returns across legs.

    Returns a symmetric n×n matrix; diagonals = 1.0. Uses aligned period
    returns so all legs contribute the same number of observations.

    Args:
        curves: Per-leg EquityCurves (must share frequency).
        mode: Alignment mode (only 'intersection' supported).

    Returns:
        list[list[float]] — corr[i][j] in [-1, 1].
    """
    if not curves:
        return []
    _, aligned = align_curves(curves, mode=mode)
    n_legs = len(aligned)
    n_points = len(aligned[0])

    # Build per-period returns from aligned values
    leg_returns: list[list[float]] = []
    for vs in aligned:
        rets = []
        for t in range(1, n_points):
            prev = vs[t - 1]
            rets.append(vs[t] / prev - 1 if prev > 0 else 0.0)
        leg_returns.append(rets)

    n = len(leg_returns[0])
    if n < 2:
        return [[1.0 if i == j else 0.0 for j in range(n_legs)]
                for i in range(n_legs)]

    means = [sum(r) / n for r in leg_returns]
    deviations = [[r[t] - means[i] for t in range(n)]
                  for i, r in enumerate(leg_returns)]
    variances = [sum(d ** 2 for d in row) / (n - 1) for row in deviations]

    corr: list[list[float]] = [[0.0] * n_legs for _ in range(n_legs)]
    for i in range(n_legs):
        corr[i][i] = 1.0
    for i in range(n_legs):
        for j in range(i + 1, n_legs):
            cov = sum(deviations[i][t] * deviations[j][t] for t in range(n)) / (n - 1)
            denom = math.sqrt(variances[i] * variances[j])
            c = cov / denom if denom > 0 else 0.0
            corr[i][j] = corr[j][i] = round(c, 6)
    return corr


def sharpe_sensitivity_2leg(
    curves: Sequence[EquityCurve],
    starting_capital: float,
    rebalance: str = "none",
    risk_free_rate: float = 0.02,
    n_grid: int = 21,
) -> dict:
    """Sweep two-leg weight (w1 ∈ [0,1]) and report ensemble Sharpe at each.

    For a 2-leg ensemble, this curve has at most one interior maximum
    (Markowitz tangency). Compares to inverse-vol and equal-weight as
    reference points.

    Args:
        curves: Exactly 2 EquityCurves.
        starting_capital: Notional for combine math.
        rebalance: Pass-through to combine logic.
        risk_free_rate: Used to compute Sharpe.
        n_grid: Number of grid points (default 21 → step 0.05).

    Returns:
        {
            "grid": [{"w1": .., "w2": .., "cagr": .., "vol": .., "sharpe": ..}, ...],
            "peak_weights": [w1, w2],
            "peak_sharpe": float,
            "inverse_vol_weights": [w1, w2],
            "inverse_vol_sharpe": float,
        }
    """
    # Local import to avoid circular dependency at module-load time.
    from lib.metrics import compute_metrics_from_curve  # noqa: WPS433

    if len(curves) != 2:
        raise ValueError(
            f"sharpe_sensitivity_2leg: expected 2 legs, got {len(curves)}"
        )

    grid: list[dict] = []
    peak_sharpe = float("-inf")
    peak_weights = (0.5, 0.5)
    for k in range(n_grid):
        w1 = k / (n_grid - 1)
        w2 = 1 - w1
        # Skip degenerate single-leg endpoints? Include them (they're
        # informative anchors); only skip if leg has length issues.
        ens = build_ensemble_curve(
            curves, [w1, w2], starting_capital, rebalance=rebalance
        )
        m = compute_metrics_from_curve(ens, risk_free_rate=risk_free_rate)["portfolio"]
        sh = m.get("sharpe_ratio")
        cagr = m.get("cagr")
        vol = m.get("annualized_volatility")
        grid.append({
            "w1": round(w1, 4),
            "w2": round(w2, 4),
            "cagr": round(cagr, 6) if cagr is not None else None,
            "vol": round(vol, 6) if vol is not None else None,
            "sharpe": round(sh, 6) if sh is not None else None,
        })
        if sh is not None and sh > peak_sharpe:
            peak_sharpe = sh
            peak_weights = (w1, w2)

    iv = compute_inverse_vol_weights(curves)
    iv_ens = build_ensemble_curve(curves, iv, starting_capital, rebalance=rebalance)
    iv_m = compute_metrics_from_curve(iv_ens, risk_free_rate=risk_free_rate)["portfolio"]
    iv_sharpe = iv_m.get("sharpe_ratio")

    return {
        "grid": grid,
        "peak_weights": [round(peak_weights[0], 4), round(peak_weights[1], 4)],
        "peak_sharpe": round(peak_sharpe, 6) if peak_sharpe != float("-inf") else None,
        "inverse_vol_weights": [round(iv[0], 4), round(iv[1], 4)],
        "inverse_vol_sharpe": round(iv_sharpe, 6) if iv_sharpe is not None else None,
    }


# ── Drawdown attribution ─────────────────────────────────────────────────

def attribute_drawdown(
    epochs: Sequence[int],
    leg_navs: Sequence[Sequence[float]],
    leg_names: Sequence[str],
) -> dict:
    """Decompose the ensemble's worst drawdown into per-leg contributions.

    Walks the combined NAV (sum of leg_navs) to find the global peak/trough
    pair (peak-to-trough that produces the largest drawdown). At those two
    epochs, computes each leg's NAV change as a fraction of the combined
    NAV at peak — those fractions sum to the ensemble drawdown.

    Args:
        epochs: Common epoch axis.
        leg_navs: Per-leg NAV series (from compute_leg_navs).
        leg_names: Display names for each leg.

    Returns:
        {
            "peak_epoch": int, "peak_date": "YYYY-MM-DD",
            "trough_epoch": int, "trough_date": "YYYY-MM-DD",
            "ensemble_drawdown": float,  # negative
            "duration_days": int,
            "legs": [
                {"name": str, "nav_at_peak": float, "nav_at_trough": float,
                 "leg_return": float, "contribution_to_dd": float},
                ...
            ]
        }
        contribution_to_dd values sum to ensemble_drawdown (within fp).
    """
    if not leg_navs or not epochs:
        return {}
    n_points = len(epochs)
    n_legs = len(leg_navs)
    if any(len(s) != n_points for s in leg_navs):
        raise ValueError("attribute_drawdown: leg_navs length mismatch")
    if len(leg_names) != n_legs:
        raise ValueError(
            f"attribute_drawdown: leg_names length {len(leg_names)} "
            f"!= legs {n_legs}"
        )

    combined = [sum(leg_navs[i][t] for i in range(n_legs)) for t in range(n_points)]

    # Find peak/trough pair that produces max drawdown
    running_peak = combined[0]
    running_peak_idx = 0
    max_dd = 0.0
    peak_idx_at_max = 0
    trough_idx_at_max = 0
    for t in range(n_points):
        if combined[t] > running_peak:
            running_peak = combined[t]
            running_peak_idx = t
        if running_peak > 0:
            dd = (combined[t] - running_peak) / running_peak
            if dd < max_dd:
                max_dd = dd
                peak_idx_at_max = running_peak_idx
                trough_idx_at_max = t

    p, q = peak_idx_at_max, trough_idx_at_max
    peak_combined = combined[p]
    legs_out = []
    for i in range(n_legs):
        nav_p = leg_navs[i][p]
        nav_q = leg_navs[i][q]
        leg_return = (nav_q / nav_p - 1) if nav_p > 0 else 0.0
        contribution = (nav_q - nav_p) / peak_combined if peak_combined > 0 else 0.0
        legs_out.append({
            "name": leg_names[i],
            "nav_at_peak": round(nav_p, 2),
            "nav_at_trough": round(nav_q, 2),
            "leg_return": round(leg_return, 6),
            "contribution_to_dd": round(contribution, 6),
        })

    peak_dt = datetime.fromtimestamp(int(epochs[p]), tz=timezone.utc)
    trough_dt = datetime.fromtimestamp(int(epochs[q]), tz=timezone.utc)
    return {
        "peak_epoch": int(epochs[p]),
        "peak_date": peak_dt.strftime("%Y-%m-%d"),
        "trough_epoch": int(epochs[q]),
        "trough_date": trough_dt.strftime("%Y-%m-%d"),
        "ensemble_drawdown": round(max_dd, 6),
        "duration_days": (int(epochs[q]) - int(epochs[p])) // 86400,
        "legs": legs_out,
    }


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

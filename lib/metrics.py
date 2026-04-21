"""Comprehensive backtesting metrics computation.

Computes 17 Tier 1 + Tier 2 advanced metrics for strategy backtests.
Pure stdlib (math only, no numpy/pandas dependency).

Usage:
    from metrics import compute_metrics

    result = compute_metrics(
        period_returns=[0.05, -0.02, 0.08, ...],
        benchmark_returns=[0.03, -0.01, 0.06, ...],
        periods_per_year=2,  # semi-annual
        risk_free_rate=0.02,
    )
    print(result["portfolio"]["cagr"])   # e.g. 0.0996
    print(result["comparison"]["sharpe_ratio"])  # e.g. 0.523

See METHODOLOGY.md for formula definitions and interpretation guides.
"""

import math

from lib.equity_curve import EquityCurve, Frequency


def compute_metrics_from_curve(port_curve, bench_curve=None,
                               risk_free_rate=0.02, additional_benchmarks=None):
    """Compute metrics from EquityCurve objects.

    This is the correct path for all new callers. CAGR is derived from
    wall-clock duration (`curve.years`), not from sample count. Volatility
    annualization uses the curve's declared `frequency.periods_per_year`.

    Result is a superset of compute_metrics() output; legacy callers that
    pass raw returns lists should continue to use compute_metrics().

    Args:
        port_curve: EquityCurve of portfolio values.
        bench_curve: EquityCurve of benchmark values. Must have the same
            frequency and equal length as port_curve; if omitted, benchmark
            metrics are reported as zeros.
        risk_free_rate: Annual risk-free rate (default 0.02).
        additional_benchmarks: Optional dict[str, EquityCurve].

    Returns:
        Same dict shape as compute_metrics(). See that function's docstring.
    """
    if not isinstance(port_curve, EquityCurve):
        raise TypeError(f"port_curve must be EquityCurve, got {type(port_curve).__name__}")
    if bench_curve is not None and not isinstance(bench_curve, EquityCurve):
        raise TypeError(f"bench_curve must be EquityCurve or None, got {type(bench_curve).__name__}")

    if len(port_curve) < 2:
        return _empty_metrics()

    ppy = port_curve.frequency.periods_per_year
    port_returns = port_curve.period_returns()

    if bench_curve is not None:
        if bench_curve.frequency != port_curve.frequency:
            raise ValueError(
                f"bench_curve frequency ({bench_curve.frequency}) must match "
                f"port_curve frequency ({port_curve.frequency})"
            )
        if len(bench_curve) != len(port_curve):
            raise ValueError(
                f"bench_curve length ({len(bench_curve)}) must match "
                f"port_curve length ({len(port_curve)})"
            )
        bench_returns = bench_curve.period_returns()
    else:
        bench_returns = [0.0] * len(port_returns)

    port_cagr, port_total_return = _cagr_from_curve(port_curve)
    port = _compute_series_metrics_with_cagr(
        port_returns, ppy, risk_free_rate, port_cagr, port_total_return)

    if bench_curve is not None:
        bench_cagr, bench_total_return = _cagr_from_curve(bench_curve)
        bench = _compute_series_metrics_with_cagr(
            bench_returns, ppy, risk_free_rate, bench_cagr, bench_total_return)
    else:
        bench_cagr = 0.0
        bench = _compute_series_metrics_with_cagr(
            bench_returns, ppy, risk_free_rate, 0.0, 0.0)

    comp = _compute_comparison(port_returns, bench_returns, ppy, risk_free_rate,
                               port_cagr, bench_cagr)

    result = {"portfolio": port, "benchmark": bench, "comparison": comp}

    if additional_benchmarks:
        result["additional_benchmarks"] = {}
        for name, ab_curve in additional_benchmarks.items():
            if not isinstance(ab_curve, EquityCurve):
                raise TypeError(f"additional_benchmarks[{name!r}] must be EquityCurve")
            if ab_curve.frequency != port_curve.frequency or len(ab_curve) != len(port_curve):
                continue
            ab_returns = ab_curve.period_returns()
            ab_cagr, ab_total = _cagr_from_curve(ab_curve)
            ab_metrics = _compute_series_metrics_with_cagr(
                ab_returns, ppy, risk_free_rate, ab_cagr, ab_total)
            ab_comp = _compute_comparison(port_returns, ab_returns, ppy,
                                          risk_free_rate, port_cagr, ab_cagr)
            result["additional_benchmarks"][name] = {
                "metrics": ab_metrics,
                "comparison": ab_comp,
            }

    return result


def _cagr_from_curve(curve):
    """Return (cagr, total_return) using wall-clock years.

    CAGR is frequency-independent: a 10-year backtest with 2520 trading-day
    points produces the same CAGR as the same backtest with 3653 calendar-day
    (forward-filled) points. This is the invariant the old `n / ppy` formula
    violated.

    Callers that want to reject short-duration CAGR numbers (e.g. a 5-day
    backtest produces an absurd annualization) should check curve.years at
    the call site. The metric itself is well-defined for any years > 0.
    """
    if len(curve) < 2:
        return None, 0.0
    start, end = curve.values[0], curve.values[-1]
    if start <= 0:
        return None, 0.0
    total_return = end / start - 1
    years = curve.years
    if years <= 0:  # impossible given EquityCurve invariants but guard anyway
        return None, total_return
    if end <= 0:
        return -1.0, total_return
    cagr = (end / start) ** (1.0 / years) - 1
    return cagr, total_return


def compute_metrics(period_returns, benchmark_returns, periods_per_year,
                    risk_free_rate=0.02, additional_benchmarks=None):
    """Compute full metrics suite for a strategy vs benchmark(s).

    Args:
        period_returns: list[float] - portfolio returns per period (e.g. 0.05 for 5%)
        benchmark_returns: list[float] - primary benchmark returns (same length)
        periods_per_year: int - 1 (annual), 2 (semi-annual), 4 (quarterly), 12 (monthly)
        risk_free_rate: float - annual risk-free rate (default 0.02 = 2%)
        additional_benchmarks: dict[str, list[float]] - optional extra benchmarks
            e.g. {"INDA": [0.03, ...], "QUAL": [0.02, ...]}

    Returns:
        dict with keys: "portfolio", "benchmark", "comparison", "additional_benchmarks"
        All return values are raw floats (e.g. 0.0996 for 9.96% CAGR).
    """
    n = len(period_returns)
    if n < 2:
        return _empty_metrics()

    ppy = periods_per_year
    rf_period = risk_free_rate / ppy

    # Portfolio metrics
    port = _compute_series_metrics(period_returns, ppy, risk_free_rate)

    # Benchmark metrics
    bench = _compute_series_metrics(benchmark_returns, ppy, risk_free_rate)

    # Comparison metrics (portfolio vs primary benchmark)
    comp = _compute_comparison(period_returns, benchmark_returns, ppy, risk_free_rate,
                               port["cagr"], bench["cagr"])

    result = {
        "portfolio": port,
        "benchmark": bench,
        "comparison": comp,
    }

    # Additional benchmarks
    if additional_benchmarks:
        result["additional_benchmarks"] = {}
        for name, bench_rets in additional_benchmarks.items():
            if len(bench_rets) == n:
                ab_metrics = _compute_series_metrics(bench_rets, ppy, risk_free_rate)
                ab_comp = _compute_comparison(period_returns, bench_rets, ppy,
                                              risk_free_rate, port["cagr"], ab_metrics["cagr"])
                result["additional_benchmarks"][name] = {
                    "metrics": ab_metrics,
                    "comparison": ab_comp,
                }

    return result


def _compute_series_metrics(returns, ppy, risk_free_rate):
    """Legacy entry point: computes CAGR from sample count (`years = n / ppy`).

    Retained for backwards compatibility with callers that pass already-correct
    returns series (e.g. the intraday stack, which produces one return per
    trading day and passes ppy=252). New callers should go via
    compute_metrics_from_curve().
    """
    n = len(returns)
    if n < 2:
        return {}

    # Legacy CAGR: `years = n / ppy`. Correct ONLY when sampling rate == ppy.
    cumulative = 1.0
    for r in returns:
        cumulative *= (1 + r)
    years = n / ppy
    if cumulative > 0 and years > 0:
        cagr = cumulative ** (1.0 / years) - 1
    else:
        cagr = -1.0
    total_return = cumulative - 1

    return _compute_series_metrics_with_cagr(returns, ppy, risk_free_rate, cagr, total_return)


def _compute_series_metrics_with_cagr(returns, ppy, risk_free_rate, cagr, total_return):
    """Compute all series metrics given an externally-supplied CAGR.

    This is the shared body used by both legacy and EquityCurve-based paths.
    CAGR and total_return are inputs so the caller can source them from
    wall-clock years (correct) or from sample-count (legacy).

    For n < 2 returns (e.g. a 2-point EquityCurve spanning a long wall-clock
    interval), CAGR and total_return are still defined but variance-based
    metrics (vol, Sharpe, Sortino, skew, kurt) are not.
    """
    n = len(returns)
    if n < 2:
        return {
            "cagr": cagr,
            "total_return": total_return,
            "max_drawdown": 0.0 if n == 0 else min(0.0, returns[0]),
            "max_dd_duration_periods": 0,
            "annualized_volatility": None,
            "sharpe_ratio": None,
            "sharpe_ratio_arithmetic": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "var_95": None, "cvar_95": None,
            "best_period": None, "worst_period": None,
            "pct_negative_periods": None,
            "max_consecutive_losses": None,
            "skewness": None, "kurtosis": None,
        }

    # Per-period risk-free rate used as the MAR (minimum acceptable return)
    # threshold in Sortino downside calculation. Invariant: `ppy` must equal
    # the return-sampling frequency for this to be dimensionally correct.
    # Callers on the EquityCurve path get ppy from `curve.frequency.periods_per_year`
    # (365 for DAILY_CALENDAR, 252 for DAILY_TRADING). Legacy callers pass ppy
    # directly and are responsible for matching their sampling rate.
    # Known minor distortion: with DAILY_CALENDAR (forward-filled weekends),
    # zero-return weekend days register as marginally-below-rf_period downside
    # contributions. Impact is negligible (<1e-5 of downside_dev) but noted.
    rf_period = risk_free_rate / ppy

    # Drawdown + cumulative equity path (used only for MDD + duration)
    cumulative = 1.0
    peak = 1.0
    max_dd = 0.0
    max_dd_duration = 0
    current_dd_start = 0
    in_drawdown = False

    cumulative_values = []
    for i, r in enumerate(returns):
        cumulative *= (1 + r)
        cumulative_values.append(cumulative)

        if cumulative > peak:
            if in_drawdown:
                duration = i - current_dd_start
                if duration > max_dd_duration:
                    max_dd_duration = duration
                in_drawdown = False
            peak = cumulative
        else:
            if not in_drawdown:
                current_dd_start = i
                in_drawdown = True

        # Defensive `peak > 0` guard is unreachable in this function (peak
        # initializes to 1.0 and only grows), but retained for symmetry with
        # compute_drawdown_series() which can receive user-supplied curves
        # that start at 0. Semantics for peak<=0: "no positive equity peak
        # ever recorded" → drawdown is undefined; returning 0 is a benign
        # default (no drawdown from nothing). P2 item L41.
        dd = (cumulative - peak) / peak if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # If still in drawdown at end, count duration to end
    if in_drawdown:
        duration = n - current_dd_start
        if duration > max_dd_duration:
            max_dd_duration = duration

    # CAGR and total_return are supplied by the caller.

    # Volatility (annualized)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    vol = math.sqrt(variance) * math.sqrt(ppy)

    # Sharpe ratio — TWO definitions emitted (P2 decision D1):
    #   sharpe_ratio            : geometric, (CAGR - rf) / ann_vol. Used by
    #                             the existing leaderboard and regression
    #                             snapshots. Systematically lower than
    #                             external tools due to variance drag.
    #   sharpe_ratio_arithmetic : textbook, (annualized arith mean excess) /
    #                             ann_vol. Matches QuantStats / PyPortfolioOpt /
    #                             most finance textbooks. For external
    #                             comparison.
    # Annual arith mean = mean(period_return) * ppy; excess = subtract
    # annual risk-free rate. Invariant: arithmetic >= geometric (equality
    # iff vol = 0).
    sharpe = (cagr - risk_free_rate) / vol if (vol > 0 and cagr is not None) else None
    ann_arith_mean = mean_r * ppy
    sharpe_arithmetic = (ann_arith_mean - risk_free_rate) / vol if vol > 0 else None

    # Sortino ratio (downside deviation)
    # Denominator uses (n - 1) to match `variance` above — both are sample
    # estimators. Pre-fix this used `/ n` (population), making downside_dev
    # smaller than it should be and inflating Sortino relative to Sharpe.
    # See docs/AUDIT_FINDINGS.md, Phase 1.1.
    downside_sq = []
    for r in returns:
        diff = r - rf_period
        if diff < 0:
            downside_sq.append(diff ** 2)
        else:
            downside_sq.append(0.0)
    downside_var = sum(downside_sq) / (n - 1) if n > 1 else 0
    downside_dev = math.sqrt(downside_var) * math.sqrt(ppy)
    sortino = (cagr - risk_free_rate) / downside_dev if (downside_dev > 0 and cagr is not None) else None

    # Calmar ratio (None when MDD is zero or CAGR is not defined)
    calmar = cagr / abs(max_dd) if (max_dd != 0 and cagr is not None) else None

    # VaR 95% (historical method - 5th percentile).
    # Convention: "lower-quantile" — picks the return at sorted position
    # ceil(n * 0.05) - 1, i.e. the worst 5th-percentile observed return.
    # This is a discrete, observation-based VaR and differs from
    # numpy.percentile(returns, 5) which uses linear interpolation between
    # neighboring observations (convention "linear", default). For n=100
    # uniform samples the two diverge by ≤ one observation-width.
    # P2 item L42: documented; no behavioral change.
    sorted_returns = sorted(returns)
    var_index = max(0, int(math.ceil(n * 0.05)) - 1)
    var_95 = sorted_returns[var_index]

    # CVaR 95% (expected shortfall)
    tail_returns = [r for r in sorted_returns if r <= var_95]
    cvar_95 = sum(tail_returns) / len(tail_returns) if tail_returns else var_95

    # Best/worst period
    best_period = max(returns)
    worst_period = min(returns)

    # Pct negative periods
    neg_count = sum(1 for r in returns if r < 0)
    pct_negative = neg_count / n

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for r in returns:
        if r < 0:
            current_consec += 1
            if current_consec > max_consec:
                max_consec = current_consec
        else:
            current_consec = 0

    # Skewness
    if n >= 3 and variance > 0:
        std = math.sqrt(variance)
        skewness = (n / ((n - 1) * (n - 2))) * sum(((r - mean_r) / std) ** 3 for r in returns)
    else:
        skewness = None

    # Kurtosis (excess)
    if n >= 4 and variance > 0:
        std = math.sqrt(variance)
        m4 = sum(((r - mean_r) / std) ** 4 for r in returns)
        kurtosis = ((n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))) * m4 - \
                   (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))
    else:
        kurtosis = None

    return {
        "cagr": cagr,
        "total_return": total_return,
        "max_drawdown": max_dd,
        # P2 L43: emit 0 (no drawdown occurred) rather than None. None is
        # reserved for the n<2 / undefined case handled in _empty_metrics.
        "max_dd_duration_periods": max_dd_duration,
        "annualized_volatility": vol,
        "sharpe_ratio": sharpe,
        "sharpe_ratio_arithmetic": sharpe_arithmetic,
        "sortino_ratio": sortino,
        "calmar_ratio": calmar,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "best_period": best_period,
        "worst_period": worst_period,
        "pct_negative_periods": pct_negative,
        "max_consecutive_losses": max_consec,
        "skewness": skewness,
        "kurtosis": kurtosis,
    }


def _compute_comparison(port_returns, bench_returns, ppy, risk_free_rate,
                        port_cagr, bench_cagr):
    """Compute comparison metrics between portfolio and benchmark.

    port_cagr / bench_cagr can be None (e.g. an equity curve starting at 0 or
    a length-1 curve). When either is None, excess_cagr and alpha are
    propagated as None; other comparison metrics that don't depend on CAGR
    (tracking error, capture ratios, beta) are still computed.
    """
    n = len(port_returns)
    if n < 2:
        return {}

    # Excess returns
    excess = [p - b for p, b in zip(port_returns, bench_returns)]
    if port_cagr is not None and bench_cagr is not None:
        excess_cagr = port_cagr - bench_cagr
    else:
        excess_cagr = None

    # Win rate
    wins = sum(1 for e in excess if e > 0)
    win_rate = wins / n

    # Tracking error and information ratio
    excess_mean = sum(excess) / n
    excess_var = sum((e - excess_mean) ** 2 for e in excess) / (n - 1) if n > 1 else 0
    tracking_error = math.sqrt(excess_var) * math.sqrt(ppy)
    info_ratio = (excess_mean * ppy) / tracking_error if tracking_error > 0 else None

    # Up/down capture
    up_port = []
    up_bench = []
    down_port = []
    down_bench = []
    for p, b in zip(port_returns, bench_returns):
        if b > 0:
            up_port.append(p)
            up_bench.append(b)
        elif b < 0:
            down_port.append(p)
            down_bench.append(b)

    up_capture = None
    if up_bench:
        up_bench_mean = sum(up_bench) / len(up_bench)
        if up_bench_mean != 0:
            up_capture = (sum(up_port) / len(up_port)) / up_bench_mean

    down_capture = None
    if down_bench:
        down_bench_mean = sum(down_bench) / len(down_bench)
        if down_bench_mean != 0:
            down_capture = (sum(down_port) / len(down_port)) / down_bench_mean

    # Beta and Alpha (CAPM)
    port_mean = sum(port_returns) / n
    bench_mean = sum(bench_returns) / n
    cov_sum = sum((p - port_mean) * (b - bench_mean) for p, b in zip(port_returns, bench_returns))
    bench_var_sum = sum((b - bench_mean) ** 2 for b in bench_returns)

    if bench_var_sum > 0:
        beta = cov_sum / bench_var_sum
        # Jensen's alpha (annualized); None if either CAGR is undefined.
        if port_cagr is not None and bench_cagr is not None:
            alpha = port_cagr - (risk_free_rate + beta * (bench_cagr - risk_free_rate))
        else:
            alpha = None
    else:
        beta = None
        alpha = None

    return {
        "excess_cagr": excess_cagr,
        "win_rate": win_rate,
        "information_ratio": info_ratio,
        "tracking_error": tracking_error,
        "up_capture": up_capture,
        "down_capture": down_capture,
        "beta": beta,
        "alpha": alpha,
    }


def compute_drawdown_series(cumulative_values):
    """Compute rolling drawdown series from cumulative values.

    Args:
        cumulative_values: list[float] - cumulative growth values (e.g. [1.0, 1.05, 1.02, ...])

    Returns:
        list[float] - drawdown at each point (e.g. [0.0, 0.0, -0.0286, ...])
    """
    if not cumulative_values:
        return []

    peak = cumulative_values[0]
    drawdowns = []
    for v in cumulative_values:
        if v > peak:
            peak = v
        # Semantics for peak<=0 (undefined drawdown denominator): emit 0
        # rather than -1/NaN. A curve that starts at 0 and never grows has
        # no reference point to draw down from. P2 item L41.
        dd = (v - peak) / peak if peak > 0 else 0
        drawdowns.append(dd)
    return drawdowns


def compute_annual_returns(period_returns, benchmark_returns, period_dates,
                           periods_per_year):
    """Aggregate period returns to annual returns.

    Args:
        period_returns: list[float] - portfolio returns per period
        benchmark_returns: list[float] - benchmark returns per period
        period_dates: list[str] - ISO date strings (e.g. "2020-01-01")
        periods_per_year: int

    Returns:
        list[dict] with keys: year, portfolio, benchmark, excess
    """
    annual = {}
    for pr, br, d in zip(period_returns, benchmark_returns, period_dates):
        year = d[:4]
        if year not in annual:
            annual[year] = {"port_cum": 1.0, "bench_cum": 1.0, "n": 0}
        annual[year]["port_cum"] *= (1 + pr)
        annual[year]["bench_cum"] *= (1 + br)
        annual[year]["n"] += 1

    result = []
    for year in sorted(annual.keys()):
        d = annual[year]
        # Only include years with enough periods
        min_periods = max(1, periods_per_year // 2)
        if d["n"] >= min_periods:
            port_annual = d["port_cum"] - 1
            bench_annual = d["bench_cum"] - 1
            result.append({
                "year": int(year),
                "portfolio": port_annual,
                "benchmark": bench_annual,
                "excess": port_annual - bench_annual,
            })
    return result


def compute_rolling_cagr(period_returns, periods_per_year, window_years=3):
    """Compute rolling N-year CAGR.

    Args:
        period_returns: list[float]
        periods_per_year: int
        window_years: int - rolling window in years (default 3)

    Returns:
        list[tuple(int, float)] - (period_index, rolling_cagr) pairs
    """
    window = window_years * periods_per_year
    if len(period_returns) < window:
        return []

    result = []
    for i in range(window, len(period_returns) + 1):
        window_returns = period_returns[i - window:i]
        cum = 1.0
        for r in window_returns:
            cum *= (1 + r)
        if cum > 0:
            cagr = cum ** (1.0 / window_years) - 1
        else:
            cagr = -1.0
        result.append((i - 1, cagr))
    return result


def format_metrics(metrics, strategy_name="Strategy", benchmark_name="S&P 500"):
    """Format metrics dict for console display.

    Args:
        metrics: dict from compute_metrics()
        strategy_name: display name for portfolio column
        benchmark_name: display name for benchmark column
    """
    p = metrics["portfolio"]
    b = metrics["benchmark"]
    c = metrics["comparison"]

    lines = []
    lines.append("")
    lines.append("=" * 65)
    lines.append(f"  {strategy_name} vs {benchmark_name}")
    lines.append("=" * 65)

    def pct(v, decimals=2):
        if v is None:
            return "N/A".rjust(10)
        return f"{v * 100:>{9}.{decimals}f}%"

    def num(v, decimals=3):
        if v is None:
            return "N/A".rjust(10)
        return f"{v:>{10}.{decimals}f}"

    header = f"  {'Metric':<28} {strategy_name:>12} {benchmark_name:>12}"
    lines.append(header)
    lines.append("  " + "-" * 54)

    # Return metrics
    lines.append(f"  {'CAGR':<28} {pct(p.get('cagr'))} {pct(b.get('cagr'))}")
    lines.append(f"  {'Total Return':<28} {pct(p.get('total_return'), 1)} {pct(b.get('total_return'), 1)}")

    # Risk metrics
    lines.append(f"  {'Max Drawdown':<28} {pct(p.get('max_drawdown'))} {pct(b.get('max_drawdown'))}")
    lines.append(f"  {'Volatility (ann.)':<28} {pct(p.get('annualized_volatility'))} {pct(b.get('annualized_volatility'))}")
    lines.append(f"  {'VaR 95%':<28} {pct(p.get('var_95'))} {pct(b.get('var_95'))}")

    # Risk-adjusted. "Sharpe Ratio" is the geometric (CAGR-based) form for
    # leaderboard continuity; "Sharpe (arith.)" is the textbook form for
    # comparison with QuantStats / PyPortfolioOpt output.
    lines.append(f"  {'Sharpe Ratio':<28} {num(p.get('sharpe_ratio'))} {num(b.get('sharpe_ratio'))}")
    lines.append(f"  {'Sharpe (arith.)':<28} {num(p.get('sharpe_ratio_arithmetic'))} {num(b.get('sharpe_ratio_arithmetic'))}")
    lines.append(f"  {'Sortino Ratio':<28} {num(p.get('sortino_ratio'))} {num(b.get('sortino_ratio'))}")
    lines.append(f"  {'Calmar Ratio':<28} {num(p.get('calmar_ratio'))} {num(b.get('calmar_ratio'))}")

    # Comparison
    lines.append("")
    lines.append(f"  {'--- Relative ---':<28}")
    lines.append(f"  {'Excess CAGR':<28} {pct(c.get('excess_cagr'))}")
    lines.append(f"  {'Win Rate':<28} {pct(c.get('win_rate'), 1)}")
    lines.append(f"  {'Information Ratio':<28} {num(c.get('information_ratio'))}")
    lines.append(f"  {'Tracking Error':<28} {pct(c.get('tracking_error'))}")
    lines.append(f"  {'Up Capture':<28} {pct(c.get('up_capture'), 1)}")
    lines.append(f"  {'Down Capture':<28} {pct(c.get('down_capture'), 1)}")
    lines.append(f"  {'Beta':<28} {num(c.get('beta'))}")
    lines.append(f"  {'Alpha (Jensen)':<28} {pct(c.get('alpha'))}")

    lines.append("=" * 65)
    return "\n".join(lines)


def _empty_metrics():
    """Return empty metrics dict for edge cases (n < 2)."""
    empty_series = {
        "cagr": None, "total_return": None, "max_drawdown": None,
        "max_dd_duration_periods": None, "annualized_volatility": None,
        "sharpe_ratio": None, "sharpe_ratio_arithmetic": None,
        "sortino_ratio": None, "calmar_ratio": None,
        "var_95": None, "cvar_95": None, "best_period": None, "worst_period": None,
        "pct_negative_periods": None, "max_consecutive_losses": None,
        "skewness": None, "kurtosis": None,
    }
    empty_comp = {
        "excess_cagr": None, "win_rate": None, "information_ratio": None,
        "tracking_error": None, "up_capture": None, "down_capture": None,
        "beta": None, "alpha": None,
    }
    return {
        "portfolio": empty_series.copy(),
        "benchmark": empty_series.copy(),
        "comparison": empty_comp,
    }

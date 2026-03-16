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
    """Compute metrics for a single return series."""
    n = len(returns)
    if n < 2:
        return {}

    rf_period = risk_free_rate / ppy

    # Cumulative return and drawdown
    cumulative = 1.0
    peak = 1.0
    max_dd = 0.0
    dd_start = 0
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

        dd = (cumulative - peak) / peak if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # If still in drawdown at end, count duration to end
    if in_drawdown:
        duration = n - current_dd_start
        if duration > max_dd_duration:
            max_dd_duration = duration

    # CAGR
    years = n / ppy
    if cumulative > 0 and years > 0:
        cagr = cumulative ** (1.0 / years) - 1
    else:
        cagr = -1.0

    total_return = cumulative - 1

    # Volatility (annualized)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    vol = math.sqrt(variance) * math.sqrt(ppy)

    # Sharpe ratio
    sharpe = (cagr - risk_free_rate) / vol if vol > 0 else None

    # Sortino ratio (downside deviation)
    downside_sq = []
    for r in returns:
        diff = r - rf_period
        if diff < 0:
            downside_sq.append(diff ** 2)
        else:
            downside_sq.append(0.0)
    downside_var = sum(downside_sq) / n if n > 0 else 0
    downside_dev = math.sqrt(downside_var) * math.sqrt(ppy)
    sortino = (cagr - risk_free_rate) / downside_dev if downside_dev > 0 else None

    # Calmar ratio
    calmar = cagr / abs(max_dd) if max_dd != 0 else None

    # VaR 95% (historical method - 5th percentile)
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
        "max_dd_duration_periods": max_dd_duration if max_dd_duration > 0 else None,
        "annualized_volatility": vol,
        "sharpe_ratio": sharpe,
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
    """Compute comparison metrics between portfolio and benchmark."""
    n = len(port_returns)
    if n < 2:
        return {}

    # Excess returns
    excess = [p - b for p, b in zip(port_returns, bench_returns)]
    excess_cagr = port_cagr - bench_cagr

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
        # Jensen's alpha (annualized)
        alpha = port_cagr - (risk_free_rate + beta * (bench_cagr - risk_free_rate))
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

    # Risk-adjusted
    lines.append(f"  {'Sharpe Ratio':<28} {num(p.get('sharpe_ratio'))} {num(b.get('sharpe_ratio'))}")
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
        "sharpe_ratio": None, "sortino_ratio": None, "calmar_ratio": None,
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

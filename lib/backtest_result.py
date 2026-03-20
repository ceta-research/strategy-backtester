"""Standardized backtest result builder.

Collects equity curve, trades, and benchmark data during simulation,
then computes all metrics and outputs a standardized JSON file.

Single config:
    result = BacktestResult("buy_2day_high", {"tsl": 5}, "NIFTYBEES", "NSE", 10_000_000)
    for day in simulation:
        result.add_equity_point(epoch, portfolio_value)
        if trade_closed:
            result.add_trade(entry_epoch, exit_epoch, entry_price, exit_price, qty, ...)
    result.set_benchmark_values(epochs, values)
    result.compute()
    result.save("result.json")

Config sweep:
    sweep = SweepResult("buy_2day_high", "NIFTYBEES", "NSE", 10_000_000)
    for params in param_grid:
        r = BacktestResult("buy_2day_high", params, "NIFTYBEES", "NSE", 10_000_000)
        # ... run simulation, add equity points and trades ...
        r.set_benchmark_values(bm_epochs, bm_values)
        sweep.add_config(params, r)
    sweep.save("result.json", top_n=20)

See docs/BACKTEST_GUIDE.md for full schema and usage.
"""

import json
import os
from datetime import datetime, timezone

from lib.metrics import compute_metrics


class BacktestResult:
    """Collects simulation data and produces standardized output.

    Args:
        strategy_name: Short identifier (e.g. "buy_2day_high").
        params: Dict of strategy parameters (e.g. {"lookback": 3, "tsl": 5}).
        instrument: Traded instrument (e.g. "NIFTYBEES").
        exchange: Exchange code (e.g. "NSE").
        capital: Starting capital.
        slippage_bps: Slippage in basis points (default 5).
        description: Human-readable strategy description.
    """

    def __init__(self, strategy_name, params, instrument, exchange,
                 capital, slippage_bps=5, description=""):
        self.strategy = {
            "name": strategy_name,
            "description": description,
            "params": params,
            "instrument": instrument,
            "exchange": exchange,
            "capital": capital,
            "slippage_bps": slippage_bps,
        }
        self.equity_curve = []       # [(epoch, value)]
        self.trades = []             # [trade_dict]
        self.benchmark_values = []   # [(epoch, value)]
        self.costs = {"total_charges": 0.0, "total_slippage": 0.0}
        self._computed = None

    def add_equity_point(self, epoch, value):
        """Record daily portfolio value. Call once per trading day."""
        self.equity_curve.append((int(epoch), float(value)))

    def add_trade(self, entry_epoch, exit_epoch, entry_price, exit_price,
                  quantity, side="LONG", charges=0.0, slippage=0.0):
        """Record a completed trade."""
        if side == "LONG":
            gross_pnl = (exit_price - entry_price) * quantity
            pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
        else:
            gross_pnl = (entry_price - exit_price) * quantity
            pnl_pct = (entry_price / exit_price - 1) * 100 if exit_price > 0 else 0

        net_pnl = gross_pnl - charges - slippage
        hold_days = (exit_epoch - entry_epoch) / 86400

        self.trades.append({
            "entry_epoch": int(entry_epoch),
            "exit_epoch": int(exit_epoch),
            "entry_date": _epoch_to_date(entry_epoch),
            "exit_date": _epoch_to_date(exit_epoch),
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "quantity": int(quantity),
            "side": side,
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "hold_days": round(hold_days, 1),
            "charges": round(charges, 2),
            "slippage": round(slippage, 2),
        })
        self.costs["total_charges"] += charges
        self.costs["total_slippage"] += slippage

    def set_benchmark_values(self, epochs, values):
        """Set benchmark equity curve (e.g. buy-and-hold). Same length as equity_curve."""
        self.benchmark_values = list(zip(
            [int(e) for e in epochs],
            [float(v) for v in values],
        ))

    # ── Compute ──────────────────────────────────────────────────────────

    def compute(self):
        """Compute all metrics from collected data. Call after simulation ends."""
        if len(self.equity_curve) < 2:
            self._computed = self._empty_result()
            return self

        # Daily returns
        daily_returns = _returns_from_values([v for _, v in self.equity_curve])

        # Benchmark returns
        if len(self.benchmark_values) == len(self.equity_curve):
            bench_returns = _returns_from_values([v for _, v in self.benchmark_values])
        else:
            bench_returns = [0.0] * len(daily_returns)

        # Core metrics via lib/metrics.py
        core = compute_metrics(daily_returns, bench_returns, periods_per_year=252)

        # Time-series breakdowns (cached for reuse)
        monthly = self._monthly_returns()
        yearly = self._yearly_returns()

        # Build summary: core + trade + portfolio + time breakdowns
        summary = dict(core["portfolio"])
        summary.update(self._trade_metrics())
        summary.update(self._portfolio_metrics())
        summary.update(self._time_extremes(daily_returns, monthly, yearly))

        self._computed = {
            "version": "1.0",
            "type": "single",
            "strategy": self.strategy,
            "summary": summary,
            "benchmark": core["benchmark"],
            "comparison": core["comparison"],
            "equity_curve": [
                {"epoch": e, "date": _epoch_to_date(e), "value": round(v, 2)}
                for e, v in self.equity_curve
            ],
            "trades": self.trades,
            "monthly_returns": monthly,
            "yearly_returns": yearly,
            "costs": {
                "total_charges": round(self.costs["total_charges"], 2),
                "total_slippage": round(self.costs["total_slippage"], 2),
                "total_cost": round(self.costs["total_charges"] + self.costs["total_slippage"], 2),
                "cost_pct_of_capital": round(
                    (self.costs["total_charges"] + self.costs["total_slippage"])
                    / self.strategy["capital"] * 100, 2
                ),
            },
        }
        return self

    def to_dict(self):
        """Return computed result as a dict."""
        if self._computed is None:
            self.compute()
        return self._computed

    def save(self, path="result.json"):
        """Write result JSON to disk."""
        if self._computed is None:
            self.compute()
        with open(path, "w") as f:
            json.dump(self._computed, f, indent=2)
        size_kb = os.path.getsize(path) / 1024
        print(f"  Saved {path} ({size_kb:.0f} KB)")
        return path

    def print_summary(self):
        """Print a short summary to stdout (< 50KB safe)."""
        if self._computed is None:
            self.compute()
        s = self._computed["summary"]
        st = self._computed["strategy"]
        c = self._computed["costs"]
        print(f"\n{'='*60}")
        print(f"  {st['name']} | {st['instrument']} ({st['exchange']})")
        print(f"  Params: {st['params']}")
        print(f"{'='*60}")
        _print_metric("CAGR", s.get("cagr"), pct=True)
        _print_metric("Max Drawdown", s.get("max_drawdown"), pct=True)
        _print_metric("Sharpe", s.get("sharpe_ratio"))
        _print_metric("Sortino", s.get("sortino_ratio"))
        _print_metric("Calmar", s.get("calmar_ratio"))
        _print_metric("Volatility", s.get("annualized_volatility"), pct=True)
        _print_metric("VaR 95%", s.get("var_95"), pct=True)
        print(f"  {'─'*56}")
        _print_metric("Trades", s.get("total_trades"), fmt="d")
        _print_metric("Win Rate", s.get("win_rate"), pct=True)
        _print_metric("Profit Factor", s.get("profit_factor"))
        _print_metric("Payoff Ratio", s.get("payoff_ratio"))
        _print_metric("Avg Hold Days", s.get("avg_hold_days"), fmt=".0f")
        _print_metric("Time in Market", s.get("time_in_market"), pct=True)
        print(f"  {'─'*56}")
        _print_metric("Final Value", s.get("final_value"), fmt=",.0f")
        _print_metric("Peak Value", s.get("peak_value"), fmt=",.0f")
        print(f"  Total Costs:   {c['total_cost']:>12,.0f}  ({c['cost_pct_of_capital']:.1f}% of capital)")
        print(f"{'='*60}")

        # Year-wise table
        yearly = self._computed.get("yearly_returns", [])
        if yearly:
            print(f"\n  {'Year':<6} {'Return':>9} {'MaxDD':>9} {'EndValue':>14} {'Trades':>7}")
            print(f"  {'-'*48}")
            for y in yearly:
                print(f"  {y['year']:<6} {y['return']*100:>+8.1f}% {y['mdd']*100:>8.1f}% "
                      f"{y['end_value']:>14,.0f} {y['trades']:>7}")

    # ── Private: metric computation ──────────────────────────────────────

    def _monthly_returns(self):
        """Month-by-month returns keyed by year then month number."""
        if len(self.equity_curve) < 2:
            return {}
        # Group by (year, month) → first and last value
        buckets = {}
        for epoch, value in self.equity_curve:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            key = (dt.year, dt.month)
            if key not in buckets:
                buckets[key] = {"first": value, "last": value}
            buckets[key]["last"] = value

        result = {}
        sorted_keys = sorted(buckets.keys())
        for i, (year, month) in enumerate(sorted_keys):
            if i == 0:
                base = buckets[(year, month)]["first"]
            else:
                prev = sorted_keys[i - 1]
                base = buckets[prev]["last"]
            ret = buckets[(year, month)]["last"] / base - 1 if base > 0 else 0
            result.setdefault(str(year), {})[str(month)] = round(ret, 6)
        return result

    def _yearly_returns(self):
        """Year-by-year return, max drawdown, end value, trade count."""
        if len(self.equity_curve) < 2:
            return []
        yearly = {}
        for epoch, value in self.equity_curve:
            yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
            if yr not in yearly:
                yearly[yr] = {"first": value, "last": value, "peak": value, "trough": value}
            yearly[yr]["last"] = value
            yearly[yr]["peak"] = max(yearly[yr]["peak"], value)
            yearly[yr]["trough"] = min(yearly[yr]["trough"], value)

        result = []
        sorted_years = sorted(yearly.keys())
        for i, yr in enumerate(sorted_years):
            y = yearly[yr]
            if i == 0:
                base = y["first"]
            else:
                base = yearly[sorted_years[i - 1]]["last"]
            ret = y["last"] / base - 1 if base > 0 else 0
            mdd = (y["trough"] - y["peak"]) / y["peak"] if y["peak"] > 0 else 0
            trades = sum(
                1 for t in self.trades
                if datetime.fromtimestamp(t["entry_epoch"], tz=timezone.utc).year == yr
            )
            result.append({
                "year": yr,
                "return": round(ret, 6),
                "mdd": round(mdd, 6),
                "end_value": round(y["last"], 2),
                "trades": trades,
            })
        return result

    def _trade_metrics(self):
        """Profit factor, win rate, payoff, Kelly, hold duration, etc."""
        if not self.trades:
            return {
                "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
                "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None,
                "profit_factor": None, "payoff_ratio": None, "expectancy": None,
                "avg_hold_days": None, "max_consecutive_wins": None,
                "max_consecutive_losses_trades": None, "kelly_criterion": None,
            }

        wins = [t for t in self.trades if t["net_pnl"] > 0]
        losses = [t for t in self.trades if t["net_pnl"] <= 0]
        n = len(self.trades)
        wr = len(wins) / n

        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

        gross_win = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = gross_win / gross_loss if gross_loss > 0 else None

        payoff = abs(avg_win / avg_loss) if avg_loss != 0 else None
        expectancy = sum(t["net_pnl"] for t in self.trades) / n

        avg_hold = sum(t["hold_days"] for t in self.trades) / n

        # Consecutive streaks
        max_cw, max_cl, cw, cl = 0, 0, 0, 0
        for t in sorted(self.trades, key=lambda x: x["entry_epoch"]):
            if t["net_pnl"] > 0:
                cw += 1; cl = 0; max_cw = max(max_cw, cw)
            else:
                cl += 1; cw = 0; max_cl = max(max_cl, cl)

        kelly = (wr - (1 - wr) / payoff) if payoff and payoff > 0 else None

        return {
            "total_trades": n,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(wr, 4),
            "avg_win_pct": round(avg_win, 4),
            "avg_loss_pct": round(avg_loss, 4),
            "profit_factor": round(pf, 4) if pf is not None else None,
            "payoff_ratio": round(payoff, 4) if payoff is not None else None,
            "expectancy": round(expectancy, 2),
            "avg_hold_days": round(avg_hold, 1),
            "max_consecutive_wins": max_cw,
            "max_consecutive_losses_trades": max_cl,
            "kelly_criterion": round(kelly, 4) if kelly is not None else None,
        }

    def _portfolio_metrics(self):
        """Final value, peak value, time in market."""
        values = [v for _, v in self.equity_curve]
        epochs = [e for e, _ in self.equity_curve]
        total_days = (epochs[-1] - epochs[0]) / 86400 if len(epochs) > 1 else 1
        days_held = sum(t["hold_days"] for t in self.trades)
        return {
            "final_value": round(values[-1], 2),
            "peak_value": round(max(values), 2),
            "time_in_market": round(min(days_held / total_days, 1.0), 4) if total_days > 0 else 0,
        }

    def _time_extremes(self, daily_returns, monthly, yearly):
        """Best/worst day, month, year."""
        best_day = max(daily_returns) if daily_returns else None
        worst_day = min(daily_returns) if daily_returns else None

        all_monthly = [v for yr in monthly.values() for v in yr.values()]
        best_month = max(all_monthly) if all_monthly else None
        worst_month = min(all_monthly) if all_monthly else None

        yr_rets = [y["return"] for y in yearly]
        best_year = max(yr_rets) if yr_rets else None
        worst_year = min(yr_rets) if yr_rets else None

        return {
            "best_day": round(best_day, 6) if best_day is not None else None,
            "worst_day": round(worst_day, 6) if worst_day is not None else None,
            "best_month": round(best_month, 6) if best_month is not None else None,
            "worst_month": round(worst_month, 6) if worst_month is not None else None,
            "best_year": round(best_year, 6) if best_year is not None else None,
            "worst_year": round(worst_year, 6) if worst_year is not None else None,
        }

    def _empty_result(self):
        return {
            "version": "1.0", "type": "single",
            "strategy": self.strategy,
            "summary": {}, "benchmark": {}, "comparison": {},
            "equity_curve": [], "trades": [],
            "monthly_returns": {}, "yearly_returns": [], "costs": {},
        }


# ── Sweep Result ─────────────────────────────────────────────────────────

class SweepResult:
    """Collects results from a parameter sweep.

    All configs get summary metrics in the output. Only the top N
    (by sort_by metric) get full detail: equity curve, trades,
    monthly/yearly breakdowns.
    """

    def __init__(self, strategy_name, instrument, exchange, capital,
                 slippage_bps=5, description=""):
        self.meta = {
            "strategy_name": strategy_name,
            "description": description,
            "instrument": instrument,
            "exchange": exchange,
            "capital": capital,
            "slippage_bps": slippage_bps,
        }
        self.configs = []  # [(params_dict, BacktestResult)]

    def add_config(self, params, result):
        """Add a completed config. result.compute() is called if needed."""
        if result._computed is None:
            result.compute()
        self.configs.append((params, result))

    def save(self, path="result.json", top_n=20, sort_by="calmar_ratio"):
        """Save consolidated sweep result.

        Args:
            path: Output file path.
            top_n: Number of configs to include full detail for.
            sort_by: Metric key to sort configs by (descending).
        """
        sorted_configs = self._sorted(sort_by)

        # All configs: summary + params only
        all_summaries = []
        for params, result in sorted_configs:
            d = result.to_dict()
            all_summaries.append({"params": params, **d["summary"]})

        # Top N: full detail
        detailed = []
        for i, (params, result) in enumerate(sorted_configs[:top_n]):
            d = result.to_dict()
            detailed.append({
                "rank": i + 1,
                "params": params,
                "summary": d["summary"],
                "benchmark": d.get("benchmark", {}),
                "comparison": d.get("comparison", {}),
                "equity_curve": d.get("equity_curve", []),
                "trades": d.get("trades", []),
                "monthly_returns": d.get("monthly_returns", {}),
                "yearly_returns": d.get("yearly_returns", []),
                "costs": d.get("costs", {}),
            })

        output = {
            "version": "1.0",
            "type": "sweep",
            "meta": self.meta,
            "sort_by": sort_by,
            "total_configs": len(self.configs),
            "top_n_detailed": min(top_n, len(self.configs)),
            "all_configs": all_summaries,
            "detailed": detailed,
        }

        with open(path, "w") as f:
            json.dump(output, f)  # no indent for sweeps (size)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"  Saved {path} ({size_mb:.1f} MB, {len(self.configs)} configs, "
              f"top {min(top_n, len(self.configs))} detailed)")
        return path

    def print_leaderboard(self, top_n=20, sort_by="calmar_ratio"):
        """Print compact sweep leaderboard to stdout."""
        sorted_configs = self._sorted(sort_by)
        print(f"\n  SWEEP: {len(self.configs)} configs, sorted by {sort_by}")
        print(f"  {'#':<3} {'CAGR':>7} {'MDD':>7} {'Cal':>6} {'Shrp':>6} "
              f"{'Sort':>6} {'WR':>5} {'Trd':>4} {'Params'}")
        print(f"  {'-'*80}")
        for i, (params, result) in enumerate(sorted_configs[:top_n]):
            s = result.to_dict()["summary"]
            cagr = (s.get("cagr") or 0) * 100
            mdd = (s.get("max_drawdown") or 0) * 100
            cal = s.get("calmar_ratio") or 0
            sh = s.get("sharpe_ratio") or 0
            so = s.get("sortino_ratio") or 0
            wr = (s.get("win_rate") or 0) * 100
            tr = s.get("total_trades") or 0
            print(f"  {i+1:<3} {cagr:>+6.1f}% {mdd:>6.1f}% {cal:>6.2f} {sh:>6.2f} "
                  f"{so:>6.2f} {wr:>4.0f}% {tr:>4} {params}")

    def _sorted(self, sort_by):
        def key(item):
            d = item[1].to_dict()
            v = d.get("summary", {}).get(sort_by)
            return v if v is not None else float("-inf")
        return sorted(self.configs, key=key, reverse=True)


# ── Helpers ──────────────────────────────────────────────────────────────

def _epoch_to_date(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def _returns_from_values(values):
    """Compute period returns from a value series."""
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            returns.append(values[i] / values[i - 1] - 1)
        else:
            returns.append(0.0)
    return returns


def _print_metric(label, value, pct=False, fmt=".2f"):
    if value is None:
        print(f"  {label + ':':<17} {'N/A':>12}")
    elif pct:
        print(f"  {label + ':':<17} {value * 100:>11.1f}%")
    elif fmt == "d":
        print(f"  {label + ':':<17} {int(value):>12}")
    else:
        print(f"  {label + ':':<17} {value:>12{fmt}}")

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

from lib.metrics import compute_metrics_from_curve
from lib.equity_curve import EquityCurve, Frequency


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
                 capital, slippage_bps=5, description="", risk_free_rate=0.02,
                 equity_curve_frequency=Frequency.DAILY_CALENDAR):
        """
        equity_curve_frequency: sampling rate of add_equity_point() calls.
            Default DAILY_CALENDAR matches the engine's forward-filled output.
            Use DAILY_TRADING for strategies that only emit points on trading
            days (e.g. intraday pipelines, standalone index scripts).
        """
        self.strategy = {
            "name": strategy_name,
            "description": description,
            "params": params,
            "instrument": instrument,
            "exchange": exchange,
            "capital": capital,
            "slippage_bps": slippage_bps,
            "risk_free_rate": risk_free_rate,
        }
        self._risk_free_rate = risk_free_rate
        self._frequency = equity_curve_frequency
        self.equity_curve = []       # [(epoch, value)]
        self.trades = []             # [trade_dict]
        self.benchmark_values = []   # [(epoch, value)]
        self.costs = {"total_charges": 0.0, "total_slippage": 0.0}
        self._computed = None

    def add_equity_point(self, epoch, value):
        """Record daily portfolio value. Call once per trading day."""
        self.equity_curve.append((int(epoch), float(value)))

    def add_trade(self, entry_epoch, exit_epoch, entry_price, exit_price,
                  quantity, side="LONG", charges=0.0, slippage=0.0,
                  symbol="", exit_reason=""):
        """Record a completed trade.

        Args:
            symbol: Instrument symbol (e.g. "INFY", "TCS").
            exit_reason: Why the trade was closed ("tsl", "max_hold", "end_of_sim",
                         "peak_recovery", etc.).
        """
        if side == "LONG":
            gross_pnl = (exit_price - entry_price) * quantity
            pnl_pct = (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
        else:
            gross_pnl = (entry_price - exit_price) * quantity
            pnl_pct = (entry_price / exit_price - 1) * 100 if exit_price > 0 else 0

        net_pnl = gross_pnl - charges - slippage
        hold_days = (exit_epoch - entry_epoch) / 86400

        trade = {
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
        }
        if symbol:
            trade["symbol"] = symbol
        if exit_reason:
            trade["exit_reason"] = exit_reason
        self.trades.append(trade)
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

        # Build typed EquityCurve for metrics. CAGR is now wall-clock based
        # and independent of sampling frequency; vol annualization uses the
        # frequency-declared periods_per_year. See lib/equity_curve.py.
        port_curve = EquityCurve.from_pairs(self.equity_curve, self._frequency)

        if len(self.benchmark_values) == len(self.equity_curve):
            bench_curve = EquityCurve.from_pairs(self.benchmark_values, self._frequency)
        else:
            bench_curve = None

        # Daily returns retained for time-series outputs (best/worst day etc.)
        daily_returns = port_curve.period_returns()

        # Core metrics via lib/metrics.py (EquityCurve path)
        core = compute_metrics_from_curve(port_curve, bench_curve,
                                          risk_free_rate=self._risk_free_rate)

        # Time-series breakdowns (cached for reuse)
        monthly = self._monthly_returns()
        yearly = self._yearly_returns()

        # Build summary: core + trade + portfolio + time breakdowns
        summary = dict(core["portfolio"])
        summary.update(self._trade_metrics())
        summary.update(self._portfolio_metrics())
        summary.update(self._time_extremes(daily_returns, monthly, yearly))

        self._computed = {
            "version": "1.1",  # v1.1: adds equity_curve_frequency for migration safety
            "type": "single",
            "strategy": self.strategy,
            "equity_curve_frequency": self._frequency.name,
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

    def compact(self):
        """Free heavy data after compute() to reduce memory in large sweeps.

        Strips everything except summary, strategy, benchmark, comparison,
        and costs from the cached result. After compact(), to_dict() still
        returns a valid dict but equity_curve, trades, monthly/yearly
        breakdowns will be empty.

        Call after sweep.add_config() in loops with 1000+ configs.
        """
        if self._computed is None:
            self.compute()
        # Clear raw data
        self.equity_curve = []
        self.trades = []
        self.benchmark_values = []
        # Strip heavy serialized data from cached dict
        if self._computed:
            self._computed["equity_curve"] = []
            self._computed["trades"] = []
            self._computed["monthly_returns"] = {}
            self._computed["yearly_returns"] = []
        return self

    def save(self, path="result.json"):
        """Write result JSON to disk."""
        if self._computed is None:
            self.compute()
        with open(path, "w") as f:
            json.dump(self._computed, f, indent=2)
        size_kb = os.path.getsize(path) / 1024
        print(f"  Saved {path} ({size_kb:.0f} KB)")

        # Append summary to catalog
        try:
            meta = {
                "strategy_name": self.strategy["name"],
                "exchange": self.strategy["exchange"],
                "capital": self.strategy["capital"],
                "slippage_bps": self.strategy.get("slippage_bps", 0),
            }
            entry = {"params": self.strategy["params"],
                     **self._computed["summary"]}
            _append_to_catalog(meta, [entry], result_file=path)
        except Exception as e:
            print(f"  Warning: catalog append failed: {e}")

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
        yearly_mdd = {}
        for epoch, value in self.equity_curve:
            yr = datetime.fromtimestamp(epoch, tz=timezone.utc).year
            if yr not in yearly:
                yearly[yr] = {"first": value, "last": value}
                yearly_mdd[yr] = {"peak": value, "max_dd": 0.0}
            yearly[yr]["last"] = value
            # Proper peak-then-trough MDD: track running peak within year
            if value > yearly_mdd[yr]["peak"]:
                yearly_mdd[yr]["peak"] = value
            dd = (yearly_mdd[yr]["peak"] - value) / yearly_mdd[yr]["peak"] if yearly_mdd[yr]["peak"] > 0 else 0
            if dd > yearly_mdd[yr]["max_dd"]:
                yearly_mdd[yr]["max_dd"] = dd

        result = []
        sorted_years = sorted(yearly.keys())
        for i, yr in enumerate(sorted_years):
            y = yearly[yr]
            if i == 0:
                base = y["first"]
            else:
                base = yearly[sorted_years[i - 1]]["last"]
            ret = y["last"] / base - 1 if base > 0 else 0
            mdd = -yearly_mdd[yr]["max_dd"]
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

    def _to_dict(self, top_n=20, sort_by="calmar_ratio"):
        """Build sweep output dict without writing to disk."""
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

        return {
            "version": "1.0",
            "type": "sweep",
            "meta": self.meta,
            "sort_by": sort_by,
            "total_configs": len(self.configs),
            "top_n_detailed": min(top_n, len(self.configs)),
            "all_configs": all_summaries,
            "detailed": detailed,
        }

    def save(self, path="result.json", top_n=20, sort_by="calmar_ratio"):
        """Save consolidated sweep result.

        Args:
            path: Output file path.
            top_n: Number of configs to include full detail for.
            sort_by: Metric key to sort configs by (descending).
        """
        output = self._to_dict(top_n, sort_by)
        with open(path, "w") as f:
            json.dump(output, f)  # no indent for sweeps (size)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"  Saved {path} ({size_mb:.1f} MB, {len(self.configs)} configs, "
              f"top {min(top_n, len(self.configs))} detailed)")

        # Append top-10 summaries to catalog
        try:
            _append_to_catalog(self.meta, output["all_configs"], sort_by,
                               result_file=path)
        except Exception as e:
            print(f"  Warning: catalog append failed: {e}")

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


# ── Multi-Sweep Result ───────────────────────────────────────────────────

class MultiSweepResult:
    """Container for scripts that run multiple named sweeps.

    Usage:
        multi = MultiSweepResult("alpha_variations", "8 variation sweeps on SPY vs EWJ")
        multi.add_sweep("zscore_params", sweep_result_1)
        multi.add_sweep("trend_filter", sweep_result_2)
        multi.print_leaderboard(top_n=10)
        multi.save("result.json")
    """

    def __init__(self, strategy_name, description=""):
        self.meta = {"strategy_name": strategy_name, "description": description}
        self.sweeps = {}  # name -> SweepResult

    def add_sweep(self, name, sweep_result):
        """Add a named sweep."""
        self.sweeps[name] = sweep_result

    def print_leaderboard(self, top_n=10, sort_by="calmar_ratio"):
        """Print leaderboards for all sweeps."""
        for name, sr in self.sweeps.items():
            print(f"\n  === {name} ===")
            sr.print_leaderboard(top_n=top_n, sort_by=sort_by)

    def save(self, path="result.json", top_n=20, sort_by="calmar_ratio"):
        """Save all sweeps into a single JSON file."""
        output = {
            "version": "1.0",
            "type": "multi_sweep",
            "meta": self.meta,
            "total_sweeps": len(self.sweeps),
            "sweeps": {
                name: sr._to_dict(top_n, sort_by)
                for name, sr in self.sweeps.items()
            },
        }
        with open(path, "w") as f:
            json.dump(output, f)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        print(f"  Saved {path} ({size_mb:.1f} MB, {len(self.sweeps)} sweeps)")

        # Append each sweep's top-10 to catalog
        for name, sr in self.sweeps.items():
            try:
                meta = {**sr.meta, "strategy_name": f"{self.meta['strategy_name']}:{name}"}
                sweep_dict = sr._to_dict(top_n, sort_by)
                _append_to_catalog(meta, sweep_dict["all_configs"], sort_by,
                                   result_file=path)
            except Exception as e:
                print(f"  Warning: catalog append for {name} failed: {e}")

        return path


# ── Result Catalog ───────────────────────────────────────────────────────

CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "results", "catalog.jsonl")

CATALOG_FIELDS = [
    "cagr", "total_return", "max_drawdown", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "total_trades", "win_rate", "avg_hold_days", "profit_factor",
    "final_value", "worst_year",
]


def _append_to_catalog(meta, configs_with_params, sort_by="calmar_ratio",
                       result_file=None, top_n=10):
    """Append a run summary to results/catalog.jsonl.

    Each line is one run: meta + top-N configs with params and key metrics.
    Lightweight (~1 KB per config), append-only, easy to grep/query.
    """
    top = configs_with_params[:top_n]
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": meta.get("strategy_name", ""),
        "exchange": meta.get("exchange", ""),
        "capital": meta.get("capital", 0),
        "slippage_bps": meta.get("slippage_bps", 0),
        "total_configs": len(configs_with_params),
        "sort_by": sort_by,
        "result_file": result_file,
        "top": [],
    }
    for item in top:
        params = item.get("params", {})
        row = {"params": params}
        for f in CATALOG_FIELDS:
            row[f] = item.get(f)
        entry["top"].append(row)

    os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
    with open(CATALOG_PATH, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    # Trim: keep only last MAX_CATALOG_RUNS entries per strategy:exchange
    _trim_catalog()


MAX_CATALOG_RUNS = 5  # Keep last N runs per strategy:exchange


def _trim_catalog():
    """Keep only the latest MAX_CATALOG_RUNS entries per strategy:exchange key."""
    if not os.path.exists(CATALOG_PATH):
        return
    try:
        with open(CATALOG_PATH) as f:
            lines = f.readlines()
        if len(lines) < 20:  # Don't bother trimming small catalogs
            return

        # Group by strategy:exchange, keep latest N per group
        from collections import defaultdict
        groups = defaultdict(list)
        for line in lines:
            entry = json.loads(line)
            key = f"{entry.get('strategy', '')}|{entry.get('exchange', '')}"
            groups[key].append(line)

        trimmed = []
        for key, entries in groups.items():
            trimmed.extend(entries[-MAX_CATALOG_RUNS:])

        if len(trimmed) < len(lines):
            with open(CATALOG_PATH, "w") as f:
                f.writelines(trimmed)
    except Exception:
        pass  # Never fail on trim


# ── Helpers ──────────────────────────────────────────────────────────────

def _epoch_to_date(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def _print_metric(label, value, pct=False, fmt=".2f"):
    if value is None:
        print(f"  {label + ':':<17} {'N/A':>12}")
    elif pct:
        print(f"  {label + ':':<17} {value * 100:>11.1f}%")
    elif fmt == "d":
        print(f"  {label + ':':<17} {int(value):>12}")
    else:
        print(f"  {label + ':':<17} {value:>12{fmt}}")

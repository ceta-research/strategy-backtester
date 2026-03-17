"""Intraday backtesting pipeline orchestrator.

Separate from the EOD pipeline (pipeline.py). All signal logic lives in SQL;
Python handles portfolio simulation and metrics only.

Supports config sweeps: SQL-affecting params generate unique queries,
simulation-only params reuse the same query results.
"""

import os
import sys
import time
from datetime import datetime, timedelta
from itertools import product

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.intraday_sql_builder import build_orb_sql, build_orb_signal_sql
from engine.intraday_simulator import simulate_intraday
from engine.intraday_simulator_v2 import simulate_intraday_v2
from lib.cr_client import CetaResearch
from lib.metrics import compute_metrics


# Map strategy_name -> SQL builder function
SQL_BUILDERS = {
    "orb": build_orb_sql,
}

# Keys that affect SQL (changing them requires a new query)
SQL_KEYS = {"min_volume", "min_price", "min_range_pct", "or_window",
            "max_entry_bar", "target_pct", "stop_pct", "max_hold_bars"}

# Keys that only affect simulation (reuse same query results)
SIM_KEYS = {"max_positions", "order_value"}

# v2: exit logic moves from SQL to Python simulator
SQL_BUILDERS_V2 = {
    "orb": build_orb_signal_sql,
}

# v2: target_pct, stop_pct, max_hold_bars are SIM keys (not SQL)
SQL_KEYS_V2 = {"min_volume", "min_price", "min_range_pct", "or_window", "max_entry_bar"}

SIM_KEYS_V2 = {"max_positions", "order_value", "target_pct", "stop_pct", "max_hold_bars",
               "trailing_stop_pct", "min_hold_bars", "use_bar_hilo",
               "sizing_type", "sizing_pct", "max_order_value",
               "max_positions_per_instrument",
               "ranking_type", "ranking_window_days"}

# Exchange-specific SQL defaults for symbol/exchange filtering
EXCHANGE_SQL_DEFAULTS = {
    "NSE":    {"symbol_filter": "symbol LIKE '%.NS'",    "exchange_filter": "m.exchange = 'NSE'"},
    "NASDAQ": {"symbol_filter": "symbol NOT LIKE '%.%'", "exchange_filter": "m.exchange = 'NASDAQ'"},
    "NYSE":   {"symbol_filter": "symbol NOT LIKE '%.%'", "exchange_filter": "m.exchange = 'NYSE'"},
    "AMEX":   {"symbol_filter": "symbol NOT LIKE '%.%'", "exchange_filter": "m.exchange = 'AMEX'"},
}


def run_intraday_pipeline(config_path: str) -> list:
    """Run intraday backtesting pipeline (dispatches to v1 or v2).

    Args:
        config_path: path to YAML config file

    Returns:
        list of result dicts sorted by Calmar ratio (descending)
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    version = raw.get("static", {}).get("pipeline_version", "v2")
    if version == "v1":
        return _run_pipeline(config_path, raw,
                             SQL_BUILDERS, SQL_KEYS, SIM_KEYS,
                             simulate_intraday, "v1")
    return _run_pipeline(config_path, raw,
                         SQL_BUILDERS_V2, SQL_KEYS_V2, SIM_KEYS_V2,
                         simulate_intraday_v2, "v2")


def _run_pipeline(config_path, raw, sql_builders, sql_keys, sim_keys,
                  simulate_fn, version_label):
    """Shared pipeline logic for v1 and v2."""
    pipeline_start = time.time()
    print(f"Loading config ({version_label}): {config_path}")

    static = raw["static"]
    strategy_name = static.get("strategy_name", "orb")
    build_sql = sql_builders.get(strategy_name)
    if not build_sql:
        print(f"Unknown intraday strategy: {strategy_name}")
        return []

    risk_free_rate = static.get("risk_free_rate", 0.065)
    initial_capital = static.get("initial_capital", 500_000)
    exchange = static.get("exchange", "NSE")

    exchange_defaults = EXCHANGE_SQL_DEFAULTS.get(exchange, EXCHANGE_SQL_DEFAULTS["NSE"])

    # Build all param combos from scanner + entry + exit + simulation sections
    all_params = {}
    for section in ("scanner", "entry", "exit", "simulation"):
        if section in raw:
            for key, val in raw[section].items():
                all_params[key] = val if isinstance(val, list) else [val]

    # Separate into SQL-affecting and simulation-only
    sql_params = {}
    sim_params = {}
    for key, vals in all_params.items():
        if key in sim_keys:
            sim_params[key] = vals
        else:
            sql_params[key] = vals

    sql_combos = list(_cartesian(sql_params))
    sim_combos = list(_cartesian(sim_params))
    total_configs = len(sql_combos) * len(sim_combos)

    print(f"Config combinations: {len(sql_combos)} SQL x {len(sim_combos)} sim = {total_configs} total")

    client = CetaResearch()

    all_results = []
    config_num = 0

    for sql_idx, sql_cfg in enumerate(sql_combos):
        full_sql_cfg = {
            "start_date": static["start_date"],
            "end_date": static["end_date"],
            **exchange_defaults,
            **sql_cfg,
        }

        query_start = time.time()
        print(f"\n  SQL config {sql_idx + 1}/{len(sql_combos)}: {sql_cfg}")

        try:
            if version_label == "v2":
                query_data = _query_chunked(client, build_sql, full_sql_cfg,
                                            chunk_months=12)
            else:
                sql = build_sql(full_sql_cfg)
                query_data = client.query(sql, memory_mb=16384, threads=6,
                                          timeout=600)
        except Exception as e:
            print(f"    Query failed: {e}")
            config_num += len(sim_combos)
            continue

        query_elapsed = round(time.time() - query_start, 1)
        print(f"    {len(query_data)} rows returned ({query_elapsed}s)")

        if not query_data:
            config_num += len(sim_combos)
            continue

        for sim_cfg in sim_combos:
            config_num += 1
            full_sim_cfg = {
                "initial_capital": initial_capital,
                "exchange": exchange,
                **sim_cfg,
            }

            sim_result = simulate_fn(query_data, full_sim_cfg)
            daily_rets = sim_result["daily_returns"]
            bench_rets = sim_result["bench_returns"]

            config_id = _make_config_id(sql_cfg, sim_cfg)

            if len(daily_rets) < 2:
                result = {
                    "config_id": config_id,
                    "sql_config": sql_cfg,
                    "sim_config": sim_cfg,
                    "trade_count": sim_result["trade_count"],
                    "active_days": len(daily_rets),
                    "cagr": None, "max_drawdown": None, "calmar_ratio": None,
                }
                all_results.append(result)
                print(f"    config {config_num}/{total_configs}: {config_id} | "
                      f"<2 active days, skipping metrics")
                continue

            metrics = compute_metrics(
                daily_rets, bench_rets,
                periods_per_year=252,
                risk_free_rate=risk_free_rate,
            )

            port = metrics["portfolio"]
            comp = metrics["comparison"]

            cagr = port.get("cagr") or 0
            max_dd = port.get("max_drawdown") or 0
            calmar = port.get("calmar_ratio")
            win_rate = (sim_result["win_count"] / sim_result["trade_count"] * 100
                        if sim_result["trade_count"] > 0 else 0)

            start_value = full_sim_cfg["initial_capital"]
            end_value = sim_result["day_wise_log"][-1]["margin_available"] if sim_result["day_wise_log"] else start_value

            result = {
                "config_id": config_id,
                "sql_config": sql_cfg,
                "sim_config": sim_cfg,
                "trade_count": sim_result["trade_count"],
                "win_count": sim_result["win_count"],
                "win_rate": round(win_rate, 1),
                "active_days": len(daily_rets),
                "cagr": port.get("cagr"),
                "total_return": port.get("total_return"),
                "max_drawdown": port.get("max_drawdown"),
                "calmar_ratio": calmar,
                "sharpe_ratio": port.get("sharpe_ratio"),
                "sortino_ratio": port.get("sortino_ratio"),
                "annualized_volatility": port.get("annualized_volatility"),
                "var_95": port.get("var_95"),
                "excess_cagr": comp.get("excess_cagr"),
                "start_value": start_value,
                "end_value": end_value,
                "day_wise_log": sim_result["day_wise_log"],
                "trade_log": sim_result["trade_log"],
                "full_metrics": metrics,
            }
            all_results.append(result)

            calmar_str = f" Calmar={calmar:.3f}" if calmar else ""
            print(f"    config {config_num}/{total_configs}: {config_id} | "
                  f"{sim_result['trade_count']} trades, {len(daily_rets)} days | "
                  f"CAGR={cagr * 100:.1f}% MaxDD={max_dd * 100:.1f}%{calmar_str}")

    all_results.sort(key=lambda r: r.get("calmar_ratio") or 0, reverse=True)

    elapsed = round(time.time() - pipeline_start, 1)
    print(f"\n--- Intraday Pipeline {version_label} Complete: {elapsed}s ---")
    if all_results and all_results[0].get("cagr") is not None:
        best = all_results[0]
        print(f"  Best config: {best['config_id']}")
        print(f"  CAGR: {best['cagr'] * 100:.2f}%")
        print(f"  Max Drawdown: {best['max_drawdown'] * 100:.2f}%")
        print(f"  Calmar: {best.get('calmar_ratio', 'N/A')}")

    return all_results


def _date_chunks(start_date: str, end_date: str, months: int = 12) -> list:
    """Split a date range into chunks of approximately `months` months."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    chunks = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=months * 30), end)
        chunks.append((chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks


def _query_chunked(client, build_sql, full_sql_cfg, chunk_months=12):
    """Run SQL in date-range chunks, merging results.

    v2 signal matrix queries return all bars from entry onward (~200K rows
    per month). For multi-year ranges this exceeds memory. Chunking by year
    keeps each query under ~2.5M rows.
    """
    start_date = full_sql_cfg["start_date"]
    end_date = full_sql_cfg["end_date"]
    chunks = _date_chunks(start_date, end_date, chunk_months)

    if len(chunks) <= 1:
        return client.query(build_sql(full_sql_cfg),
                            memory_mb=16384, threads=6, timeout=600)

    all_rows = []
    for i, (cs, ce) in enumerate(chunks):
        chunk_cfg = {**full_sql_cfg, "start_date": cs, "end_date": ce}
        sql = build_sql(chunk_cfg)
        print(f"      chunk {i+1}/{len(chunks)}: {cs} to {ce}", end="", flush=True)
        try:
            rows = client.query(sql, memory_mb=16384, threads=6, timeout=600)
            all_rows.extend(rows)
            print(f" -> {len(rows)} rows")
        except Exception as e:
            print(f" -> failed: {e}")
    return all_rows


def _cartesian(params: dict) -> list:
    """Generate Cartesian product of param dict values."""
    if not params:
        return [{}]
    keys = list(params.keys())
    vals = [params[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in product(*vals)]


def _make_config_id(sql_cfg: dict, sim_cfg: dict) -> str:
    """Create a compact config ID string."""
    parts = []
    for k, v in {**sql_cfg, **sim_cfg}.items():
        # Abbreviate key names
        short = k.replace("min_", "").replace("max_", "mx").replace("_pct", "")
        parts.append(f"{short}={v}")
    return "_".join(parts)

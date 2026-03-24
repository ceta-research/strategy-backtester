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

from engine.intraday_sql_builder import (
    build_orb_sql, build_orb_signal_sql, build_rvol_atr_sql,
    build_vwap_mr_signal_sql,
)
from engine.intraday_simulator import simulate_intraday, _date_to_epoch
from engine.intraday_simulator_v2 import simulate_intraday_v2
from lib.backtest_result import BacktestResult, SweepResult
from lib.cr_client import CetaResearch


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
    "vwap_mr": build_vwap_mr_signal_sql,
}

# v2: target_pct, stop_pct, max_hold_bars are SIM keys (not SQL)
SQL_KEYS_V2 = {"min_volume", "min_price", "min_range_pct", "or_window", "max_entry_bar",
               "min_rvol", "warmup_bars", "dip_pct"}

SIM_KEYS_V2 = {"max_positions", "order_value", "target_pct", "stop_pct", "max_hold_bars",
               "trailing_stop_pct", "min_hold_bars", "use_bar_hilo",
               "eod_buffer_bars",
               "time_stop_bars", "use_atr_stop", "atr_multiplier", "exit_reentry_range",
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


def run_intraday_pipeline(config_path: str) -> SweepResult:
    """Run intraday backtesting pipeline (dispatches to v1 or v2).

    Args:
        config_path: path to YAML config file

    Returns:
        SweepResult with BacktestResult per config
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
        return SweepResult(strategy_name, "PORTFOLIO", "UNKNOWN", 0)

    risk_free_rate = static.get("risk_free_rate", 0.065)
    initial_capital = static.get("initial_capital", 500_000)
    exchange = static.get("exchange", "NSE")

    exchange_defaults = EXCHANGE_SQL_DEFAULTS.get(exchange, EXCHANGE_SQL_DEFAULTS["NSE"])

    sweep = SweepResult(strategy_name, "PORTFOLIO", exchange, initial_capital,
                        description=f"Intraday {version_label} sweep")

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
                exchange = static.get("exchange", "NSE")
                # US markets have 5-10x more liquid stocks; use smaller chunks.
                # VWAP MR also benefits from smaller chunks (dense signal matrix).
                default_months = 3 if exchange in ("NASDAQ", "NYSE", "AMEX") else 6
                months = static.get("chunk_months", default_months)
                query_data, row_count = _query_and_build_entries(
                    client, build_sql, full_sql_cfg, chunk_months=months)
                # query_data is now a pre-built entries_by_date dict
            else:
                sql = build_sql(full_sql_cfg)
                query_data = client.query(sql, memory_mb=16384, threads=6, disk_mb=40960,
                                          timeout=600)
                row_count = len(query_data)
        except Exception as e:
            print(f"    Query failed: {e}")
            config_num += len(sim_combos)
            continue

        query_elapsed = round(time.time() - query_start, 1)
        if version_label == "v2":
            n_entries = sum(len(v) for v in query_data.values())
            print(f"    {row_count} rows -> {n_entries} entries across {len(query_data)} days ({query_elapsed}s)")
            # Second pass: enrich with RVOL + ATR (separate lightweight query)
            query_data = _enrich_entries_rvol_atr(client, query_data, full_sql_cfg)
            n_after = sum(len(v) for v in query_data.values())
            if n_after != n_entries:
                print(f"    After RVOL filter: {n_after} entries across {len(query_data)} days")
        else:
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

            config_id = _make_config_id(sql_cfg, sim_cfg)
            params = {**sql_cfg, **sim_cfg}

            # Build BacktestResult
            br = BacktestResult(strategy_name, params, "PORTFOLIO", exchange,
                                initial_capital, risk_free_rate=risk_free_rate)

            # Feed equity points
            for day in sim_result["day_wise_log"]:
                br.add_equity_point(day["log_date_epoch"], day["margin_available"])

            # Feed trades
            for t in sim_result["trade_log"]:
                trade_epoch = _date_to_epoch(t["trade_date"])
                ov = t.get("order_value") or full_sim_cfg.get("order_value", initial_capital)
                qty = max(int(ov / t["entry_price"]), 1) if t["entry_price"] > 0 else 1
                br.add_trade(trade_epoch, trade_epoch, t["entry_price"], t["exit_price"],
                             qty, charges=t.get("charges", 0))

            sweep.add_config(params, br)

            # Console output
            s = br.to_dict().get("summary", {})
            cagr = (s.get("cagr") or 0) * 100
            max_dd = (s.get("max_drawdown") or 0) * 100
            calmar = s.get("calmar_ratio")
            calmar_str = f" Calmar={calmar:.3f}" if calmar else ""
            print(f"    config {config_num}/{total_configs}: {config_id} | "
                  f"{sim_result['trade_count']} trades, {len(daily_rets)} days | "
                  f"CAGR={cagr:.1f}% MaxDD={max_dd:.1f}%{calmar_str}")

    elapsed = round(time.time() - pipeline_start, 1)
    print(f"\n--- Intraday Pipeline {version_label} Complete: {elapsed}s ---")
    if sweep.configs:
        best_params, best_result = sweep._sorted("calmar_ratio")[0]
        bs = best_result.to_dict()["summary"]
        print(f"  Best config: {best_params}")
        print(f"  CAGR: {(bs.get('cagr') or 0) * 100:.2f}%")
        print(f"  Max Drawdown: {(bs.get('max_drawdown') or 0) * 100:.2f}%")
        print(f"  Calmar: {bs.get('calmar_ratio', 'N/A')}")

    return sweep


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
                            memory_mb=16384, threads=6, disk_mb=40960, timeout=600)

    all_rows = []
    for i, (cs, ce) in enumerate(chunks):
        chunk_cfg = {**full_sql_cfg, "start_date": cs, "end_date": ce}
        sql = build_sql(chunk_cfg)
        print(f"      chunk {i+1}/{len(chunks)}: {cs} to {ce}", end="", flush=True)
        try:
            rows = client.query(sql, memory_mb=16384, threads=6, disk_mb=40960, timeout=600)
            all_rows.extend(rows)
            print(f" -> {len(rows)} rows")
        except Exception as e:
            print(f" -> failed: {e}")
    return all_rows


def _query_and_build_entries(client, build_sql, full_sql_cfg, chunk_months=12):
    """Query signal matrix in chunks and build entry signals incrementally.

    Instead of accumulating all rows (~8M+) into a flat list, builds the
    structured entries_by_date dict chunk by chunk. Each chunk's raw rows
    are freed immediately after processing, keeping peak memory to ~1 chunk
    + the growing entries dict.

    Returns:
        (entries_by_date, total_rows) where entries_by_date is a dict
        keyed by trade_date string, ready for simulate_intraday_v2.
    """
    from engine.intraday_simulator_v2 import _build_entry_signals

    start_date = full_sql_cfg["start_date"]
    end_date = full_sql_cfg["end_date"]
    chunks = _date_chunks(start_date, end_date, chunk_months)

    all_entries = {}
    total_rows = 0

    for i, (cs, ce) in enumerate(chunks):
        chunk_cfg = {**full_sql_cfg, "start_date": cs, "end_date": ce}
        sql = build_sql(chunk_cfg)
        print(f"      chunk {i+1}/{len(chunks)}: {cs} to {ce}", end="", flush=True)
        try:
            rows = client.query(sql, memory_mb=16384, threads=6, disk_mb=40960, timeout=600)
            total_rows += len(rows)
            print(f" -> {len(rows)} rows")

            # Build entry signals from this chunk and merge
            chunk_entries = _build_entry_signals(rows)
            for date, entries in chunk_entries.items():
                all_entries.setdefault(date, []).extend(entries)
            del rows, chunk_entries  # free chunk memory
        except Exception as e:
            print(f" -> failed: {e}")

    return all_entries, total_rows


def _enrich_entries_rvol_atr(client, all_entries: dict, full_sql_cfg: dict) -> dict:
    """Fetch RVOL + ATR in a separate lightweight query, merge into entries.

    Also applies min_rvol filter if configured.

    Args:
        client: CetaResearch API client
        all_entries: dict keyed by trade_date -> list of entry dicts
        full_sql_cfg: SQL config with start_date, end_date, exchange_filter, min_rvol

    Returns:
        Filtered entries dict with rvol and atr_14 populated.
    """
    # Collect unique symbols from entries
    symbols = set()
    for entries in all_entries.values():
        for e in entries:
            symbols.add(e["symbol"])

    if not symbols:
        return all_entries

    print(f"    RVOL+ATR query: {len(symbols)} symbols")

    sql = build_rvol_atr_sql(
        symbols=sorted(symbols),
        start_date=full_sql_cfg["start_date"],
        end_date=full_sql_cfg["end_date"],
        symbol_filter=full_sql_cfg.get("symbol_filter", "symbol NOT LIKE '%.%'"),
        exchange_filter=full_sql_cfg.get("exchange_filter", "m.exchange = 'NASDAQ'"),
    )

    try:
        rows = client.query(sql, memory_mb=16384, threads=6, disk_mb=40960, timeout=600)
        print(f"    RVOL+ATR: {len(rows)} rows returned")
    except Exception as e:
        print(f"    RVOL+ATR query failed: {e} (continuing without RVOL/ATR)")
        return all_entries

    # Build lookup: (symbol, trade_date) -> {rvol, atr_14}
    lookup = {}
    for row in rows:
        key = (row["symbol"], str(row["trade_date"]))
        lookup[key] = {"rvol": row.get("rvol"), "atr_14": row.get("atr_14")}
    del rows

    # Merge into entries and apply min_rvol filter
    min_rvol = full_sql_cfg.get("min_rvol", 0)
    filtered = {}
    removed = 0
    for d, entries in all_entries.items():
        day_entries = []
        for e in entries:
            key = (e["symbol"], d)
            enrichment = lookup.get(key, {})
            e["rvol"] = enrichment.get("rvol")
            e["atr_14"] = enrichment.get("atr_14")

            # Apply min_rvol filter
            rvol = e["rvol"]
            if min_rvol > 0 and rvol is not None and rvol < min_rvol:
                removed += 1
                continue
            day_entries.append(e)
        if day_entries:
            filtered[d] = day_entries

    if min_rvol > 0 and removed > 0:
        print(f"    RVOL filter (min_rvol={min_rvol}): removed {removed} entries")

    return filtered


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

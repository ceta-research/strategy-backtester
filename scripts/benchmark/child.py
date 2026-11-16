"""Run one pipeline config the way the hosted backtest wrapper does, with stage timers and a query log.

Run by scripts/benchmark_catalog.py: `python scripts/benchmark/child.py --help` lists the arguments.
The wrapper's steps: write the config to OUT_DIR/pipeline_config.yaml, set argv to
`run.py --config ... --output OUT_DIR/result.json`, call run.main(). OUT_DIR/stages.json gets the stage
spans, every data query (rows, server time, resources sent) and the environment, rewritten after each
span so a killed run shows where it stopped.
"""

import argparse
import contextlib
import functools
import inspect
import io
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import polars as pl  # noqa: E402

from engine import data_provider, pipeline, simulator  # noqa: E402
from lib.backtest_result import SweepResult  # noqa: E402
from lib.cr_client import CetaResearch  # noqa: E402
from scripts.benchmark.report import KNOBS  # noqa: E402

PROVIDERS = [data_provider.CRDataProvider, data_provider.BhavcopyDataProvider,
             data_provider.NseChartingDataProvider, data_provider.NseChartingRepairedProvider]


class Recorder:
    """Spans with exclusive time: a span's own seconds leave out the spans inside it.
    A span inside one of the same name is part of it, so a stage is counted once."""

    def __init__(self, clock=time.time, on_event=None):
        self.clock, self.events, self.queries, self.on_event = clock, [], [], on_event
        self._stack, self.last_body, self.last_task, self.empty_orders = [], None, None, 0

    @contextlib.contextmanager
    def span(self, stage):
        if any(frame["stage"] == stage for frame in self._stack):
            yield
            return
        frame = {"stage": stage, "t0": self.clock(), "inner": 0.0}
        self._stack.append(frame)
        try:
            yield
        finally:
            self._stack.pop()
            t1 = self.clock()
            spent = t1 - frame["t0"]
            if self._stack:
                self._stack[-1]["inner"] += spent
            self.events.append({"stage": stage, "t0": frame["t0"], "t1": t1, "exclusive": spent - frame["inner"]})
            if self.on_event:
                self.on_event()


def row_count(data):
    if data is None:
        return 0
    if isinstance(data, (bytes, bytearray)):  # polars reads the footer only; the engine loads it anyway
        return pl.scan_parquet(io.BytesIO(data)).select(pl.len()).collect().item()
    if isinstance(data, str):
        return max(len(data.splitlines()) - 1, 0)
    return len(data)


def is_cut(rows, limit, row_cap):
    return (row_cap is not None and rows >= row_cap) or (limit is not None and rows >= limit)


def _seconds_between(start, end):
    try:
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        return round((parse(end) - parse(start)).total_seconds(), 3)
    except (AttributeError, TypeError, ValueError):
        return None


def query_record(sql, task, rows, limit, seconds, body, row_cap, error=None):
    task = task or {}
    table = re.search(r"\bFROM\s+([\w.]+)", sql, re.I)
    return {"table": table.group(1) if table else "", "ok": error is None, "error": error, "rows": rows,
            "row_count": task.get("rowCount"), "limit": limit, "cut": is_cut(rows, limit, row_cap),
            "seconds": round(seconds, 3), "server_seconds": task.get("executionTime"),
            "queue_seconds": _seconds_between(task.get("submittedAt"), task.get("startedAt")),
            "size_bytes": task.get("sizeBytes"), "status": task.get("status"),
            "resources": (body or {}).get("resources")}


def _wrap(owner, name, wrapper_for, undo):
    had_own = name in vars(owner)
    original = getattr(owner, name)
    setattr(owner, name, functools.wraps(original)(wrapper_for(original)))
    undo.append(lambda: setattr(owner, name, original) if had_own else delattr(owner, name))


def _timed(rec, stage, on_call=None):
    def wrapper_for(fn):
        def timed(*args, **kwargs):
            if on_call:
                on_call(*args, **kwargs)
            with rec.span(stage):
                return fn(*args, **kwargs)
        return timed
    return wrapper_for


def install(rec, provider_classes=None, query_tier_limits=None, row_cap=None):
    """Patch timers into the pipeline; returns a function that removes them all.
    query_tier_limits makes every query ask for that tier's caps, and its defaults where the engine
    leaves a knob out, instead of the key's own."""
    undo = []
    for cls in PROVIDERS if provider_classes is None else provider_classes:
        _wrap(cls, "fetch_ohlcv", _timed(rec, "fetch_ohlcv"), undo)
    _wrap(pipeline, "create_epoch_wise_instrument_stats", _timed(rec, "stats"), undo)
    _wrap(pipeline, "sort_orders", _timed(rec, "rank"), undo)

    def count_empty(context, orders, *args, **kwargs):
        rec.empty_orders += int(orders.is_empty())
    _wrap(simulator, "process", _timed(rec, "simulate", on_call=count_empty), undo)
    _wrap(SweepResult, "save", _timed(rec, "save"), undo)
    wrapped_generators = set()

    def generator_for(get):
        def get_timed(*args, **kwargs):
            gen = get(*args, **kwargs)
            if type(gen) not in wrapped_generators:
                wrapped_generators.add(type(gen))
                _wrap(type(gen), "generate_orders", _timed(rec, "generate_orders"), undo)
            return gen
        return get_timed
    _wrap(pipeline, "get_signal_generator", generator_for, undo)

    if query_tier_limits is not None:
        _wrap(CetaResearch, "_tier_limits", lambda fn: lambda self: query_tier_limits, undo)

    def capture(post):
        def post_logged(url, json=None, **kw):
            rec.last_body = json
            return post(url, json=json, **kw)
        return post_logged

    def submit_for(submit):
        signature = inspect.signature(submit)

        def submit_logged(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            if query_tier_limits is not None:
                for _, kwarg, default in KNOBS:
                    if bound.arguments[kwarg] is None:
                        bound.arguments[kwarg] = query_tier_limits[default]
            local = []
            _wrap(self.session, "post", capture, local)
            try:
                return submit(*bound.args, **bound.kwargs)
            finally:
                local.pop()()
        return submit_logged
    _wrap(CetaResearch, "_submit", submit_for, undo)

    def poll_for(poll):
        def poll_logged(self, *args, **kwargs):
            rec.last_task = poll(self, *args, **kwargs)
            return rec.last_task
        return poll_logged
    _wrap(CetaResearch, "_poll", poll_for, undo)

    def query_for(query):
        signature = inspect.signature(query)

        def query_logged(self, *args, **kwargs):
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            rec.last_body = rec.last_task = None
            start, error, data = rec.clock(), None, None
            with rec.span("query"):
                try:
                    data = query(self, *args, **kwargs)
                    return data
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"[:300]
                    raise
                finally:
                    rec.queries.append(query_record(
                        bound.arguments["sql"], rec.last_task, row_count(data), bound.arguments["limit"],
                        rec.clock() - start, rec.last_body, row_cap, error))
        return query_logged
    _wrap(CetaResearch, "query", query_for, undo)

    def remove():
        while undo:
            undo.pop()()
    return remove


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:  # no /proc off Linux
        return ""


def environment():
    from importlib.metadata import version
    packages = {p: version(p) for p in ("polars", "pandas", "numpy", "pyarrow", "scikit-learn")}
    cpu = next((line.split(":", 1)[1].strip() for line in _read("/proc/cpuinfo").splitlines()
                if line.startswith("model name")), "")
    return {"python": sys.version.split()[0], "packages": packages, "cpu_model": cpu,
            "polars_threads": pl.thread_pool_size(), "loadavg": _read("/proc/loadavg").split()[:3],
            "cpus_seen": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()}


def write_json(path, data):
    with open(path + ".tmp", "w") as f:
        f.write(json.dumps(data))
    os.replace(path + ".tmp", path)


def run_hosted(config, out_dir):
    """The backend wrapper's steps for one config."""
    config_path = os.path.join(out_dir, "pipeline_config.yaml")
    with open(config_path, "w") as f:
        json.dump(config, f)
    sys.argv = ["run.py", "--config", config_path, "--output", os.path.join(out_dir, "result.json")]
    from run import main
    main()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one config through the hosted wrapper's steps, timed.")
    ap.add_argument("config", help="the pipeline config, JSON")
    ap.add_argument("out_dir", help="where result.json and stages.json go")
    ap.add_argument("--limits", required=True,
                    help="JSON file: the key's userTier and every tier's caps (allTierLimits), as /resource-limits gives")
    ap.add_argument("--query-tier", help="ask every data query for this tier's caps and defaults")
    ap.add_argument("--row-cap", type=int, help="a query returning this many rows is recorded as cut")
    args = ap.parse_args(argv)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(args.config) as f:
        config = json.load(f)
    with open(args.limits) as f:
        limits = json.load(f)
    tier_limits = limits.get("allTierLimits") or {}
    out = {"error": None, "user_tier": limits.get("userTier"), "tier_limits": tier_limits,
           "query_tier": args.query_tier or limits.get("userTier"), **environment()}
    stages_path, start = os.path.join(args.out_dir, "stages.json"), time.time()

    def flush():
        write_json(stages_path, {**out, "start": start, "end": time.time(), "events": rec.events,
                                 "queries": rec.queries, "empty_orders": rec.empty_orders})
    rec = Recorder(on_event=flush)
    try:
        tier_caps = tier_limits[args.query_tier] if args.query_tier else None
        remove = install(rec, query_tier_limits=tier_caps, row_cap=args.row_cap)
        try:
            run_hosted(config, args.out_dir)
        finally:
            remove()
    except BaseException as e:
        out["error"] = "".join(traceback.format_exception_only(type(e), e)).strip()[:500]
        raise
    finally:
        flush()


if __name__ == "__main__":
    main()

"""Offline tests for the catalog benchmark harness (scripts/benchmark_catalog.py, scripts/benchmark/).

Tier caps, rates and the container shape here are made-up round numbers, not the platform's.
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import pipeline  # noqa: E402
from engine.signals.base import get_signal_generator  # noqa: E402
from lib.cr_client import CetaResearch  # noqa: E402
from scripts import benchmark_catalog as cli  # noqa: E402
from scripts.benchmark import cases, child, measure, report, stages  # noqa: E402
from tests.test_pipeline import MINIMAL_CONFIG, make_deterministic_data  # noqa: E402

LINUX = sys.platform.startswith("linux")
TIERS = {
    "small": {"maxMemory": 1000, "maxThreads": 2, "maxDisk": 5000, "maxExecutionTimeout": 100,
              "defaultMemory": 1000, "defaultThreads": 2, "defaultDisk": 2000, "defaultExecutionTimeout": 100},
    "big": {"maxMemory": 16000, "maxThreads": 6, "maxDisk": 40000, "maxExecutionTimeout": 600,
            "defaultMemory": 6000, "defaultThreads": 3, "defaultDisk": 8192, "defaultExecutionTimeout": 180},
}
TWO_EXITS = MINIMAL_CONFIG.replace("trailing_stop_pct: [10]", "trailing_stop_pct: [5, 10]")


# --- suite -----------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(cases.load_suite()))
def test_suite_case_loads_and_counts(name):
    case = cases.load_suite()[name]
    base = cases.load_base(case)
    get_signal_generator(base["static"]["strategy_type"])  # raises when unregistered
    counts = {size: cases.count_configs(cases.build_config(case, size)) for size in cases.SIZES}
    assert counts["1"] == 1
    assert 8 <= counts["10"] <= 12
    assert 40 <= counts["50"] <= 60


@pytest.mark.parametrize("name", sorted(cases.load_suite()))
def test_sweep_axes_name_real_params_and_keep_the_default(name):
    case = cases.load_suite()[name]
    base = cases.load_base(case)
    for size in ("10", "50"):
        assert {d.split(".")[0] for d in case["sweeps"][size]} == {"entry", "exit"}
        for dotted, values in case["sweeps"][size].items():
            section, param = dotted.split(".")
            assert param in base[section], f"{name}: {dotted} is not in the base config"
            default = base[section][param][0]
            assert default in values, f"{name}: {dotted} leaves out the default {default}"
            assert all(isinstance(v, (int, float)) and v > 0 for v in values) and len(set(values)) == len(values)


# --- query recorder ----------------------------------------------------------

def _parquet(n):
    buf = io.BytesIO()
    pq.write_table(pa.table({"x": list(range(n))}), buf)
    return buf.getvalue()


def test_row_count_reads_each_format():
    assert child.row_count(_parquet(7)) == 7
    assert child.row_count([{"x": 1}, {"x": 2}]) == 2
    assert child.row_count("x\n1\n2\n3\n") == 3
    assert child.row_count(None) == 0


def test_cut_flag():
    assert child.is_cut(rows=100, limit=5_000_000, row_cap=100)
    assert child.is_cut(rows=10, limit=10, row_cap=None)
    assert not child.is_cut(rows=9, limit=10, row_cap=None)


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.posted, self.headers = [], {}

    def post(self, url, json=None, **kw):
        self.posted.append(json)
        return FakeResponse({"taskId": "t-1"})


def _fake_client(limits):
    c = CetaResearch.__new__(CetaResearch)
    c.base_url, c.session, c._limits_cache = "http://fake", FakeSession(), limits
    return c


def test_submit_capture_records_sent_resources():
    rec = child.Recorder()
    undo = child.install(rec, provider_classes=[])
    try:
        c = _fake_client(TIERS["big"])
        c._submit("SELECT 1", timeout=600, limit=10, format="parquet", memory_mb=16000, threads=6)
    finally:
        undo()
    assert rec.last_body["resources"] == {"executionTimeoutSeconds": 600, "memoryMb": 16000, "threads": 6}
    assert c.session.posted[0] is rec.last_body


def test_query_tier_clamps_and_fills_omitted_knobs_in_the_body_sent():
    rec = child.Recorder()
    undo = child.install(rec, provider_classes=[], query_tier_limits=TIERS["small"])
    try:
        c = _fake_client(TIERS["big"])  # the key's own tier would allow more
        c._submit("SELECT 1", timeout=600, limit=10, format="parquet", memory_mb=16000, threads=6)
    finally:
        undo()
    assert c.session.posted[0]["resources"] == {
        "executionTimeoutSeconds": 100, "memoryMb": 1000, "threads": 2, "diskMb": 2000}
    assert "post" not in vars(c.session)  # the capture came off the session


def test_query_record_from_task():
    task = {"status": "completed", "rowCount": 5, "executionTime": 3.2, "sizeBytes": 99,
            "submittedAt": "2026-11-16T12:00:00Z", "startedAt": "2026-11-16T12:00:04.5Z"}
    q = child.query_record("SELECT a FROM nse.nse_charting_day WHERE x = 1", task, rows=5, limit=10,
                           seconds=9.0, body={"resources": {"threads": 6}}, row_cap=None)
    assert q["table"] == "nse.nse_charting_day" and q["ok"]
    assert q["queue_seconds"] == 4.5
    assert q["server_seconds"] == 3.2 and q["rows"] == 5 and not q["cut"]
    assert q["resources"] == {"threads": 6}


def test_failed_query_is_recorded_and_still_raises():
    rec = child.Recorder()
    undo = child.install(rec, provider_classes=[])
    try:
        c = _fake_client(TIERS["big"])
        c._poll = lambda *a, **k: {"status": "failed", "error": "memory limit"}
        with pytest.raises(Exception, match="memory limit"):
            c.query("SELECT * FROM fmp.key_metrics", timeout=60, limit=10, format="parquet")
    finally:
        undo()
    assert rec.queries[0]["ok"] is False and "memory limit" in rec.queries[0]["error"]
    assert rec.queries[0]["table"] == "fmp.key_metrics"


# --- stage accounting --------------------------------------------------------

class TickClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_stage_net_of_queries_and_outermost_only():
    clock = TickClock()
    rec = child.Recorder(clock=clock)

    def query():
        with rec.span("query"):
            clock.t += 3

    def inner_fetch():
        with rec.span("fetch_ohlcv"):
            clock.t += 1

    with rec.span("generate_orders"):
        clock.t += 2
        query()
        with rec.span("fetch_ohlcv"):  # nested call of the same stage counts once
            clock.t += 1
            inner_fetch()
    s = rec.summary(wall=10)
    assert s["query_seconds"] == 3
    assert s["stage_counts"]["fetch_ohlcv"] == 1  # the nested call is part of the outer one
    assert s["fetch_seconds"] == 3 + 2  # all query time plus the provider's own 2 s
    assert s["signal_seconds"] == 7 - 3 - 2  # generate_orders' own time only
    assert s["other_seconds"] == pytest.approx(10 - 5 - 2)


def test_simulation_window_covers_result_building_between_spans():
    ev = lambda stage, t0, t1: {"stage": stage, "t0": t0, "t1": t1, "exclusive": t1 - t0}  # noqa: E731
    events = [ev("generate_orders", 0, 4), ev("stats", 5, 6), ev("rank", 6, 7), ev("simulate", 7, 8),
              ev("rank", 9, 10), ev("simulate", 10, 11), ev("save", 12, 13)]
    s = stages.summarize(events, wall=15)
    assert s["simulation_seconds"] == 12 - 5  # gaps after each simulate are result building
    assert s["other_seconds"] == 15 - 4 - 7 - 1
    assert stages.stage_peaks([(8.5, 900), (14, 10)], events) == {"simulation": 900, "other": 10}
    assert stages.summarize([ev("generate_orders", 0, 1)], 2)["simulation_seconds"] == 0


def test_stage_peaks_take_the_innermost_span():
    events = [{"stage": "generate_orders", "t0": 0, "t1": 10}, {"stage": "query", "t0": 2, "t1": 4}]
    peaks = stages.stage_peaks([(1, 100), (3, 500), (5, 300), (11, 50)], events)
    assert peaks == {"signal": 300, "fetch": 500, "other": 50}


class FakeProvider:
    def fetch_ohlcv(self, **kw):
        return make_deterministic_data()


def _run_fake(tmp_path, name, hooks):
    cfg = tmp_path / f"{name}.yaml"
    cfg.write_text(TWO_EXITS)
    rec = child.Recorder()
    undo = child.install(rec, provider_classes=[FakeProvider]) if hooks else (lambda: None)
    try:
        sweep = pipeline.run_pipeline(str(cfg), data_provider=FakeProvider())
        sweep.save(str(tmp_path / f"{name}.json"))
    finally:
        undo()
    return rec, sweep


def test_hooks_fire_on_real_pipeline_and_come_off(tmp_path):
    originals = (pipeline.sort_orders, pipeline.get_signal_generator, CetaResearch.query, FakeProvider.fetch_ohlcv)
    gen_cls = type(get_signal_generator("eod_technical"))
    own_generate = vars(gen_cls)["generate_orders"]
    rec, sweep = _run_fake(tmp_path, "hooked", hooks=True)
    counts = rec.summary(wall=1)["stage_counts"]
    assert len(sweep.configs) == 2
    assert all(br.to_dict()["summary"].get("total_trades", 0) > 0 for _, br in sweep.configs)  # orders > 0
    assert counts["fetch_ohlcv"] == 1 and counts["generate_orders"] == 1 and counts["stats"] == 1
    assert counts["simulate"] == 2 and counts["rank"] == 2 and counts["save"] == 1
    assert (pipeline.sort_orders, pipeline.get_signal_generator, CetaResearch.query,
            FakeProvider.fetch_ohlcv) == originals
    assert vars(gen_cls)["generate_orders"] is own_generate


def test_hooks_leave_the_result_byte_identical(tmp_path):
    _run_fake(tmp_path, "plain", hooks=False)
    _run_fake(tmp_path, "hooked", hooks=True)
    assert (tmp_path / "plain.json").read_bytes() == (tmp_path / "hooked.json").read_bytes()


def test_child_main_runs_the_hosted_steps_end_to_end(tmp_path, monkeypatch):
    """run.main -> NseChartingDataProvider -> CetaResearch.query, with only the HTTP calls stubbed."""
    df = make_deterministic_data().select(["symbol", "date_epoch", "open", "high", "low", "close",
                                           "average_price", "volume"])
    buf = io.BytesIO()
    df.write_parquet(buf)
    task = {"status": "completed", "rowCount": df.height, "executionTime": 0.5, "artifactId": "a"}
    monkeypatch.setenv("CR_API_KEY", "test-only")
    monkeypatch.setattr(requests.Session, "post", lambda self, url, json=None, **k: FakeResponse({"taskId": "t"}))
    monkeypatch.setattr(CetaResearch, "_tier_limits", lambda self: TIERS["big"])
    monkeypatch.setattr(CetaResearch, "_poll", lambda self, *a, **k: task)
    monkeypatch.setattr(CetaResearch, "_download", lambda self, t, format="json": buf.getvalue())
    monkeypatch.setattr(child, "resource_limits", lambda: ("big", TIERS, "fake.host"))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    import yaml
    config = yaml.safe_load(TWO_EXITS)
    config["static"].update(data_provider="nse_charting", strategy_type="eod_technical")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config))
    out = tmp_path / "run"
    child.main([str(cfg), str(out)])
    got = json.loads((out / "stages.json").read_text())
    assert (out / "result.json").exists() and got["error"] is None
    assert got["user_tier"] == "big" and got["query_tier"] == "big" and got["api_host"] == "fake.host"
    q = got["queries"][0]
    assert q["table"] == "nse.nse_charting_day" and q["rows"] == df.height and q["ok"]
    assert q["resources"] == {"executionTimeoutSeconds": 600, "memoryMb": 16000, "threads": 6}
    counts = stages.summarize(got["events"], wall=10)["stage_counts"]
    assert counts == {"query": 1, "fetch_ohlcv": 1, "generate_orders": 1, "stats": 1, "rank": 2, "simulate": 2,
                      "save": 1}
    assert not hasattr(CetaResearch.query, "__wrapped__")  # the child took its hooks off


def test_environment_without_proc(monkeypatch):
    monkeypatch.setattr(child, "_read", lambda path: "")
    env = child.environment()
    assert env["cpu_model"] == "" and env["loadavg"] == [] and env["packages"]["polars"] == env["polars"]


# --- run validity --------------------------------------------------------------

def test_problems_mark_a_run_that_cannot_stand_as_a_pass():
    ok = {"exit": 0, "timed_out": False, "configs": 2, "stage_counts": {"simulate": 2},
          "queries": [{"table": "t", "ok": True}]}
    assert cli.problems(ok, result_exists=True) == []
    failed_query = dict(ok, queries=[{"table": "fmp.x", "ok": False, "error": "timeout"}])
    assert cli.problems(failed_query, True) == ["query failed: fmp.x (timeout)"]
    assert cli.problems(ok, result_exists=False) == ["no result.json"]
    assert cli.problems(dict(ok, stage_counts={"simulate": 1}), True) == ["simulated 1 of 2 configs"]
    assert cli.problems(dict(ok, exit=-9, timed_out=True), True) == ["exit -9, timed out"]


def test_hosted_env_is_built_not_inherited(monkeypatch):
    monkeypatch.setenv("POLARS_MAX_THREADS", "2")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    monkeypatch.setenv("CR_API_KEY", "k")
    env = cli.hosted_env("/tmp/run", container_cpus=3)
    assert "POLARS_MAX_THREADS" not in env and "PYTHONPATH" not in env
    assert env["CR_API_KEY"] == "k" and env["OUTPUT_DIR"] == env["HOME"] == "/tmp/run"
    assert env["OMP_NUM_THREADS"] == env["OPENBLAS_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "3"
    assert env["PYTHONUNBUFFERED"] == "1"


# --- measure -----------------------------------------------------------------

ALLOC = "b = bytearray({mib} * 1024 * 1024); b[::4096] = b'x' * len(b[::4096])"


@pytest.mark.skipif(not LINUX, reason="reads /proc and uses taskset")
def test_measure_peak_exit_and_cpus(tmp_path):
    code = f"import os, time; {ALLOC.format(mib=300)}; print(len(os.sched_getaffinity(0))); time.sleep(1); raise SystemExit(3)"
    out = tmp_path / "out.txt"
    with open(out, "w") as f:
        m = measure.measure([sys.executable, "-c", code], cpus="0", stdout=f, timeout=60)
    assert m["exit"] == 3 and not m["timed_out"]
    assert m["peak_pss_gib"] >= 0.29 and m["maxrss_gib"] >= 0.29
    assert out.read_text().strip() == "1"
    assert m["samples"] and m["samples"][0][0] >= m["start"] - 1


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_measure_counts_forked_workers():
    code = (f"import os, time\nif os.fork() == 0:\n    {ALLOC.format(mib=200)}\n    time.sleep(1.5)\n    os._exit(0)\n"
            f"{ALLOC.format(mib=200)}\ntime.sleep(1.5)\nos.wait()")
    m = measure.measure([sys.executable, "-c", code], timeout=60)
    assert m["peak_pss_gib"] >= 0.38  # both processes, where ru_maxrss sees one
    assert m["maxrss_gib"] < 0.3


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_measure_timeout_kills_the_whole_group(tmp_path):
    pid_file = tmp_path / "grandchild"
    code = (f"import os, time\npid = os.fork()\nif pid == 0:\n    time.sleep(60)\n    os._exit(0)\n"
            f"open({str(pid_file)!r}, 'w').write(str(pid))\ntime.sleep(60)")
    m = measure.measure([sys.executable, "-c", code], timeout=1)
    assert m["timed_out"] and m["wall_seconds"] < 10
    grandchild = int(pid_file.read_text())
    time.sleep(0.2)
    assert not os.path.exists(f"/proc/{grandchild}") or "Z" in open(f"/proc/{grandchild}/stat").read().split()[2]


# --- window guard ------------------------------------------------------------

@pytest.mark.parametrize("hh,mm,allowed", [(12, 0, True), (4, 0, True), (3, 59, False), (1, 0, False),
                                           (22, 29, True), (22, 30, False), (22, 59, False), (23, 0, False)])
def test_window_guard(hh, mm, allowed):
    assert cases.in_window(datetime(2026, 11, 16, hh, mm, tzinfo=timezone.utc), (4, 23)) is allowed


# --- report ------------------------------------------------------------------

PLATFORM = {
    "container": {"cpu": 2, "ram_gb": 4, "disk_gb": 1, "timeout_s": 500, "max_timeout_s": 1000},
    "container_rates": {"cpu": 0.01, "ram_gb": 0.001, "disk_gb": 0.0001},
    "query_rates": {"thread": 0.01, "ram_gb": 0.001, "disk_gb": 0.0001, "cpu": 0.01, "minimum": 0.5},
    "paid_tiers": ["small", "big"],
    "refused_tiers": ["none"],
    "container_ram_caps_gb": {"small": 6, "big": 12},
    "possible_container_timeouts": {"small": 30, "big": 60},
}


def _record(case="low_pe", size="1", tier="big", wall=40.0, peak=3.5, fetch=20.0, ok=True, server=10.0):
    return {"case": case, "size": size, "configs": int(size), "query_tier": tier, "exit": 0 if ok else 1,
            "tag": "main" if tier == "big" else "tier", "valid": ok, "problems": [] if ok else ["query failed: t"],
            "wall_seconds": wall, "peak_pss_gib": peak, "fetch_seconds": fetch, "signal_seconds": 5.0,
            "simulation_seconds": 10.0, "result_bytes": 1000, "start_utc": "2026-11-16T12:00:00Z",
            "tier_limits": TIERS, "empty_configs": 0, "cpus": "0-3",
            "queries": [{"table": "nse.nse_charting_day", "rows": 6_000_000, "cut": False, "ok": ok,
                         "server_seconds": server, "resources": {"memoryMb": 16000, "threads": 6}}]}


def test_container_credits_by_hand():
    # 100 s x (2 cpu x 0.01 + 4 GB x 0.001 + 1 GB x 0.0001) = 100 x 0.0241
    assert report.container_credits(100, PLATFORM) == pytest.approx(2.41)


def test_query_credits_by_hand():
    # ceil(9.2) = 10 s x (6 threads x 0.01 + 15.625 GB x 0.001 + 8 GB default disk x 0.0001) = 10 x 0.076425
    low, high = report.query_credits({"server_seconds": 9.2, "resources": {"memoryMb": 16000, "threads": 6}},
                                     TIERS["big"], PLATFORM)
    assert low == pytest.approx(0.76425)
    assert high == pytest.approx(0.76425 + 6 * 10 * 0.01)  # every thread busy for the whole 10 s
    # the minimum wraps the whole sum: 1 s x (2 x 0.01 + ~0.98 GB x 0.001 + ~1.95 GB x 0.0001) < 0.5
    assert report.query_credits({"server_seconds": 0.2, "resources": {}}, TIERS["small"], PLATFORM) == (0.5, 0.5)


def test_breaches():
    found = report.breaches([_record(peak=4.3), _record(case="near", peak=3.8),
                             _record(case="eod_breakout", tier="small", ok=False), _record(case="slow", wall=700),
                             dict(_record(case="empty", size="10"), empty_configs=3)], PLATFORM)
    text = "\n".join(found)
    assert "low_pe 1" in text and "above the 4 GiB request" in text
    assert "near 1" in text and "within 0.3 GiB" in text
    assert "eod_breakout 1" in text and "small query caps" in text and "not a pass: query failed" in text
    assert "slow 1" in text and "above the 500 s default timeout" in text
    assert "700 s above small 30 s; big 60 s (if those caps are live)" in text
    assert "3 of 10 configs had no orders" in text
    assert "none: every strategy: refused at submit" in text
    assert "container RAM cap" not in text  # 4.3 GiB fits both tiers' caps


def test_breaches_against_tier_ram_caps_use_the_larger_peak():
    found = report.breaches([dict(_record(case="huge", peak=6.5), maxrss_gib=13.0)], PLATFORM)
    assert any("peak 13.0 GiB above the container RAM cap of small 6 GB; big 12 GB: no request fits" in f
               for f in found)
    assert report.peak({"peak_pss_gib": 2.0, "maxrss_gib": None}) == 2.0


def test_derived_wall_for_other_tier():
    assert report.derived_wall(_record(size="50", wall=300, fetch=20), _record(tier="small", wall=60, fetch=45)) == 325


def test_credits_at_other_tier_use_that_tiers_run():
    by_tier = report.tier_records([_record(tier="small", wall=60, fetch=45, server=30.0)])
    main50 = _record(size="50", wall=300, fetch=20)
    low, high, derived = report.credits_at(main50, "small", by_tier, PLATFORM)
    q_low, _ = report.query_credits({"server_seconds": 30.0, "resources": {"memoryMb": 16000, "threads": 6}},
                                    TIERS["small"], PLATFORM)
    assert derived and low == pytest.approx(report.container_credits(325, PLATFORM) + q_low)
    assert report.credits_at(main50, "small", {}, PLATFORM) is None  # no run at that tier's caps
    assert report.credits_at(main50, "big", {}, PLATFORM)[2] is False
    assert report.tier_records([_record(tier="small", ok=False)]) == {}  # a failed tier run prices nothing


def test_markdown_has_a_row_per_size_and_marks_invalid_runs():
    recs = [_record(size=s) for s in ("1", "10", "50")] + [_record(tier="small")]
    recs[1] = dict(recs[1], valid=False, problems=["no result.json"])
    md = report.markdown(recs, PLATFORM)
    table = md.split("### low_pe")[1].split("###")[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Configs")]
    assert [row.split(" | ")[0] for row in rows] == ["| 1", "| 10", "| 50"]
    assert "NOT A PASS" in rows[1] and rows[1].endswith("n/a | n/a |")
    assert md.startswith("Runs: ") and "Cap breaches" in md


def test_run_key_matches_what_cmd_run_skips():
    args = SimpleNamespace(tag="main", query_tier=None, image="plain process")
    rec = {"case": "low_pe", "size": "1", "tag": args.tag, "cpus": "0-3", "query_tier_asked": args.query_tier}
    assert cli.run_key(rec) == ("low_pe", "1", "main", "0-3", None)

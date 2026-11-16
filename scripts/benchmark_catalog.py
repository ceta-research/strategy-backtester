"""Benchmark the catalog strategies: rows fetched, stage times, peak memory and result size per run.

Each run goes through the hosted backtest wrapper's steps (scripts/benchmark/child.py) under a sampler
(scripts/benchmark/measure.py), one run at a time, and appends one JSON line to --out. `report` turns
those lines into tables. Run from the repo root with CR_API_KEY (and CR_BASE_URL if not the default
host) in the environment; `run --help` and `report --help` list the options.

The platform file (fields in scripts/benchmark/report.py) holds the container a hosted run requests and
the platform's rates and caps; it stays outside the repo. The child gets the environment the hosted
executor gives its subprocess, not this shell's. A run already recorded as a pass for the same engine
commit, image, case, size, tag, CPUs and query tier is skipped, so re-running resumes.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.cr_client import CetaResearch  # noqa: E402
from scripts.benchmark import cases, measure, report, stages  # noqa: E402

CHILD = os.path.join(ROOT, "scripts", "benchmark", "child.py")
ENGINE_PATHS = ["engine", "lib", "run.py", "requirements.txt"]
HOOKED = ("query", "fetch_ohlcv", "generate_orders", "stats", "save")  # each fires in every run that simulates


def hosted_env(run_dir, container_cpus):
    """What the hosted executor passes its subprocess: a fixed set, with BLAS and OpenMP threads set to
    the container's CPU count, plus the CR_ and TS_ variables."""
    env = {"PATH": f"{os.path.dirname(sys.executable)}:/usr/local/bin:/usr/bin:/bin", "HOME": run_dir,
           "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg", "OUTPUT_DIR": run_dir,
           **{k: str(container_cpus) for k in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS")}}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("CR_", "TS_"))})
    return env


def problems(rec, result_exists):
    """Why a run's figures can't stand as a pass; empty when they can."""
    found = []
    if rec["exit"] != 0 or rec["timed_out"]:
        found.append(f"exit {rec['exit']}" + (", timed out" if rec["timed_out"] else ""))
    found += [f"query failed: {q['table']} ({q['error']})" for q in rec["queries"] if not q["ok"]]
    if not result_exists:
        found.append("no result.json")
    counts = rec["stage_counts"]
    if counts.get("simulate", 0) != rec["configs"]:
        found.append(f"simulated {counts.get('simulate', 0)} of {rec['configs']} configs")
    elif counts.get("simulate"):
        found += [f"no {stage} span: a hook didn't fire" for stage in HOOKED if not counts.get(stage)]
    return found


def run_key(engine_commit, image, case, size, tag, cpus, query_tier):
    return engine_commit, image, case, size, tag, cpus, query_tier


def record_key(rec):
    return run_key(rec["engine_commit"], rec["image"], rec["case"], rec["size"], rec["tag"], rec["cpus"],
                   rec["query_tier_asked"])


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def engine_commit():
    """The last commit that changed the engine, and whether those paths differ from it now."""
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
    return git("log", "-1", "--format=%h", "--", *ENGINE_PATHS), bool(git("status", "--porcelain", "--", *ENGINE_PATHS))


def resource_limits():
    """The key's tier and every tier's query caps, from /resource-limits, and the API host."""
    client = CetaResearch()
    body = client.session.get(f"{client.base_url}/resource-limits", timeout=15).json()
    return body, urlparse(client.base_url).netloc


def make_record(name, size, configs, args, m, child_out, run_dir, api_host):
    result = os.path.join(run_dir, "result.json")
    events = child_out.get("events", [])
    rec = {
        "case": name, "size": size, "configs": configs, "tag": args.tag, "query_tier_asked": args.query_tier,
        "cpus": args.cpus, "container": args.container, "start_utc": utc(m["start"]), "end_utc": utc(m["end"]),
        "exit": m["exit"], "timed_out": m["timed_out"], "wall_seconds": m["wall_seconds"],
        "peak_pss_gib": m["peak_pss_gib"], "maxrss_gib": m["maxrss_gib"],
        **stages.summarize(events, m["wall_seconds"]),
        "stage_peaks_kib": stages.stage_peaks(m["samples"], events),
        "queries": child_out.get("queries", []), "empty_configs": child_out.get("empty_orders", 0),
        "result_bytes": os.path.getsize(result) if os.path.exists(result) else 0,
        "engine_commit": args.engine_commit, "engine_dirty": args.engine_dirty, "image": args.image,
        "api_host": api_host,
        **{k: child_out.get(k) for k in ("python", "packages", "cpu_model", "loadavg", "polars_threads",
                                         "cpus_seen", "user_tier", "query_tier", "tier_limits", "error")},
        "run_dir": os.path.basename(run_dir),
    }
    rec["problems"] = problems(rec, os.path.exists(result))
    return rec


def child_cmd(config_path, run_dir, limits_path, args):
    cmd = [sys.executable, CHILD, config_path, run_dir, "--limits", limits_path]
    if args.query_tier:
        cmd += ["--query-tier", args.query_tier]
    if args.row_cap is not None:
        cmd += ["--row-cap", str(args.row_cap)]
    return cmd


def write_json(path, data, indent=None):
    with open(path, "w") as f:
        json.dump(data, f, indent=indent)


def run_one(name, case, size, args):
    config = cases.build_config(name, case, size)
    configs = cases.count_configs(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), f"{name}-{size}-{args.tag}-{stamp}")
    os.makedirs(run_dir)
    config_path, limits_path = os.path.join(run_dir, "config.json"), os.path.join(run_dir, "limits.json")
    write_json(config_path, config, indent=1)
    limits, api_host = resource_limits()  # read per run, outside the measured process
    write_json(limits_path, limits)
    with open(os.path.join(run_dir, "run.log"), "w") as log:
        m = measure.measure(child_cmd(config_path, run_dir, limits_path, args),
                            cpus=None if args.cpus == "all" else args.cpus,
                            env=hosted_env(run_dir, args.container["cpu"]),
                            timeout=args.timeout, stdout=log, cwd=run_dir)
    write_json(os.path.join(run_dir, "samples.json"), m["samples"])
    child_out = {}
    if os.path.exists(os.path.join(run_dir, "stages.json")):
        with open(os.path.join(run_dir, "stages.json")) as f:
            child_out = json.load(f)
    return make_record(name, size, configs, args, m, child_out, run_dir, api_host)


def recorded(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def cmd_run(args):
    suite = cases.load_suite()
    names = args.cases.split(",") if args.cases else list(suite)
    hours = tuple(int(h) for h in args.hours.split("-"))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    lock = open(args.out + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another run holds {args.out}.lock")
    args.engine_commit, args.engine_dirty = engine_commit()
    # a run on uncommitted engine changes never counts as done: its commit doesn't name the code it ran
    done = {record_key(r) for r in recorded(args.out) if report.passed(r) and not r["engine_dirty"]}
    for size in args.sizes.split(","):
        for name in names:
            if run_key(args.engine_commit, args.image, name, size, args.tag, args.cpus, args.query_tier) in done:
                print(f"skip {name} size {size}: already recorded", flush=True)
                continue
            if not cases.in_window(datetime.now(timezone.utc), hours):
                sys.exit(f"Outside {args.hours} UTC: stopping before {name} {size}")
            rec = run_one(name, suite[name], size, args)
            with open(args.out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"{rec['start_utc']} {name} size {size} ({rec['configs']} configs) exit {rec['exit']}: "
                  f"wall {rec['wall_seconds']} s, peak {rec['peak_pss_gib']} GiB, "
                  f"fetch {rec['fetch_seconds']:.1f} / signal {rec['signal_seconds']:.1f} / "
                  f"sim {rec['simulation_seconds']:.1f} s"
                  + (f" NOT A PASS: {'; '.join(rec['problems'])}" if rec["problems"] else ""), flush=True)


def default_tag(args):
    if args.query_tier:
        return "tier"
    return "unpinned" if args.cpus == "all" else "main"


def load_platform(path):
    with open(path) as f:
        return json.load(f)


def cmd_report(args):
    if not os.path.exists(args.runs):
        sys.exit(f"No such file: {args.runs}")
    print(report.markdown(recorded(args.runs), load_platform(args.platform)), end="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run the suite, one run at a time")
    r.add_argument("--platform", required=True, help="the platform file: container shape, rates and caps")
    r.add_argument("--cases", help="comma-separated case names from scripts/benchmark/suite.json (default: all)")
    r.add_argument("--sizes", default=",".join(cases.SIZES), help="comma-separated sizes (default: 1,10,50)")
    r.add_argument("--cpus", default="0-3", help="taskset CPU list, as a 4-CPU container gets; 'all' leaves it unpinned")
    r.add_argument("--query-tier", help="ask every data query for this tier's caps and defaults")
    r.add_argument("--row-cap", type=int, help="the key's row cap, when it has one: a query returning this "
                                               "many rows is recorded as cut")
    r.add_argument("--tag", help="a label for the runs (default: tier with --query-tier, unpinned with "
                                 "--cpus all, else main; the report's tables show main)")
    r.add_argument("--image", default="plain process", help="what the runs ran in, for the record")
    r.add_argument("--out", default=os.path.join(ROOT, "results", "benchmark", "runs.jsonl"),
                   help="the JSON-lines file runs append to; run folders go beside it")
    r.add_argument("--hours", default="4-23", help="UTC hours a run may start in, none in the last 30 minutes")
    r.add_argument("--timeout", type=int, default=3660, help="seconds before a run is killed")
    p = sub.add_parser("report", help="markdown tables from a runs file")
    p.add_argument("runs", help="the JSON-lines file `run` wrote")
    p.add_argument("--platform", required=True, help="the platform file: container shape, rates and caps")
    args = ap.parse_args()
    if args.command == "run":
        args.tag = args.tag or default_tag(args)
        args.container = load_platform(args.platform)["container"]
        cmd_run(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()

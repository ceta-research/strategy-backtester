"""Benchmark the catalog strategies: rows fetched, stage times, peak memory and result size per run.

Each run goes through the hosted backtest wrapper's steps (scripts/benchmark/child.py) under a sampler
(scripts/benchmark/measure.py), one run at a time, and appends one JSON line to --out.

Run from the repo root, with CR_API_KEY (and CR_BASE_URL if not the default host) in the environment:
  python scripts/benchmark_catalog.py run [--cases low_pe,eod_breakout] [--sizes 1,10,50] [--cpus 0-3|all]
      [--query-tier basic] [--row-cap N] [--tag main] [--image DIGEST] [--out results/benchmark/runs.jsonl]
      --platform FILE
  python scripts/benchmark_catalog.py report results/benchmark/runs.jsonl --platform FILE

The platform file (fields in scripts/benchmark/report.py) holds the container a hosted run requests and
the platform's rates and caps; it stays outside the repo.

`run` goes size by size (every case at 1, then 10, then 50), skips a run already recorded as valid in
--out, starts one only inside --hours (UTC, default 4-23, none in the last 30 minutes) and kills one
after --timeout seconds. --cpus pins the run to those CPUs, as a 4-CPU container would be; `all` leaves
it unpinned. --query-tier asks every data query for that tier's caps and defaults instead of the key's.
The child gets the environment the hosted executor gives its subprocess, not this shell's.
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.benchmark import cases, measure, report, stages  # noqa: E402

CHILD = os.path.join(ROOT, "scripts", "benchmark", "child.py")
ENGINE_PATHS = ["engine", "lib", "run.py", "requirements.txt"]


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
    found += [f"query failed: {q['table']} ({q.get('error')})" for q in rec["queries"] if not q.get("ok", True)]
    if not result_exists:
        found.append("no result.json")
    if rec["stage_counts"].get("simulate", 0) != rec["configs"]:
        found.append(f"simulated {rec['stage_counts'].get('simulate', 0)} of {rec['configs']} configs")
    return found


def run_key(rec):
    return rec["case"], rec["size"], rec["tag"], rec["cpus"], rec.get("query_tier_asked")


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def engine_commit():
    """The last commit that changed the engine, and whether those paths differ from it now."""
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()  # noqa: E731
    return git("log", "-1", "--format=%h", "--", *ENGINE_PATHS), bool(git("status", "--porcelain", "--", *ENGINE_PATHS))


def make_record(name, size, configs, args, m, child_out, run_dir):
    sha, dirty = engine_commit()
    result = os.path.join(run_dir, "result.json")
    events = child_out.get("events", [])
    rec = {
        "case": name, "size": size, "configs": configs, "tag": args.tag, "query_tier_asked": args.query_tier,
        "container": args.container,
        "start_utc": utc(m["start"]),
        "end_utc": utc(m["end"]), "exit": m["exit"], "timed_out": m["timed_out"], "wall_seconds": m["wall_seconds"],
        "peak_pss_gib": m["peak_pss_gib"], "maxrss_gib": m["maxrss_gib"], "cpus": m["cpus"],
        **stages.summarize(events, m["wall_seconds"]),
        "stage_peaks_kib": stages.stage_peaks(m["samples"], events),
        "queries": child_out.get("queries", []),
        "result_bytes": os.path.getsize(result) if os.path.exists(result) else 0,
        "engine_commit": sha, "engine_dirty": dirty, "image": args.image,
        **{k: child_out.get(k) for k in ("python", "polars", "packages", "cpu_model", "loadavg", "polars_threads",
                                         "cpus_seen", "user_tier", "query_tier", "tier_limits", "api_host", "error")},
        "run_dir": os.path.basename(run_dir),
    }
    counts = rec["stage_counts"]
    rec["empty_configs"] = counts.get("simulate", 0) - counts.get("rank", 0)  # the pipeline skips ranking those
    rec["problems"] = problems(rec, os.path.exists(result))
    rec["valid"] = not rec["problems"]
    return rec


def child_cmd(config_path, run_dir, args):
    cmd = [sys.executable, CHILD, config_path, run_dir]
    if args.query_tier:
        cmd += ["--query-tier", args.query_tier]
    if args.row_cap is not None:
        cmd += ["--row-cap", str(args.row_cap)]
    return cmd


def run_one(name, case, size, args):
    config = cases.build_config(case, size)
    configs = cases.count_configs(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = os.path.join(os.path.dirname(os.path.abspath(args.out)), f"{name}-{size}-{args.tag}-{stamp}")
    os.makedirs(run_dir)
    config_path = os.path.join(run_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=1)
    with open(os.path.join(run_dir, "run.log"), "w") as log:
        m = measure.measure(child_cmd(config_path, run_dir, args), cpus=None if args.cpus == "all" else args.cpus,
                            env=hosted_env(run_dir, args.container["cpu"]),
                            timeout=args.timeout, stdout=log, cwd=run_dir)
    with open(os.path.join(run_dir, "samples.json"), "w") as f:
        json.dump(m["samples"], f)
    child_out = {}
    if os.path.exists(os.path.join(run_dir, "stages.json")):
        with open(os.path.join(run_dir, "stages.json")) as f:
            child_out = json.load(f)
    return make_record(name, size, configs, args, m, child_out, run_dir)


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
    done = {run_key(r) for r in recorded(args.out) if r.get("valid")}
    for size in args.sizes.split(","):
        for name in names:
            if (name, size, args.tag, args.cpus, args.query_tier) in done:
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
                  + ("" if rec["valid"] else f" INVALID: {'; '.join(rec['problems'])}"), flush=True)


def load_platform(path):
    with open(path) as f:
        return json.load(f)


def cmd_report(args):
    with open(args.runs) as f:
        records = [json.loads(line) for line in f if line.strip()]
    print(report.markdown(records, load_platform(args.platform)), end="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cases")
    r.add_argument("--sizes", default=",".join(cases.SIZES))
    r.add_argument("--cpus", default="0-3")
    r.add_argument("--query-tier")
    r.add_argument("--row-cap", type=int, help="the key's row cap, when it has one: a query that returns this "
                                               "many rows is recorded as cut")
    r.add_argument("--tag")
    r.add_argument("--image", default="plain process")
    r.add_argument("--out", default=os.path.join(ROOT, "results", "benchmark", "runs.jsonl"))
    r.add_argument("--hours", default="4-23")
    r.add_argument("--timeout", type=int, default=3660)
    r.add_argument("--platform", required=True)
    p = sub.add_parser("report")
    p.add_argument("runs")
    p.add_argument("--platform", required=True)
    args = ap.parse_args()
    if args.command == "run":
        args.tag = args.tag or ("tier" if args.query_tier else "main")
        args.container = load_platform(args.platform)["container"]
        cmd_run(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()

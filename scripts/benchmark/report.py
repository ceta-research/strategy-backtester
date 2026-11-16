"""Turn benchmark run records into markdown tables, cap breaches and credit estimates.

The platform file (passed with --platform) holds what the engine repo doesn't: the container a hosted
run requests, compute and query rates, which tiers can run, and the container caps to check:
{"container": {"cpu", "ram_gb", "disk_gb", "timeout_s", "max_timeout_s"},
 "container_rates": {"cpu", "ram_gb", "disk_gb"},
 "query_rates": {"thread", "ram_gb", "disk_gb", "cpu", "minimum"},
 "paid_tiers": [...], "refused_tiers": [...], "container_ram_caps_gb": {tier: GB},
 "possible_container_timeouts": {tier: seconds}}
Query caps and defaults per tier come from each record (read live from /resource-limits).
"""

import math

AT_RISK_GIB = 0.3  # in a container the executor and its output buffer share the same memory limit
# A query's resource knobs: request body key, CetaResearch._submit argument, the tier default that fills it.
KNOBS = (("memoryMb", "memory_mb", "defaultMemory"), ("threads", "threads", "defaultThreads"),
         ("diskMb", "disk_mb", "defaultDisk"))
COLUMNS = ("Configs", "Rows fetched", "Cut", "Fetch s", "Signal s", "Simulation s", "Wall s", "Peak GiB",
           "Peak during", "result.json B", "Start UTC")


def passed(r):
    return not r["problems"]


def peak(r):
    """The larger of the tree's summed PSS and the kernel's ru_maxrss, in GiB."""
    return max(r["peak_pss_gib"], r.get("maxrss_gib") or 0)


def _by_value(caps, exceeded, unit):
    """'a, b 300 s; c 600 s' for the tiers whose cap the value exceeds, grouped by cap."""
    over = {}
    for tier, cap in caps.items():
        if exceeded(cap):
            over.setdefault(cap, []).append(tier)
    return "; ".join(f"{', '.join(tiers)} {cap} {unit}" for cap, tiers in sorted(over.items()))


def container_credits(wall, platform):
    c, r = platform["container"], platform["container_rates"]
    return wall * (c["cpu"] * r["cpu"] + c["ram_gb"] * r["ram_gb"] + c["disk_gb"] * r["disk_gb"])


def query_credits(q, limits, platform):
    """(low, high): latency-billed resources as sent, tier defaults for knobs left out; high adds the
    CPU-seconds term at its ceiling (every thread busy for the whole query), since no API reports it.
    Latency is the task's executionTime (started to completed), at or above the engine's own."""
    r, res = platform["query_rates"], q.get("resources") or {}
    sent = {key: res.get(key, limits[default]) for key, _, default in KNOBS}
    threads, mem_gb, disk_gb = sent["threads"], sent["memoryMb"] / 1024, sent["diskMb"] / 1024
    secs = math.ceil(q.get("server_seconds") or 0)
    latency = secs * (threads * r["thread"] + mem_gb * r["ram_gb"] + disk_gb * r["disk_gb"])
    cpu_ceiling = threads * secs * r["cpu"]
    return max(r["minimum"], latency), max(r["minimum"], latency + cpu_ceiling)


def derived_wall(record, tier_size1):
    """A run's wall time with its fetch swapped for another tier's size-1 fetch (same strategy)."""
    return record["wall_seconds"] - record["fetch_seconds"] + tier_size1["fetch_seconds"]


def tier_records(records):
    """{(case, tier): size-1 record} for passing runs made at a tier's query caps."""
    return {(r["case"], r["query_tier"]): r for r in records
            if r["size"] == "1" and r.get("query_tier_asked") and passed(r)}


def same_caps(a, b):
    keys = ("maxMemory", "maxThreads", "defaultMemory", "defaultThreads", "defaultDisk")
    return all(a.get(k) == b.get(k) for k in keys)


def credits_at(record, tier, by_tier, platform):
    """(low, high, derived) for one record priced at a tier, or None when no run covers that tier."""
    limits = record["tier_limits"]
    if tier not in limits:
        return None
    if tier == record["query_tier"] or same_caps(limits[tier], limits[record["query_tier"]]):
        source, wall, derived = record, record["wall_seconds"], False
    elif (record["case"], tier) in by_tier:
        source = by_tier[(record["case"], tier)]
        derived = record["size"] != "1"
        wall = derived_wall(record, source) if derived else source["wall_seconds"]
    else:
        return None
    box = container_credits(wall, platform)
    priced = [query_credits(q, limits[tier], platform) for q in source["queries"]]
    return box + sum(p[0] for p in priced), box + sum(p[1] for p in priced), derived


def _fmt_credits(c):
    if c is None:
        return "n/a"
    low, high, derived = c
    return f"{'~' if derived else ''}{low:.3f}-{high:.3f}"


def breaches(records, platform):
    box, found = platform["container"], []
    for tier in platform.get("refused_tiers", []):
        found.append(f"{tier}: every strategy: refused at submit")
    for r in records:
        where = f"{r['case']} {r['size']} ({r['tag']}, {r['query_tier']} query caps, cpus {r['cpus']})"
        if not passed(r):
            found.append(f"{where}: not a pass: {'; '.join(r['problems'])}")
        top = peak(r)
        if top > box["ram_gb"]:
            found.append(f"{where}: peak {top} GiB above the {box['ram_gb']} GiB request (every paid tier)")
        elif top > box["ram_gb"] - AT_RISK_GIB:
            found.append(f"{where}: peak {top} GiB, within {AT_RISK_GIB} GiB of the {box['ram_gb']} GiB request")
        ram = _by_value(platform.get("container_ram_caps_gb", {}), lambda cap: top > cap, "GB")
        if ram:
            found.append(f"{where}: peak {top} GiB above the container RAM cap of {ram}: no request fits")
        for cap, label in ((box["timeout_s"], "default timeout"), (box["max_timeout_s"], "max timeout")):
            if r["wall_seconds"] > cap:
                found.append(f"{where}: {r['wall_seconds']} s above the {cap} s {label}")
        slow = _by_value(platform.get("possible_container_timeouts", {}), lambda cap: r["wall_seconds"] > cap, "s")
        if slow:
            found.append(f"{where}: {r['wall_seconds']} s above {slow} (if those caps are live)")
        if r["result_bytes"] > box["disk_gb"] * 1024 ** 3:
            found.append(f"{where}: result.json {r['result_bytes']:,} B above the {box['disk_gb']} GB disk")
        if r["empty_configs"]:
            found.append(f"{where}: {r['empty_configs']} of {r['configs']} configs had no orders")
        caps = (r["tier_limits"] or {}).get(r["query_tier"], {})  # none when the limits read failed
        for q in r["queries"]:
            if q["ok"] and (q.get("server_seconds") or 0) > caps.get("maxExecutionTimeout", math.inf):
                found.append(f"{where}: {q['table']} ran {q['server_seconds']} s, above {caps['maxExecutionTimeout']} s")
            if q["cut"]:
                found.append(f"{where}: {q['table']} cut at {q['rows']} rows")
    return found


def strategy_table(case, records, by_tier, platform):
    cols = COLUMNS + tuple(f"Credits {t}" for t in platform["paid_tiers"])
    lines = [f"### {case}", "", "| " + " | ".join(cols) + " |", "|" + "---:|" * len(cols)]
    for r in records:
        rows = " + ".join(f"{q['rows']:,}" for q in r["queries"])
        cut = "yes" if any(q["cut"] for q in r["queries"]) else "no"
        peaks = r.get("stage_peaks_kib") or {}
        during = (max(peaks, key=peaks.get) if peaks else "") if passed(r) else "NOT A PASS"
        credits = [_fmt_credits(credits_at(r, t, by_tier, platform) if passed(r) else None)
                   for t in platform["paid_tiers"]]
        cells = [r["configs"], rows, cut, f"{r['fetch_seconds']:.1f}", f"{r['signal_seconds']:.1f}",
                 f"{r['simulation_seconds']:.1f}", f"{r['wall_seconds']:.1f}", f"{peak(r):.2f}", during,
                 f"{r['result_bytes']:,}", r["start_utc"][:16], *credits]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return "\n".join(lines)


def header(records):
    """What every row shares, said once: where and how the runs were made."""
    fields = {"engine_commit": lambda r: r["engine_commit"], "image": lambda r: r["image"],
              "python": lambda r: r["python"], "polars": lambda r: (r.get("packages") or {}).get("polars"),
              "user_tier": lambda r: r["user_tier"], "cpus": lambda r: r["cpus"],
              "polars_threads": lambda r: r["polars_threads"], "cpu_model": lambda r: r["cpu_model"],
              "api_host": lambda r: r["api_host"]}
    seen = {k: sorted({str(get(r)) for r in records}) for k, get in fields.items()}
    dates = sorted(r["start_utc"] for r in records)
    return ("Runs: " + "; ".join(f"{k} {', '.join(v)}" for k, v in seen.items())
            + f"; container request as in the platform file; started {dates[0]} to {dates[-1]}, one at a time. "
            "Peak = the larger of the process tree's summed PSS (sampled every 0.25-0.4 s) and ru_maxrss, "
            "with no memory limit. "
            "Credits = container time + data queries, low-high (high adds the queries' CPU-seconds term at its "
            "ceiling); ~ = derived from that tier's size-1 run. result.json details at most the top configs, "
            "so its size flattens with large sweeps.")


def engine_section(records, platform):
    by_tier = tier_records(records)
    main = [r for r in records if r["tag"] == "main"]
    cases = list(dict.fromkeys(r["case"] for r in main))
    parts = [header(main)] + [strategy_table(c, [r for r in main if r["case"] == c], by_tier, platform)
                              for c in cases]
    parts.append("### Cap breaches\n\n" + "\n".join(f"- {b}" for b in breaches(records, platform)))
    return "\n\n".join(parts)


def markdown(records, platform):
    """One section per engine commit, so runs of two releases never share a table."""
    engines = list(dict.fromkeys(r["engine_commit"] for r in records))
    if len(engines) == 1:
        return engine_section(records, platform) + "\n"
    return "\n\n".join(f"## Engine {e}\n\n" + engine_section([r for r in records if r["engine_commit"] == e], platform)
                       for e in engines) + "\n"

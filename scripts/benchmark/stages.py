"""Stage accounting over a run's spans: seconds per stage group and peak memory per stage group."""

FETCH_STAGES, SIM_STAGES = ("query", "fetch_ohlcv"), ("stats", "rank", "simulate")
STAGE_GROUP = {"query": "fetch", "fetch_ohlcv": "fetch", "generate_orders": "signal", "stats": "simulation",
               "rank": "simulation", "simulate": "simulation", "save": "save"}


def simulation_window(events):
    """From the stats build to the save: ranking, simulating, and building each config's result."""
    starts = [e["t0"] for e in events if e["stage"] == SIM_STAGES[0]]
    if not starts:
        return None
    saves = [e["t0"] for e in events if e["stage"] == "save"]
    ends = saves or [e["t1"] for e in events if e["stage"] in SIM_STAGES]
    return min(starts), max(ends)


def summarize(events, wall):
    """Fetch is all query time plus the providers' own; signal is generate_orders' own time; simulation
    is the simulation window; other is what is left of the wall time (startup, config, exit)."""
    own, counts = {}, {}
    for e in events:
        own[e["stage"]] = own.get(e["stage"], 0.0) + e["exclusive"]
        counts[e["stage"]] = counts.get(e["stage"], 0) + 1
    fetch = sum(own.get(s, 0.0) for s in FETCH_STAGES)
    signal = own.get("generate_orders", 0.0)
    window = simulation_window(events)
    sim = window[1] - window[0] if window else 0.0
    save = own.get("save", 0.0)
    return {"query_seconds": own.get("query", 0.0), "fetch_seconds": fetch, "signal_seconds": signal,
            "simulation_seconds": sim, "save_seconds": save, "other_seconds": wall - fetch - signal - sim - save,
            "stage_seconds": own, "stage_counts": counts}


def stage_peaks(samples, events):
    """Peak memory (KiB) during each stage group; a sample belongs to the innermost span covering it,
    and one between spans of the simulation window to the simulation."""
    peaks, window = {}, simulation_window(events)
    for t, kib in samples:
        covering = [e for e in events if e["t0"] <= t <= e["t1"]]
        if covering:
            group = STAGE_GROUP.get(max(covering, key=lambda e: e["t0"])["stage"], "other")
        else:
            group = "simulation" if window and window[0] <= t <= window[1] else "other"
        peaks[group] = max(peaks.get(group, 0), kib)
    return peaks

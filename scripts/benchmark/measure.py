"""Run a command and record its wall time and the peak memory of its whole process tree (Linux).

Memory is the sum of Pss over the process and all its descendants, read from /proc every 0.2 s, so a
page shared by forked workers counts once. ru_maxrss is the kernel's exact peak RSS of the largest
waited-for process. There is no cgroup or memory limit here: a run a container would kill still finishes.
"""

import os
import signal
import subprocess
import time

SAMPLE_SECONDS = 0.2
GIB_KIB = 1024 * 1024


def children_map():
    kids = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(name))
    return kids


def tree(root):
    kids, todo, seen = children_map(), [root], []
    while todo:
        pid = todo.pop()
        seen.append(pid)
        todo.extend(kids.get(pid, []))
    return seen


def pss_kib(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def measure(cmd, cpus=None, env=None, timeout=None, stdout=None, cwd=None):
    """cpus is a taskset list such as "0-3", or None to leave the affinity alone. A run past timeout
    seconds is killed with its whole process group."""
    argv = ["taskset", "-c", cpus, *cmd] if cpus else list(cmd)  # taskset execs: same pid
    start = time.time()
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=stdout, stderr=subprocess.STDOUT if stdout else None,
                            start_new_session=True)
    peak, samples, timed_out = 0, [], False
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        if timeout is not None and time.time() - start > timeout:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
        total = sum(pss_kib(p) for p in tree(proc.pid))
        samples.append((round(time.time(), 2), total))
        peak = max(peak, total)
        time.sleep(SAMPLE_SECONDS)
    end = time.time()
    return {"exit": os.waitstatus_to_exitcode(status), "timed_out": timed_out, "start": start, "end": end,
            "wall_seconds": round(end - start, 1), "peak_pss_gib": round(peak / GIB_KIB, 2),
            "maxrss_gib": round(usage.ru_maxrss / GIB_KIB, 2), "cpus": cpus or "all", "samples": samples}

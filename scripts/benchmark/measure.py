"""Run a command and record its wall time and the peak memory of its whole process tree (Linux).

Memory is the sum of Pss over the process and all its descendants, read from /proc, so a page shared
by forked workers counts once. A sample starts 0.2 s after the last one ended; reading a 20 GiB tree
takes about 0.15 s, so samples land 0.25 to 0.4 s apart. ru_maxrss is the kernel's exact peak RSS of
the largest waited-for process. There is no cgroup or memory limit here: a run a container would kill
still finishes.
"""

import glob
import os
import signal
import subprocess
import time

SAMPLE_SECONDS = 0.2
GIB_KIB = 1024 * 1024


def tree(root):
    """The root and its descendants, from each thread's /proc children list."""
    todo, seen = [root], []
    while todo:
        pid = todo.pop()
        seen.append(pid)
        for path in glob.glob(f"/proc/{pid}/task/*/children"):
            try:
                with open(path) as f:
                    todo.extend(int(p) for p in f.read().split())
            except OSError:
                pass
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
        now = time.time()
        if timeout is not None and now - start > timeout:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
        total = sum(pss_kib(p) for p in tree(proc.pid))
        samples.append((round(now, 2), total))
        peak = max(peak, total)
        time.sleep(SAMPLE_SECONDS)
    end = time.time()
    proc.returncode = os.waitstatus_to_exitcode(status)  # reaped by wait4; tell Popen so it doesn't warn
    return {"exit": proc.returncode, "timed_out": timed_out, "start": start, "end": end,
            "wall_seconds": round(end - start, 1), "peak_pss_gib": round(peak / GIB_KIB, 2),
            "maxrss_gib": round(usage.ru_maxrss / GIB_KIB, 2), "samples": samples}

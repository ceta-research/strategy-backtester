"""Path bootstrap for backtest scripts.

Import this first in any script under scripts/:
    import _bootstrap  # noqa: F401

Works both locally (resolves via __file__) and on cloud container (/session).
"""
import sys
import os

def _setup_path():
    candidates = []

    # 1. Local: this file is at scripts/_bootstrap.py → parent is repo root
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.dirname(here))

    # 2. Cloud container: project files are at /session
    candidates.append("/session")

    for p in candidates:
        if os.path.isdir(os.path.join(p, "lib")) and p not in sys.path:
            sys.path.insert(0, p)
            return

_setup_path()

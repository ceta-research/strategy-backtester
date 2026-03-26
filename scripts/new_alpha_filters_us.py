#!/usr/bin/env python3
"""New Alpha Filters on US market. Wrapper for new_alpha_filters.py."""
import sys
import os

sys.argv.extend(["--market", "us"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.new_alpha_filters import main
main()

#!/usr/bin/env python3
"""ORB Corrected Execution on US market. Wrapper for orb_standalone.py."""
import sys
import os

sys.argv.extend(["--market", "us"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.orb_standalone import main
main()

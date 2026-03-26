#!/usr/bin/env python3
"""Forced-Selling Dip on LSE market. Wrapper for forced_selling_dip.py."""
import sys
import os

sys.argv.extend(["--market", "lse"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.forced_selling_dip import main
main()

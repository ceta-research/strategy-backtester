#!/usr/bin/env python3
"""Earnings Volume Confirm on US market. Wrapper for earnings_volume_confirm.py."""
import sys
import os

sys.argv.extend(["--market", "us"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.earnings_volume_confirm import main
main()

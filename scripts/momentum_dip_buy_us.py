#!/usr/bin/env python3
"""Momentum Dip-Buy on US market. Wrapper for momentum_dip_buy.py."""
import sys
import os

sys.argv.extend(["--market", "us"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.momentum_dip_buy import main
main()

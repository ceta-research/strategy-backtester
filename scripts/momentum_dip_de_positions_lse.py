#!/usr/bin/env python3
"""Extended Momentum Dip-Buy on LSE market. Wrapper for momentum_dip_de_positions.py."""
import sys
import os

sys.argv.extend(["--market", "lse"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from scripts.momentum_dip_de_positions import main
main()

"""Shared indicator functions for standalone strategy scripts.

Pure-Python implementations (no polars dependency). For polars-based
indicators used in engine signal generators, see engine/signals/base.py.
"""

import math


def compute_z(values, lookback):
    """Rolling z-score of values with given lookback window."""
    z = [0.0] * len(values)
    for i in range(lookback, len(values)):
        w = values[i - lookback:i]
        m = sum(w) / len(w)
        v = sum((x - m) ** 2 for x in w) / len(w)
        s = math.sqrt(v) if v > 0 else 1e-9
        z[i] = (values[i] - m) / s
    return z


def compute_sma(values, period):
    """Simple moving average using a running sum."""
    sma = [0.0] * len(values)
    r = 0.0
    for i in range(len(values)):
        r += values[i]
        if i >= period:
            r -= values[i - period]
        sma[i] = r / min(i + 1, period)
    return sma


def compute_realized_vol(closes, window):
    """Annualized realized volatility from log returns."""
    vol = [0.0] * len(closes)
    for i in range(1, len(closes)):
        start = max(1, i - window + 1)
        rets = [math.log(closes[j] / closes[j - 1])
                for j in range(start, i + 1) if closes[j - 1] > 0 and closes[j] > 0]
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol[i] = math.sqrt(var) * math.sqrt(252)
    return vol

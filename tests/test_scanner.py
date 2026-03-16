"""Tests for engine/scanner.py."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from engine.scanner import fill_missing_dates, process


def make_synthetic_data(n_instruments=3, n_days=50):
    """Create synthetic tick data for testing."""
    rows = []
    base_epoch = 1577836800  # 2020-01-01
    for i in range(n_instruments):
        symbol = f"SYM{i}"
        for d in range(n_days):
            epoch = base_epoch + d * 86400
            price = 100 + i * 50 + d * 0.5 + np.random.randn() * 2
            volume = 1000000 + np.random.randint(0, 500000)
            rows.append({
                "date_epoch": epoch,
                "open": price - 1,
                "high": price + 2,
                "low": price - 2,
                "close": price,
                "average_price": price,
                "volume": volume,
                "symbol": symbol,
                "instrument": f"NSE:{symbol}",
                "exchange": "NSE",
            })
    return pd.DataFrame(rows)


def test_fill_missing_dates():
    df = make_synthetic_data(n_instruments=2, n_days=10)
    # Remove a few rows to create gaps
    df = df.drop([3, 7, 15]).reset_index(drop=True)
    original_len = len(df)
    filled = fill_missing_dates(df)
    assert len(filled) >= original_len


def test_scanner_process():
    df = make_synthetic_data(n_instruments=3, n_days=50)
    context = {
        "scanner_config_input": {
            "instruments": [[{"exchange": "NSE", "symbols": []}]],
            "price_threshold": [50],
            "avg_day_transaction_threshold": [{"period": 10, "threshold": 100}],
            "n_day_gain_threshold": [{"n": 5, "threshold": -999}],
        },
        "static_config": {"start_epoch": 1577836800},
        "start_epoch": 1577836800,
    }
    result = process(context, df)
    assert "scanner_config_ids" in result.columns
    assert "uid" in result.columns
    # Some rows should have scanner signals
    has_signals = result["scanner_config_ids"].notna().sum()
    assert has_signals > 0, "Expected some scanner signals"


if __name__ == "__main__":
    test_fill_missing_dates()
    test_scanner_process()
    print("All scanner tests passed!")

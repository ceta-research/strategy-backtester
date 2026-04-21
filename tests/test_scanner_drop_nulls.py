"""Tests for engine.scanner drop_nulls subset behavior.

Filled weekend rows (null open/high/low/volume, backward-filled close)
must be dropped; real rows with null volume/avg_price must survive.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine import scanner


def _make_context(start_epoch=1_600_000_000):
    return {
        "start_epoch": start_epoch,
        "scanner_config_input": {
            "instruments": [[{"exchange": "NSE", "symbols": []}]],
            "price_threshold": [0.0],
            "avg_day_transaction_threshold": [{"period": 2, "threshold": 0}],
            "n_day_gain_threshold": [{"n": 2, "threshold": -100.0}],
        },
        "static_config": {"start_epoch": start_epoch},
    }


def _base_row(epoch, close=100.0, open_=None, volume=1000, avg_price=None):
    """Build one bar row. None for open/avg_price omits (null) for testing."""
    return {
        "date_epoch": epoch,
        "open": open_ if open_ is not None else close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "average_price": avg_price if avg_price is not None else close,
        "volume": volume,
        "symbol": "TEST",
        "instrument": "NSE:TEST",
        "exchange": "NSE",
    }


class TestScannerDropNullsSubset(unittest.TestCase):

    def test_real_row_with_null_volume_retained(self):
        """Real trading-day row with null volume/avg_price must survive."""
        start = 1_600_000_000
        rows = []
        for i in range(5):
            epoch = start + i * 86400
            row = _base_row(epoch, close=100.0 + i)
            if i == 2:
                # Real row with null volume — should survive
                row["volume"] = None
                row["average_price"] = None
            rows.append(row)
        df = pl.DataFrame(rows)

        ctx = _make_context(start_epoch=start)
        out = scanner.process(ctx, df)

        # All 5 trading days present (none dropped for null volume/avg_price)
        epochs_out = set(out["date_epoch"].to_list())
        expected_epochs = {start + i * 86400 for i in range(5)}
        self.assertTrue(expected_epochs.issubset(epochs_out))

    def test_filled_weekend_rows_dropped(self):
        """fill_missing_dates injects rows with null open/high/low/volume
        but (after backward-fill) non-null close. Those rows must be
        dropped by the scanner."""
        # Build mon/wed/fri trades; scanner.fill_missing_dates adds tue/thu
        # with all-null OHLCV except a backward-filled close.
        start = 1_600_000_000
        rows = []
        for i in (0, 2, 4):  # sparse
            epoch = start + i * 86400
            rows.append(_base_row(epoch, close=100.0 + i))
        df = pl.DataFrame(rows)

        ctx = _make_context(start_epoch=start)
        out = scanner.process(ctx, df)

        # Output must only contain the original 3 trading days, not the
        # 2 filled intermediate days (those have null open).
        epochs_out = sorted(out["date_epoch"].to_list())
        self.assertEqual(epochs_out, [start + 0 * 86400,
                                       start + 2 * 86400,
                                       start + 4 * 86400])


if __name__ == "__main__":
    unittest.main()

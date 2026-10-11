"""Snapshot-backed nse_charting provider for the long-window study.

Every window reads ONE local snapshot of nse.nse_charting_day, so a 10y vs 20y difference
can't be a data-vintage difference. The repairs themselves now live in
engine/charting_repairs.py (and run live through NseChartingRepairedProvider); this module
keeps the snapshot and the study scripts' entry points, delegating to the engine functions.
"""
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from engine.constants import SECONDS_IN_ONE_DAY  # noqa: E402
from engine.data_provider import cast_ohlcv_dtypes, remove_price_oscillations  # noqa: E402
from engine import charting_repairs as R  # noqa: E402
from engine.charting_repairs import (  # noqa: E402,F401  (re-exported for the study scripts)
    MATCH_TOL, MIN_EFFECT, DOUBLE_MIN_EFFECT, back_adjust, drop_interleaved, split_stitches)

SNAP_DIR = os.environ.get("LW_SNAP_DIR", os.path.join(HERE, "snapshot"))
CHARTING = os.path.join(SNAP_DIR, "nse_charting_day.parquet")
SPLITS = os.path.join(SNAP_DIR, "splits_calendar_nse.parquet")


def snapshot():
    """Pull the full charting table and the NSE split calendar once."""
    from lib.cr_client import CetaResearch
    import io
    os.makedirs(SNAP_DIR, exist_ok=True)
    cr = CetaResearch()
    raw = cr.query(
        """SELECT symbol, date_epoch, open, high, low, close,
                  (high + low + close) / 3.0 AS average_price, volume
           FROM nse.nse_charting_day ORDER BY symbol, date_epoch""",
        timeout=600, limit=20000000, verbose=True, memory_mb=16384, threads=6,
        format="parquet")
    pl.read_parquet(io.BytesIO(raw)).write_parquet(CHARTING)
    raw = cr.query(R.SPLITS_SQL, timeout=600, limit=200000, verbose=True, format="parquet")
    pl.read_parquet(io.BytesIO(raw)).write_parquet(SPLITS)


def detect_raw_events(df: pl.DataFrame) -> pl.DataFrame:
    return R.detect_raw_events(df, pl.read_parquet(SPLITS))


class SnapshotChartingProvider:
    """Drop-in for NseChartingDataProvider reading the local snapshot.

    ca=True back-adjusts raw/double-adjusted splits; clean=True also drops interleaved stray
    series and splits recycled tickers.
    """

    def __init__(self, ca=False, clean=False):
        self.ca = ca
        self.clean = clean
        self.events = None
        self.dropped = None
        self.splits = None

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None,
                    prefetch_days=400):
        fetch_start = start_epoch - prefetch_days * SECONDS_IN_ONE_DAY
        df = pl.read_parquet(CHARTING)
        if self.ca:
            # Detect on the FULL series so an event just outside the window still shifts
            # the bars inside it.
            self.events = detect_raw_events(df)
            df = R.back_adjust(df, self.events)
            print(f"  CA: back-adjusted {self.events.height} raw split/bonus events")
        if self.clean:
            df, self.dropped = R.drop_interleaved(df)
            df, self.splits = R.split_stitches(df)
            print(f"  CLEAN: dropped {self.dropped.height} interleaved stray bars; split "
                  f"{self.splits['symbol'].n_unique()} symbols at >1y gaps")
        df = df.filter((pl.col("date_epoch") >= fetch_start) & (pl.col("date_epoch") <= end_epoch))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        df = cast_ohlcv_dtypes(df)
        df = df.with_columns([
            pl.lit("NSE").alias("exchange"),
            (pl.lit("NSE:") + pl.col("symbol")).alias("instrument"),
        ])
        df = remove_price_oscillations(df)
        df = df.sort(["instrument", "date_epoch"])
        print(f"  Snapshot: {df.height} rows, {df['instrument'].n_unique()} instruments "
              f"(ca={self.ca})")
        return df


if __name__ == "__main__":
    snapshot()

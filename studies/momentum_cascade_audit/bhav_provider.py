"""nse_bhavcopy_historical (survivorship-free, UNADJUSTED) with the calendar split repair.

bhav stamps each trading day at 00:00 IST (= charting's midnight-UTC epoch minus 19,800 s;
verified on RELIANCE 2015, 245/247 daily returns match, and re-checked on 2026 rows at load).
Every split is raw here, so detect_raw_events matches nearly every calendar event. No
interleave/stitch repairs: series codes keep partly-paid shares apart, and renames are not
re-keyed (a renamed company simply restarts its history, so it drops out of a 360-bar
momentum window for ~1.5 years).
"""
import io
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LW = os.path.join(os.path.dirname(HERE), "long_window")
sys.path[:0] = [ROOT, LW]
from engine.constants import SECONDS_IN_ONE_DAY  # noqa: E402
from engine.data_provider import cast_ohlcv_dtypes, remove_price_oscillations  # noqa: E402
from engine import charting_repairs as R  # noqa: E402
import providers as P  # noqa: E402

CACHE = os.path.join(P.SNAP_DIR, "bhav_eq.parquet")
IST = 19800


def snapshot():
    from lib.cr_client import CetaResearch
    raw = CetaResearch().query(
        """SELECT SYMBOL AS symbol, date_epoch, OPEN AS open, HIGH AS high, LOW AS low, CLOSE AS close,
                  VOLUME AS volume
           FROM nse.nse_bhavcopy_historical WHERE SERIES = 'EQ' ORDER BY SYMBOL, date_epoch""",
        timeout=900, limit=20000000, verbose=True, memory_mb=16384, threads=6, format="parquet")
    df = pl.read_parquet(io.BytesIO(raw)).with_columns(
        (pl.col("date_epoch").cast(pl.Int64) + IST).alias("date_epoch"),
        ((pl.col("high") + pl.col("low") + pl.col("close")) / 3.0).alias("average_price"))
    df.write_parquet(CACHE)
    return df


def check_offset():
    """Daily-return agreement with charting on RELIANCE 2026 at the +IST shift vs a UTC-day floor."""
    b = pl.read_parquet(CACHE).filter((pl.col("symbol") == "RELIANCE") & (pl.col("date_epoch") >= 1767225600))
    c = pl.read_parquet(P.CHARTING).filter((pl.col("symbol") == "RELIANCE") & (pl.col("date_epoch") >= 1767225600)) \
          .with_columns(pl.col("date_epoch").cast(pl.Int64))
    for name, bb in (("+IST (used)", b), ("UTC-day floor", b.with_columns(((pl.col("date_epoch") - IST)
                                                                           - (pl.col("date_epoch") - IST) % 86400).alias("date_epoch")))):
        j = c.join(bb, on="date_epoch", suffix="_b")
        print(f"  offset check {name}: {j.height} matched days, close equal on {(j['close'] == j['close_b']).sum()}")


class BhavRepairedProvider:
    """restrict_to_charting=True keeps only symbols charting also has: bhav prices and bhav rename
    gaps, but charting's (survivor) universe, so BH-full minus this = universe membership alone."""

    def __init__(self, restrict_to_charting=False):
        self.events = None
        self.restrict = restrict_to_charting

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None, prefetch_days=400):
        fetch_start = start_epoch - prefetch_days * SECONDS_IN_ONE_DAY
        df = pl.read_parquet(CACHE)
        self.events = R.detect_raw_events(df, pl.read_parquet(P.SPLITS))
        df = R.back_adjust(df, self.events)
        if self.restrict:
            keep = pl.read_parquet(P.CHARTING)["symbol"].unique()
            df = df.filter(pl.col("symbol").is_in(keep))
        print(f"  BHAV: {df['symbol'].n_unique()} symbols; back-adjusted {self.events.height} split events")
        df = df.filter((pl.col("date_epoch") >= fetch_start) & (pl.col("date_epoch") <= end_epoch))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        df = cast_ohlcv_dtypes(df).with_columns([
            pl.lit("NSE").alias("exchange"), (pl.lit("NSE:") + pl.col("symbol")).alias("instrument")])
        df = remove_price_oscillations(df).sort(["instrument", "date_epoch"])
        print(f"  BHAV: {df.height} rows, {df['instrument'].n_unique()} instruments in window")
        return df


if __name__ == "__main__":
    if not os.path.exists(CACHE):
        snapshot()
    check_offset()

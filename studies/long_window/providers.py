"""Snapshot-backed nse_charting provider, with optional split back-adjustment.

Every window in this study reads ONE local snapshot of nse.nse_charting_day, so a
10y vs 20y difference can't be a data-vintage difference. The SQL, derived
columns and oscillation filter match engine.data_provider.NseChartingDataProvider.

ca=True back-adjusts the split/bonus events that nse_charting_day left RAW
(2003-2010 mostly, plus ~90 later ones such as TCS 2018). Events come from
fmp.splits_calendar; an event is applied only when the charting series shows
the raw ratio jump within +-5 days of the ex-date, so already-adjusted events
are left alone.
"""
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from engine.constants import SECONDS_IN_ONE_DAY  # noqa: E402
from engine.data_provider import cast_ohlcv_dtypes, remove_price_oscillations  # noqa: E402

SNAP_DIR = os.environ.get("LW_SNAP_DIR", os.path.join(HERE, "snapshot"))
CHARTING = os.path.join(SNAP_DIR, "nse_charting_day.parquet")
SPLITS = os.path.join(SNAP_DIR, "splits_calendar_nse.parquet")

PRICE_COLS = ["open", "high", "low", "close", "average_price"]
MATCH_TOL = 0.10      # |ln(bar ratio / raw ratio)| must be under this
WINDOW_DAYS = 5       # search +-5 calendar days around the FMP ex-date
MIN_EFFECT = 0.05     # ignore events whose |ln ratio| is below this


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
    raw = cr.query(
        """SELECT replace(symbol, '.NS', '') AS symbol, dateEpoch AS ex_epoch, date AS ex_date,
                  numerator AS num, denominator AS den
           FROM fmp.splits_calendar
           WHERE symbol LIKE '%.NS' AND numerator > 0 AND denominator > 0
             AND numerator <> denominator""",
        timeout=600, limit=200000, verbose=True, format="parquet")
    pl.read_parquet(io.BytesIO(raw)).write_parquet(SPLITS)


def detect_raw_events(df: pl.DataFrame) -> pl.DataFrame:
    """Return split events that are visibly unadjusted in df, with the first post-ex bar.

    df must be the full (unfiltered) charting frame keyed by `symbol`.
    """
    # Skip sub-5% factors (FMP files rights issues as 1017:1000-style splits): too small to
    # move a stop, and an ordinary down day near the ex-date matches them by chance.
    ev = (pl.read_parquet(SPLITS)
            .with_columns((pl.col("den") / pl.col("num")).alias("raw_ratio"))
            .filter(pl.col("raw_ratio").log().abs() >= MIN_EFFECT))
    bars = (df.select(["symbol", "date_epoch", "close"])
              .sort(["symbol", "date_epoch"])
              .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol"))
                            .alias("bar_ratio"))
              .drop_nulls("bar_ratio"))
    w = WINDOW_DAYS * SECONDS_IN_ONE_DAY
    cand = (ev.join(bars, on="symbol", how="inner")
              .filter((pl.col("date_epoch") >= pl.col("ex_epoch") - w)
                      & (pl.col("date_epoch") <= pl.col("ex_epoch") + w))
              .with_columns((pl.col("bar_ratio") / pl.col("raw_ratio")).log().abs().alias("err"))
              .sort(["symbol", "ex_epoch", "err"])
              .group_by(["symbol", "ex_epoch"], maintain_order=True).first())
    # Raw if the best bar matches the split ratio in log space, within MATCH_TOL and within
    # half the split's own size (so an 11:10 bonus can't be matched by an ordinary -5% day).
    tol = pl.min_horizontal(pl.lit(MATCH_TOL), pl.col("raw_ratio").log().abs() * 0.5)
    hits = cand.filter(pl.col("err") < tol)
    # The calendar has duplicate rows; one jump bar is one event.
    return (hits.sort(["symbol", "date_epoch", "err"])
                .group_by(["symbol", "date_epoch"], maintain_order=True).first()
                .select(["symbol", "ex_epoch", "ex_date", "num", "den", "raw_ratio",
                         pl.col("date_epoch").alias("jump_epoch"), "bar_ratio"]))


def back_adjust(df: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """Scale every bar before each raw jump by the split ratio (volume inversely)."""
    if events.is_empty():
        return df
    # Cumulative factor per bar = product of raw_ratio over this symbol's later jumps.
    f = (df.select(["symbol", "date_epoch"])
           .join(events.select(["symbol", "jump_epoch", "raw_ratio"]), on="symbol", how="inner")
           .filter(pl.col("date_epoch") < pl.col("jump_epoch"))
           .group_by(["symbol", "date_epoch"])
           .agg(pl.col("raw_ratio").product().alias("_f")))
    df = df.join(f, on=["symbol", "date_epoch"], how="left").with_columns(
        pl.col("_f").fill_null(1.0))
    return df.with_columns(
        [(pl.col(c) * pl.col("_f")).alias(c) for c in PRICE_COLS]
        + [(pl.col("volume") / pl.col("_f")).alias("volume")]).drop("_f")


JUMP = 2.5            # a bar >2.5x away from the last kept close is off-level
PERSIST = 10          # an off-level run this long is a real level change, not a stray series


def drop_interleaved(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Drop bars from a second series interleaved into a symbol's history.

    TATASTEEL 2018-2020 alternates between the stock (~Rs 55) and a ~Rs 7-15 series
    (the partly-paid rights shares, by level and dates); BFUTILITIE 2012-2015 does the
    same. remove_price_oscillations misses both because the stray runs last days, not
    1-2 bars. Walk each affected symbol: keep a bar within JUMP of the last kept close;
    an off-level run is dropped unless it lasts PERSIST bars, which is accepted as a
    genuine level change.
    """
    df = df.sort(["symbol", "date_epoch"])
    r = pl.col("close") / pl.col("close").shift(1).over("symbol")
    hit = (df.filter((r > JUMP) | (r < 1 / JUMP))["symbol"].unique())
    keep_parts, dropped_parts = [df.filter(~pl.col("symbol").is_in(hit))], []
    for sym in hit:
        s = df.filter(pl.col("symbol") == sym)
        c = s["close"].to_numpy()
        keep = [True] * len(c)
        last, i = c[0], 1
        while i < len(c):
            if 1 / JUMP <= c[i] / last <= JUMP:
                last = c[i]
                i += 1
                continue
            # Off-level: find the run length, then accept or drop it.
            j = i
            while j < len(c) and not (1 / JUMP <= c[j] / last <= JUMP):
                j += 1
            if j - i >= PERSIST:
                last = c[i]
                i += 1
                continue
            for k in range(i, j):
                keep[k] = False
            i = j
        mask = pl.Series(keep)
        keep_parts.append(s.filter(mask))
        dropped_parts.append(s.filter(~mask).select(["symbol", "date_epoch", "close", "volume"]))
    dropped = pl.concat(dropped_parts) if dropped_parts else df.clear().select(
        ["symbol", "date_epoch", "close", "volume"])
    return pl.concat(keep_parts).sort(["symbol", "date_epoch"]), dropped


class SnapshotChartingProvider:
    """Drop-in for NseChartingDataProvider reading the local snapshot.

    ca=True back-adjusts raw splits; clean=True also drops interleaved stray series.
    """

    def __init__(self, ca=False, clean=False):
        self.ca = ca
        self.clean = clean
        self.events = None
        self.dropped = None

    def fetch_ohlcv(self, exchanges, symbols=None, start_epoch=None, end_epoch=None,
                    prefetch_days=400):
        fetch_start = start_epoch - prefetch_days * SECONDS_IN_ONE_DAY
        df = pl.read_parquet(CHARTING)
        if self.ca:
            # Detect on the FULL series so an event just outside the window still shifts
            # the bars inside it.
            self.events = detect_raw_events(df)
            df = back_adjust(df, self.events)
            print(f"  CA: back-adjusted {self.events.height} raw split/bonus events")
        if self.clean:
            df, self.dropped = drop_interleaved(df)
            print(f"  CLEAN: dropped {self.dropped.height} interleaved stray bars")
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

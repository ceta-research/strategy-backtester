"""Repairs for nse.nse_charting_day (BLOCKERS B045, B046, B047).

Pure functions over the full charting frame (columns symbol, date_epoch, open, high, low,
close, average_price, volume) and the NSE split calendar (symbol, ex_epoch, ex_date, num, den
from fmp.splits_calendar, '.NS' stripped). Found and validated in
studies/long_window (2026-10-11); `repair()` is what NseChartingRepairedProvider applies.

  1. detect_raw_events / back_adjust: splits the feed left RAW (ex-date bar jumps DOWN by
     the ratio: 520 events, 30 in 2026) and splits it adjusted TWICE (jumps UP: BALKRISIND
     2017, PERSISTENT 2024). Detected by matching the calendar to the bar whose ratio equals
     the split ratio, so already-correct events are left alone.
  2. drop_interleaved: a second thin price series interleaved into a symbol's history
     (TATASTEEL 2018-20 partly-paid shares at Rs 7 vs the stock at Rs 55), listing stubs on
     another scale (IDEAFORGE x551), and two quarantined stretches.
  3. split_stitches: an older company's history keyed to a recycled ticker (AWL, INDIGO,
     RATEGAIN ...) is split at any >1-year gap into separate instruments ("AWL~0").
"""
import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY

PRICE_COLS = ["open", "high", "low", "close", "average_price"]
MATCH_TOL = 0.10          # |ln(bar ratio / split ratio)| must be under this
WINDOW_DAYS = 5           # search +-5 calendar days around the FMP ex-date
MIN_EFFECT = 0.05         # ignore raw events whose |ln ratio| is below this
DOUBLE_MIN_EFFECT = 0.2   # double-adjusted (inverse) jumps are only repaired at >=22%

JUMP = 2.0                # a bar >2x away from the last kept close is off-level
PERSIST = 10              # an off-level run this long is a real level change, not a stray series
QUARANTINE = {            # symbol: (from_epoch, to_epoch) dropped outright; interleaved at ~1.6-2.5x
    "BFUTILITIE": (1325376000, 1451606399),   # 2012-01-01 .. 2015-12-31
    "WEWIN": (1483228800, 1546300799),        # 2017-01-01 .. 2018-12-31
}
STITCH_GAP_DAYS = 365

SPLITS_SQL = """SELECT replace(symbol, '.NS', '') AS symbol, dateEpoch AS ex_epoch, date AS ex_date,
                       numerator AS num, denominator AS den
                FROM fmp.splits_calendar
                WHERE symbol LIKE '%.NS' AND numerator > 0 AND denominator > 0
                  AND numerator <> denominator"""


def detect_raw_events(df: pl.DataFrame, splits: pl.DataFrame) -> pl.DataFrame:
    """Split events whose jump is visible in df, with the bar it lands on and its kind."""
    # Sub-5% factors (FMP files rights issues as 1017:1000 splits) are skipped: too small to
    # move a stop, and an ordinary down day near the ex-date matches them by chance.
    raw = (splits.with_columns((pl.col("den") / pl.col("num")).alias("raw_ratio"), pl.lit("raw").alias("kind"))
                 .filter(pl.col("raw_ratio").log().abs() >= MIN_EFFECT))
    dbl = (splits.with_columns((pl.col("num") / pl.col("den")).alias("raw_ratio"), pl.lit("double").alias("kind"))
                 .filter(pl.col("raw_ratio").log().abs() >= DOUBLE_MIN_EFFECT))
    ev = pl.concat([raw, dbl])
    bars = (df.select(["symbol", "date_epoch", "close"])
              .sort(["symbol", "date_epoch"])
              .with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol")).alias("bar_ratio"))
              .drop_nulls("bar_ratio"))
    w = WINDOW_DAYS * SECONDS_IN_ONE_DAY
    cand = (ev.join(bars, on="symbol", how="inner")
              .filter((pl.col("date_epoch") >= pl.col("ex_epoch") - w)
                      & (pl.col("date_epoch") <= pl.col("ex_epoch") + w))
              .with_columns((pl.col("bar_ratio") / pl.col("raw_ratio")).log().abs().alias("err"))
              .sort(["symbol", "ex_epoch", "err"])
              .group_by(["symbol", "ex_epoch"], maintain_order=True).first())
    # A hit must sit within MATCH_TOL in log space and within half the split's own size, so an
    # 11:10 bonus can't be matched by an ordinary -5% day.
    tol = pl.min_horizontal(pl.lit(MATCH_TOL), pl.col("raw_ratio").log().abs() * 0.5)
    hits = cand.filter(pl.col("err") < tol)
    # The calendar has duplicate rows; one jump bar is one event.
    return (hits.sort(["symbol", "date_epoch", "err"])
                .group_by(["symbol", "date_epoch"], maintain_order=True).first()
                .select(["symbol", "ex_epoch", "ex_date", "num", "den", "raw_ratio", "kind",
                         pl.col("date_epoch").alias("jump_epoch"), "bar_ratio"]))


def back_adjust(df: pl.DataFrame, events: pl.DataFrame) -> pl.DataFrame:
    """Scale every bar before each detected jump by its ratio (volume inversely)."""
    if events.is_empty():
        return df
    f = (df.select(["symbol", "date_epoch"])
           .join(events.select(["symbol", "jump_epoch", "raw_ratio"]), on="symbol", how="inner")
           .filter(pl.col("date_epoch") < pl.col("jump_epoch"))
           .sort(["symbol", "date_epoch", "jump_epoch"])  # fixes the order of the product
           .group_by(["symbol", "date_epoch"], maintain_order=True)
           .agg(pl.col("raw_ratio").product().alias("_f")))
    df = df.join(f, on=["symbol", "date_epoch"], how="left").with_columns(pl.col("_f").fill_null(1.0))
    return df.with_columns(
        [(pl.col(c) * pl.col("_f")).alias(c) for c in PRICE_COLS]
        + [(pl.col("volume") / pl.col("_f")).alias("volume")]).drop("_f")


def drop_interleaved(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Drop bars from a second series interleaved into a symbol's history, and listing stubs.

    Walk each symbol that has any >JUMP bar: keep a bar within JUMP of the last kept close; an
    off-level run is dropped unless it lasts PERSIST bars (a real level change). Bars before the
    last >3x jump in a symbol's first 60 are listing stubs (IDEAFORGE x551, AWL x153).
    """
    df = df.sort(["symbol", "date_epoch"])
    q = pl.lit(False)
    for sym, (a, b) in QUARANTINE.items():
        q = q | ((pl.col("symbol") == sym) & pl.col("date_epoch").is_between(a, b))
    quarantined = df.filter(q).select(["symbol", "date_epoch", "close", "volume"])
    df = df.filter(~q)
    r = pl.col("close") / pl.col("close").shift(1).over("symbol")
    hit = df.filter((r > JUMP) | (r < 1 / JUMP))["symbol"].unique()
    keep_parts, dropped_parts = [df.filter(~pl.col("symbol").is_in(hit))], []
    for sym in hit:
        s = df.filter(pl.col("symbol") == sym)
        c = s["close"].to_numpy()
        keep = [True] * len(c)
        start = 0
        for k in range(1, min(60, len(c))):
            if not (1 / 3 <= c[k] / c[k - 1] <= 3):
                start = k
        for k in range(start):
            keep[k] = False
        last, i = c[start], start + 1
        while i < len(c):
            if 1 / JUMP <= c[i] / last <= JUMP:
                last = c[i]
                i += 1
                continue
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
    dropped = pl.concat([quarantined] + dropped_parts)
    return pl.concat(keep_parts).sort(["symbol", "date_epoch"]), dropped


def split_stitches(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Split a symbol at any >STITCH_GAP_DAYS data gap into separate instruments.

    Older segments become "<SYM>~<k>"; the newest keeps the symbol. A gap can also be a
    suspension (ROML 2010->2020); a held position then exits at its last pre-gap close, which
    is optimistic, so the returned frame lists every split.
    """
    df = df.sort(["symbol", "date_epoch"]).with_columns(
        ((pl.col("date_epoch").cast(pl.Int64) - pl.col("date_epoch").shift(1).over("symbol").cast(pl.Int64))
         > STITCH_GAP_DAYS * SECONDS_IN_ONE_DAY).fill_null(False).cum_sum().over("symbol").alias("_seg"))
    df = df.with_columns(pl.col("_seg").max().over("symbol").alias("_last"))
    splits = (df.filter(pl.col("_last") > 0).group_by(["symbol", "_seg"], maintain_order=True)
                .agg(pl.col("date_epoch").min().alias("from"), pl.col("date_epoch").max().alias("to"))
                .sort(["symbol", "_seg"]))
    df = df.with_columns(
        pl.when(pl.col("_seg") < pl.col("_last"))
          .then(pl.col("symbol") + "~" + pl.col("_seg").cast(pl.Utf8))
          .otherwise(pl.col("symbol")).alias("symbol")).drop(["_seg", "_last"])
    return df, splits


def repair(df: pl.DataFrame, splits: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    """All repairs in order on the FULL frame. Returns (frame, info) with the detected events,
    dropped bars and stitch splits, so a caller can report them."""
    events = detect_raw_events(df, splits)
    df = back_adjust(df, events)
    df, dropped = drop_interleaved(df)
    df, stitched = split_stitches(df)
    return df, {"events": events, "dropped": dropped, "stitches": stitched}


NICE_RATIOS = [1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4, 1 / 5, 2 / 5, 3 / 5, 4 / 5, 1 / 6, 5 / 6, 1 / 7, 1 / 8,
               1 / 10, 10 / 11, 1 / 20, 4 / 7, 5 / 7, 1 / 1.1]


def looks_like_split(ratio: float) -> bool:
    """True if a close/prev-close ratio is a clean split or bonus factor in either direction.
    Used by the live runner to hold an exit when a held name prints a split the calendar
    hasn't listed yet (an unadjusted -50% bar is not a stop trigger)."""
    import math
    if not ratio or ratio <= 0:
        return False
    lr = abs(math.log(ratio))
    return lr > 0.08 and any(abs(lr - abs(math.log(n))) < 0.03 for n in NICE_RATIOS)

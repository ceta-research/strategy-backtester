"""Internal regime filter — computes regime signal from the strategy's own
scanner-passed universe rather than an external index.

For each trading day, computes:
    internal_regime_score = fraction of scanner-passed instruments
                           where close > SMA(sma_period)

Returns a set of "bull epochs" (days where the score exceeds a threshold),
compatible with the existing bull_epochs interface used by both eod_breakout
and eod_technical for regime gating and force-exit.

Usage:
    from engine.internal_regime import compute_internal_regime_epochs

    bull_epochs = compute_internal_regime_epochs(
        df_scanned,          # scanner output with scanner_config_ids populated
        sma_period=50,       # lookback for per-instrument SMA
        threshold=0.5,       # fraction of universe that must be above SMA
    )
    # bull_epochs is a set of int epochs — same shape as build_regime_filter()
"""

import polars as pl


def compute_internal_regime_epochs(
    df_scanned: pl.DataFrame,
    sma_period: int = 50,
    threshold: float = 0.5,
) -> set:
    """Compute internal regime and return set of bull epochs.

    Args:
        df_scanned: DataFrame with at minimum (instrument, date_epoch, close,
            scanner_config_ids). Only rows where scanner_config_ids is not null
            are considered part of the "passed universe".
        sma_period: Rolling window for per-instrument SMA.
        threshold: Fraction of scanner-passed instruments that must have
            close > their own SMA(sma_period) for a day to be "bull".

    Returns:
        Set of date_epoch values where internal_regime_score > threshold.
    """
    if df_scanned.is_empty():
        return set()

    # Work only with scanner-passed rows (non-null scanner_config_ids).
    df = df_scanned.filter(
        pl.col("scanner_config_ids").is_not_null()
    ).select(["instrument", "date_epoch", "close"])

    if df.is_empty():
        return set()

    # Compute per-instrument SMA.
    df = df.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("close")
          .rolling_mean(window_size=sma_period, min_samples=max(1, sma_period // 2))
          .over("instrument")
          .alias("_sma")
    )

    # Per-day: fraction of instruments with close > their SMA.
    daily = (
        df.group_by("date_epoch", maintain_order=True)
          .agg([
              (pl.col("close") > pl.col("_sma")).mean().alias("regime_score"),
          ])
          .sort("date_epoch")
    )

    # Bull epochs: days where regime_score > threshold.
    # Simple threshold (no hysteresis):
    bull_days = daily.filter(pl.col("regime_score") > threshold)
    return set(bull_days["date_epoch"].to_list())


def compute_internal_regime_epochs_hysteresis(
    df_scanned: pl.DataFrame,
    sma_period: int = 50,
    entry_threshold: float = 0.45,
    exit_threshold: float = 0.35,
) -> set:
    """Compute internal regime with hysteresis (asymmetric thresholds).

    State machine:
      - Start BEARISH
      - BEARISH → BULLISH when score > entry_threshold
      - BULLISH → BEARISH when score < exit_threshold
      - Between exit_threshold and entry_threshold: no change (dead zone)

    This eliminates whipsaw when the score hovers near a single threshold.

    Args:
        df_scanned: Same as compute_internal_regime_epochs.
        sma_period: Rolling window for per-instrument SMA.
        entry_threshold: Score must exceed this to flip BULLISH.
        exit_threshold: Score must drop below this to flip BEARISH.

    Returns:
        Set of date_epoch values where regime is BULLISH.
    """
    if df_scanned.is_empty():
        return set()

    df = df_scanned.filter(
        pl.col("scanner_config_ids").is_not_null()
    ).select(["instrument", "date_epoch", "close"])

    if df.is_empty():
        return set()

    df = df.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("close")
          .rolling_mean(window_size=sma_period, min_samples=max(1, sma_period // 2))
          .over("instrument")
          .alias("_sma")
    )

    daily = (
        df.group_by("date_epoch", maintain_order=True)
          .agg([
              (pl.col("close") > pl.col("_sma")).mean().alias("regime_score"),
          ])
          .sort("date_epoch")
    )

    # State machine walk
    epochs = daily["date_epoch"].to_list()
    scores = daily["regime_score"].to_list()

    bull_set = set()
    is_bull = False  # start bearish

    for i, (epoch, score) in enumerate(zip(epochs, scores)):
        if score is None:
            continue
        if is_bull:
            if score < exit_threshold:
                is_bull = False
            else:
                bull_set.add(epoch)
        else:
            if score > entry_threshold:
                is_bull = True
                bull_set.add(epoch)

    return bull_set


def compute_internal_regime_series(
    df_scanned: pl.DataFrame,
    sma_period: int = 50,
) -> pl.DataFrame:
    """Return the full daily regime score series (for analysis/debugging).

    Returns DataFrame with (date_epoch, regime_score) — one row per
    trading day. regime_score ∈ [0, 1].
    """
    if df_scanned.is_empty():
        return pl.DataFrame(schema={"date_epoch": pl.Int64, "regime_score": pl.Float64})

    df = df_scanned.filter(
        pl.col("scanner_config_ids").is_not_null()
    ).select(["instrument", "date_epoch", "close"])

    if df.is_empty():
        return pl.DataFrame(schema={"date_epoch": pl.Int64, "regime_score": pl.Float64})

    df = df.sort(["instrument", "date_epoch"]).with_columns(
        pl.col("close")
          .rolling_mean(window_size=sma_period, min_samples=max(1, sma_period // 2))
          .over("instrument")
          .alias("_sma")
    )

    daily = (
        df.group_by("date_epoch", maintain_order=True)
          .agg([
              (pl.col("close") > pl.col("_sma")).mean().alias("regime_score"),
              pl.len().alias("universe_size"),
          ])
          .sort("date_epoch")
    )
    return daily

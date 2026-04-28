"""OHLCV-window fetcher — Phase 3a of inspection drill (2026-04-28).

Thin wrapper around the existing data providers in
``engine/data_provider.py``. Given an ``(instrument, event_epoch,
window_days)`` triple, returns the OHLCV bars for a window of size
``[event_epoch - window_days, event_epoch + window_days]`` so a human
(or Phase-3 spot-check script) can eyeball a single trade without
reloading the whole universe.

Default provider is ``nse_charting`` to match both audit-drill champions
(eod_breakout / eod_technical). Other providers (cr, bhavcopy) are
selectable via ``--provider``.

Out of scope: data-pipeline integration, multi-symbol batch fetches.
This helper is one-symbol-at-a-time and intentionally small.

CLI usage:
    python3 scripts/fetch_ohlcv_window.py \
        --instrument NSE:RELIANCE \
        --epoch 1735689600 \
        --window-days 10

    # Save to CSV
    python3 scripts/fetch_ohlcv_window.py \
        --instrument NSE:TCS --epoch 1700000000 --window-days 5 \
        --csv /tmp/tcs_window.csv

Module usage:
    from scripts.fetch_ohlcv_window import fetch_window
    df = fetch_window("NSE:RELIANCE", event_epoch=1735689600, window_days=10)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402

from engine.constants import SECONDS_IN_ONE_DAY  # noqa: E402
from engine.data_provider import (  # noqa: E402
    BhavcopyDataProvider,
    CRDataProvider,
    NseChartingDataProvider,
)


PROVIDERS = {
    "nse_charting": NseChartingDataProvider,
    "bhavcopy": BhavcopyDataProvider,
    "cr": CRDataProvider,
}


def _epoch_to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _split_instrument(instrument: str) -> tuple[str, str]:
    """Split 'NSE:RELIANCE' into ('NSE', 'RELIANCE').

    Accepts a bare symbol too (assumes NSE).
    """
    if ":" in instrument:
        ex, sym = instrument.split(":", 1)
        return ex.strip().upper(), sym.strip().upper()
    return "NSE", instrument.strip().upper()


def fetch_window(
    instrument: str,
    event_epoch: int,
    window_days: int = 10,
    provider_name: str = "nse_charting",
    provider: Optional[object] = None,
) -> pl.DataFrame:
    """Fetch a small OHLCV window around ``event_epoch`` for ``instrument``.

    Args:
        instrument: ``"EXCHANGE:SYMBOL"`` (e.g. ``"NSE:RELIANCE"``). A bare
            symbol is treated as NSE.
        event_epoch: epoch (UTC seconds) of the event to centre the window
            around.
        window_days: number of calendar days BEFORE and AFTER the event
            to include. Default 10. The returned frame contains all
            trading-day rows whose ``date_epoch`` falls in
            ``[event_epoch - window_days*86400, event_epoch + window_days*86400]``.
        provider_name: one of ``PROVIDERS`` keys. Default ``"nse_charting"``
            matches both audit-drill champions.
        provider: optional pre-constructed provider instance (overrides
            ``provider_name``). Use this when re-using a provider across
            many calls in a notebook.

    Returns:
        ``pl.DataFrame`` with columns:
        ``date_epoch, date, open, high, low, close, average_price, volume,
        symbol, instrument, exchange, days_from_event, is_event_row``.
        Sorted ascending by ``date_epoch``. Empty frame if the window has
        no data.
    """
    exchange, symbol = _split_instrument(instrument)

    if provider is None:
        if provider_name not in PROVIDERS:
            raise ValueError(
                f"Unknown provider {provider_name!r}; "
                f"valid: {sorted(PROVIDERS)}"
            )
        provider = PROVIDERS[provider_name]()

    start_epoch = event_epoch - window_days * SECONDS_IN_ONE_DAY
    end_epoch = event_epoch + window_days * SECONDS_IN_ONE_DAY

    # The providers carry their own prefetch_days buffer for indicator warm-up.
    # We don't need that here; pass a tiny prefetch (1 day) and rely on the
    # post-fetch filter below to bound the window precisely.
    df = provider.fetch_ohlcv(
        exchanges=[exchange],
        symbols=[symbol],
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        prefetch_days=1,
    )

    if df is None or df.is_empty():
        return pl.DataFrame()

    # Bound to the requested window (providers may return prefetch days).
    df = df.filter(
        (pl.col("date_epoch") >= start_epoch)
        & (pl.col("date_epoch") <= end_epoch)
        & (pl.col("instrument") == f"{exchange}:{symbol}")
    )
    if df.is_empty():
        return df

    # Annotate: human-readable date, distance from event, event-row flag.
    df = df.with_columns([
        pl.from_epoch(pl.col("date_epoch"), time_unit="s")
          .dt.strftime("%Y-%m-%d")
          .alias("date"),
        ((pl.col("date_epoch") - event_epoch) // SECONDS_IN_ONE_DAY)
          .cast(pl.Int64)
          .alias("days_from_event"),
        (pl.col("date_epoch") == event_epoch).alias("is_event_row"),
    ]).sort("date_epoch")

    # Reorder for human inspection.
    preferred = [
        "date", "date_epoch", "days_from_event", "is_event_row",
        "open", "high", "low", "close", "average_price", "volume",
        "symbol", "instrument", "exchange",
    ]
    cols = [c for c in preferred if c in df.columns] + [
        c for c in df.columns if c not in preferred
    ]
    return df.select(cols)


def _format_for_terminal(df: pl.DataFrame) -> str:
    """Pretty-print the small frame with full width."""
    if df.is_empty():
        return "(empty — no data in window)"
    with pl.Config(
        tbl_rows=200,
        tbl_cols=20,
        tbl_width_chars=200,
        fmt_str_lengths=40,
    ):
        return str(df)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fetch a small OHLCV window around an event epoch."
    )
    p.add_argument("--instrument", required=True,
                   help="EXCHANGE:SYMBOL, e.g. NSE:RELIANCE")
    p.add_argument("--epoch", type=int, required=True,
                   help="Event epoch (UTC seconds)")
    p.add_argument("--window-days", type=int, default=10,
                   help="Bars before/after the event (default 10)")
    p.add_argument("--provider", default="nse_charting",
                   choices=sorted(PROVIDERS.keys()),
                   help="Data provider (default nse_charting)")
    p.add_argument("--csv", default=None,
                   help="Optional path; if given, write CSV here too.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress the printed table (useful with --csv).")
    args = p.parse_args()

    print(f"# instrument={args.instrument} "
          f"event={_epoch_to_iso(args.epoch)} ({args.epoch}) "
          f"window=±{args.window_days}d provider={args.provider}")

    df = fetch_window(
        instrument=args.instrument,
        event_epoch=args.epoch,
        window_days=args.window_days,
        provider_name=args.provider,
    )

    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or ".",
                    exist_ok=True)
        df.write_csv(args.csv)
        print(f"# wrote {df.height} rows to {args.csv}")

    if not args.quiet:
        print(_format_for_terminal(df))

    return 0


if __name__ == "__main__":
    sys.exit(main())

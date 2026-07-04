"""Kite Connect → portfolio JSON for live_daily_runner.py.

Reads holdings + cash from Kite via the read-only API (no static-IP
restriction — that policy applies only to order placement). Maintains a
small state file at `~/.ato_live/entry_dates.json` that remembers when
each symbol first appeared in your holdings, so the daily runner has
entry_date for every position.

Usage:
    export KITE_API_KEY=xxx                     # one-time, your Kite Connect app
    export KITE_ACCESS_TOKEN=yyy                # regenerated daily via OAuth
    python scripts/portfolio_helper.py > portfolio.json

    # First-time bootstrap: pull entry dates from your trade history.
    python scripts/portfolio_helper.py --bootstrap-from-trades > portfolio.json

    # Override state-file location:
    python scripts/portfolio_helper.py \\
        --state /custom/entry_dates.json \\
        --output portfolio.json

Credentials posture:
    - KITE_API_KEY / KITE_ACCESS_TOKEN read from env vars ONLY
    - Never logged, never written to disk
    - State file holds only {symbol: first_seen_date} — no credentials

Limitations:
    - Entry dates: for new symbols added to holdings AFTER the helper
      starts running, "first seen" = today (we can't archeologise from
      Kite's holdings response alone). For pre-existing positions, run
      with --bootstrap-from-trades to derive earliest BUY per symbol from
      the trade book (last 365 days max — Kite API limitation).
    - Only equity holdings are emitted (mutual fund / commodity / F&O
      positions are skipped).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from kiteconnect import KiteConnect
except ImportError:
    print(
        "ERROR: kiteconnect not installed. pip install kiteconnect",
        file=sys.stderr,
    )
    sys.exit(2)


DEFAULT_STATE_PATH = Path.home() / ".ato_live" / "entry_dates.json"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch Kite portfolio → JSON for live_daily_runner.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to entry-dates state file (default: {DEFAULT_STATE_PATH})",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path (default: stdout)",
    )
    p.add_argument(
        "--bootstrap-from-trades",
        action="store_true",
        help=(
            "Pull trade history from Kite to derive earliest BUY date per "
            "currently-held symbol. Only needed once (Kite API caps at ~365d)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print fetched data without updating state file",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_kite_client() -> KiteConnect:
    api_key = os.environ.get("KITE_API_KEY")
    access_token = os.environ.get("KITE_ACCESS_TOKEN")
    if not api_key:
        raise EnvironmentError(
            "KITE_API_KEY env var not set. Get this from your Kite Connect "
            "app at https://kite.trade (one-time)."
        )
    if not access_token:
        raise EnvironmentError(
            "KITE_ACCESS_TOKEN env var not set. Regenerate daily via the "
            "Kite OAuth login flow."
        )
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return {"entry_dates": {}, "last_updated": None}
    with open(path) as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = date.today().isoformat()
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_holdings_and_cash(kite: KiteConnect) -> tuple[list[dict], float]:
    """Return (holdings_list, cash_inr).

    Filters to equity holdings with positive net quantity (qty + t1).
    """
    holdings_raw = kite.holdings()
    margins = kite.margins()  # default segment = "equity"
    cash = float(margins.get("equity", {}).get("available", {}).get("cash", 0))

    holdings = []
    for h in holdings_raw:
        if h.get("product") not in ("CNC", "DELIVERY", "MTF"):
            # Only delivery-style equity. Skip MIS / NRML / other intraday.
            continue
        qty = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
        if qty <= 0:
            continue
        holdings.append({
            "symbol": h["tradingsymbol"],
            "exchange": h["exchange"],
            "qty": qty,
            "avg_price": float(h["average_price"]),
            "isin": h.get("isin"),
            "last_price": float(h.get("last_price", 0)),
        })
    return holdings, cash


def bootstrap_entry_dates_from_trades(
    kite: KiteConnect, holdings: list[dict]
) -> dict[str, str]:
    """For each held symbol, pull recent trades and find the earliest BUY date.

    Kite's `trades()` returns up to ~365d of history. Symbols without any
    BUY in that window get None.
    """
    entry_dates: dict[str, str] = {}
    held_symbols = {h["symbol"] for h in holdings}

    try:
        trades = kite.trades()
    except Exception as e:
        print(f"WARN: kite.trades() failed: {e}", file=sys.stderr)
        return entry_dates

    earliest: dict[str, str] = {}
    for t in trades:
        sym = t.get("tradingsymbol")
        if sym not in held_symbols:
            continue
        if t.get("transaction_type") != "BUY":
            continue
        ts = t.get("fill_timestamp") or t.get("order_timestamp") or t.get("exchange_timestamp")
        if ts is None:
            continue
        # Kite returns datetime objects; coerce to ISO date.
        if isinstance(ts, datetime):
            d_str = ts.date().isoformat()
        else:
            d_str = str(ts)[:10]
        if sym not in earliest or d_str < earliest[sym]:
            earliest[sym] = d_str

    return earliest


# ---------------------------------------------------------------------------
# State merge logic
# ---------------------------------------------------------------------------

def update_entry_dates(
    state_entry_dates: dict[str, str],
    holdings: list[dict],
    bootstrap_dates: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge today's holdings into the entry-date state.

    Rules:
        1. Symbol already in state: keep existing entry_date (don't overwrite).
        2. Symbol new to state, has bootstrap_date: use bootstrap_date.
        3. Symbol new to state, no bootstrap: use today's date as fallback.

    Symbols absent from holdings (i.e., positions that were exited) are
    NOT removed from state — kept as historical record. The runner only
    reads entries that are in the current portfolio JSON.
    """
    today_iso = date.today().isoformat()
    bootstrap_dates = bootstrap_dates or {}
    held_keys = {f"{h['exchange']}:{h['symbol']}" for h in holdings}

    new_state = dict(state_entry_dates)
    for h in holdings:
        key = f"{h['exchange']}:{h['symbol']}"
        if key in new_state:
            continue
        if h["symbol"] in bootstrap_dates:
            new_state[key] = bootstrap_dates[h["symbol"]]
        else:
            new_state[key] = today_iso
            print(
                f"WARN: {key} new to state, using today ({today_iso}) as "
                f"entry_date fallback. Run with --bootstrap-from-trades "
                f"or edit state file manually for accuracy.",
                file=sys.stderr,
            )

    return new_state


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_portfolio_json(
    holdings: list[dict], cash: float, entry_dates: dict[str, str]
) -> dict:
    today_iso = date.today().isoformat()
    positions = []
    for h in holdings:
        key = f"{h['exchange']}:{h['symbol']}"
        positions.append({
            "symbol": h["symbol"],
            "exchange": h["exchange"],
            "qty": h["qty"],
            "avg_price": h["avg_price"],
            "entry_date": entry_dates.get(key, today_iso),
        })
    return {
        "as_of_date": today_iso,
        "cash_available_inr": round(cash, 2),
        "positions": positions,
    }


def main() -> int:
    args = parse_args()
    kite = get_kite_client()

    print(f"Fetching Kite holdings and margins...", file=sys.stderr)
    holdings, cash = fetch_holdings_and_cash(kite)
    print(
        f"  positions: {len(holdings)}, cash: Rs {cash:,.0f}",
        file=sys.stderr,
    )

    bootstrap_dates: dict[str, str] | None = None
    if args.bootstrap_from_trades:
        print(f"Fetching trade history for entry-date bootstrap...", file=sys.stderr)
        bootstrap_dates = bootstrap_entry_dates_from_trades(kite, holdings)
        print(
            f"  derived entry_dates for {len(bootstrap_dates)} symbol(s) "
            f"from trades",
            file=sys.stderr,
        )

    state = load_state(args.state)
    new_entry_dates = update_entry_dates(
        state.get("entry_dates", {}), holdings, bootstrap_dates
    )

    portfolio = build_portfolio_json(holdings, cash, new_entry_dates)

    if not args.dry_run:
        state["entry_dates"] = new_entry_dates
        save_state(args.state, state)
        print(f"State saved: {args.state}", file=sys.stderr)

    payload = json.dumps(portfolio, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(payload)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())

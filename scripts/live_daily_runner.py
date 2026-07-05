"""Daily live decision runner — IR-hyst champion (eod_breakout).

Reads a portfolio JSON, runs the SAME signal generator the backtest uses
against today's data, and emits SELL/BUY recommendations for tomorrow's
open. User places orders manually (NSE static-IP requirement only applies
to order-placement endpoints, not reads / not signal generation).

Usage:
    export CR_API_KEY=...                       # data fetch
    python scripts/live_daily_runner.py \\
        --portfolio /path/to/portfolio.json \\
        --signal-date 2026-05-04 \\
        [--config strategies/eod_breakout/config_ir_hyst_best.yaml] \\
        [--output /path/to/recommendations.json] \\
        [--max-positions 15] \\
        [--min-order-value 50000] \\
        [--lookback-days 30]

Portfolio JSON schema:
    {
      "as_of_date": "2026-05-04",
      "cash_available_inr": 200000,
      "positions": [
        {
          "symbol": "RELIANCE",
          "exchange": "NSE",
          "qty": 50,
          "avg_price": 2400.0,
          "entry_date": "2026-04-15"
        }
      ]
    }

Design notes:
- The signal generator's entry filter requires a non-null `next_epoch` —
  i.e. the latest bar in the data is filtered out by default. To get
  signals for tomorrow's open, we append a synthetic T+1 bar per
  instrument (open=close[T] as a placeholder for tomorrow's open) so
  signal_epoch rows produce orders with `entry_epoch = T+1`.
- Exits are computed per-held-position via `_walk_forward_tsl` with the
  internal-regime-hysteresis epochs the signal generator computes — same
  exit semantics as the backtest.
- Capital sizing per BUY uses the config's `max_order_value` (default
  4.5% of 125d avg turnover) for parity with the backtest.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import polars as pl  # noqa: E402

from engine.config_loader import (  # noqa: E402
    load_config,
    get_entry_config_iterator,
    get_exit_config_iterator,
    get_simulation_config_iterator,
)
from engine.data_provider import NseChartingDataProvider  # noqa: E402
from engine.internal_regime import (  # noqa: E402
    compute_internal_regime_epochs,
    compute_internal_regime_epochs_hysteresis,
)
from engine.ranking import sort_orders_by_highest_gainer  # noqa: E402
from engine.signals import eod_breakout  # noqa: E402, F401
from engine.signals.base import run_scanner, add_next_day_values  # noqa: E402
from engine.signals.eod_breakout import (  # noqa: E402
    EodBreakoutSignalGenerator,
    _walk_forward_tsl,
)


SECONDS_IN_ONE_DAY = 86400
DEFAULT_CONFIG = "strategies/eod_breakout/config_ir_hyst_best.yaml"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily live decision runner — IR-hyst champion.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--portfolio",
        required=True,
        type=Path,
        help="Path to portfolio JSON (see schema in module docstring)",
    )
    p.add_argument(
        "--signal-date",
        required=True,
        help="Signal day, YYYY-MM-DD. Must be a trading day with data.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=Path(REPO_ROOT) / DEFAULT_CONFIG,
        help=f"Champion config YAML (default: {DEFAULT_CONFIG})",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write recommendations JSON",
    )
    p.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help="Override max_positions from config",
    )
    p.add_argument(
        "--min-order-value",
        type=float,
        default=50_000.0,
        help="Skip BUYs below this rupee value (default 50000)",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=252,
        help=(
            "Active signal range in days (default 252). Must be long enough "
            "to cover the regime SMA + warmup so internal-regime hysteresis "
            "computes correctly. Prefetch is unchanged."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def date_to_epoch_utc(date_str: str) -> int:
    """Parse YYYY-MM-DD as midnight UTC."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def epoch_to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Portfolio loading + validation
# ---------------------------------------------------------------------------

REQUIRED_PORTFOLIO_KEYS = {"as_of_date", "cash_available_inr", "positions"}
REQUIRED_POSITION_KEYS = {"symbol", "exchange", "qty", "avg_price", "entry_date"}


def load_portfolio(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"portfolio JSON not found: {path}")
    with open(path) as f:
        data = json.load(f)

    missing = REQUIRED_PORTFOLIO_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"portfolio JSON missing keys: {sorted(missing)}")

    if not isinstance(data["positions"], list):
        raise ValueError("portfolio.positions must be a list")
    for i, pos in enumerate(data["positions"]):
        missing = REQUIRED_POSITION_KEYS - set(pos.keys())
        if missing:
            raise ValueError(f"position[{i}] missing keys: {sorted(missing)}")
        if pos["qty"] <= 0:
            raise ValueError(f"position[{i}] {pos['symbol']}: qty must be > 0")
        if pos["avg_price"] <= 0:
            raise ValueError(f"position[{i}] {pos['symbol']}: avg_price must be > 0")
        # Validate date format
        date_to_epoch_utc(pos["entry_date"])

    return data


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_nse_universe(start_epoch: int, end_epoch: int) -> pl.DataFrame:
    """Fetch full NSE OHLCV via the same provider the backtest uses."""
    if not (os.environ.get("CR_API_KEY") or os.environ.get("TS_API_KEY")):
        raise EnvironmentError(
            "Neither CR_API_KEY nor TS_API_KEY env var is set — "
            "cr_client cannot fetch data."
        )
    provider = NseChartingDataProvider()
    df = provider.fetch_ohlcv(
        exchanges=["NSE"],
        symbols=None,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        prefetch_days=0,  # caller already extended start_epoch
    )
    if df.is_empty():
        raise RuntimeError(
            f"No data returned for {epoch_to_iso(start_epoch)} → "
            f"{epoch_to_iso(end_epoch)}"
        )
    return df


# ---------------------------------------------------------------------------
# Synthetic next-day bar
# ---------------------------------------------------------------------------

def append_synthetic_next_day(
    df: pl.DataFrame, signal_epoch: int, next_day_epoch: int
) -> pl.DataFrame:
    """Append placeholder T+1 AND T+2 rows per instrument with data at signal_epoch.

    Why two bars: the signal generator calls `add_next_day_values()` which
    drops the LAST bar per instrument (no `next_epoch` after a `shift(-1)`).
    With only one synthetic at T+1, the T+1 bar gets dropped, signal_epoch
    rows get next_epoch=T+1 but the generator's per-instrument `exit_data`
    walk doesn't have T+1 in `epochs` — so orders at signal_epoch fail the
    `epochs.index(entry_epoch)` lookup and are silently skipped.

    Adding T+2 as well: T+1 survives the drop (it now has next_epoch=T+2),
    `exit_data` includes T+1, and signal_epoch entries resolve correctly.
    Both synthetic bars use `open=close[signal_epoch]` and `volume=0`, so
    they fail the scanner liquidity gate and the `close > open` entry
    clause — they cannot fire entries themselves.
    """
    signal_rows = df.filter(pl.col("date_epoch") == signal_epoch)
    if signal_rows.is_empty():
        raise ValueError(
            f"No bars at signal_epoch={epoch_to_iso(signal_epoch)} — "
            f"check date is a trading day with available data."
        )

    def _make(epoch: int) -> pl.DataFrame:
        return signal_rows.with_columns([
            pl.lit(epoch).cast(pl.Int64).alias("date_epoch"),
            pl.col("close").alias("open"),
            pl.col("close").alias("high"),
            pl.col("close").alias("low"),
            pl.col("close").alias("average_price"),
            pl.lit(0.0).alias("volume"),
        ])

    synthetic_t1 = _make(next_day_epoch)
    synthetic_t2 = _make(next_day_epoch + SECONDS_IN_ONE_DAY)
    return pl.concat([df, synthetic_t1, synthetic_t2], how="diagonal").sort(
        ["instrument", "date_epoch"]
    )


# ---------------------------------------------------------------------------
# Internal regime helper
# ---------------------------------------------------------------------------

def compute_regime_epochs(df_trimmed: pl.DataFrame, entry_cfg: dict) -> set[int]:
    """Replicate the IR-hyst champion's internal-regime computation."""
    ir_sma = entry_cfg.get("internal_regime_sma_period", 0)
    ir_thr = entry_cfg.get("internal_regime_threshold", 0.5)
    ir_exit = entry_cfg.get("internal_regime_exit_threshold", 0)
    if ir_sma <= 0:
        return set()
    if ir_exit > 0:
        return compute_internal_regime_epochs_hysteresis(
            df_trimmed,
            sma_period=ir_sma,
            entry_threshold=ir_thr,
            exit_threshold=ir_exit,
        )
    return compute_internal_regime_epochs(
        df_trimmed, sma_period=ir_sma, threshold=ir_thr
    )


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def compute_exits(
    positions: list[dict],
    df_with_synth: pl.DataFrame,
    bull_epochs: set[int],
    entry_cfg: dict,
    exit_cfg: dict,
    signal_epoch: int,
    next_day_epoch: int,
) -> list[dict]:
    """For each held position, run _walk_forward_tsl. Emit two cases:

    - "fresh": exit_epoch == next_day_epoch → place at MOO tomorrow
    - "overdue": exit_epoch <= signal_epoch → strategy exit fired in the past
      but the user is still holding. Catch-up sell at MOO tomorrow.

    No exit emitted if `_walk_forward_tsl` finds no trigger or returns an
    epoch beyond next_day_epoch.
    """
    df_ind = add_next_day_values(df_with_synth)

    # Today's close per instrument — used as the realistic exit-price estimate
    # for BOTH fresh and overdue exits (user sells at tomorrow's MOO ≈ today's
    # close, regardless of when the strategy's original trigger was).
    today_close: dict[str, float] = dict(zip(
        df_with_synth.filter(pl.col("date_epoch") == signal_epoch)["instrument"].to_list(),
        df_with_synth.filter(pl.col("date_epoch") == signal_epoch)["close"].to_list(),
    ))

    use_regime = bool(bull_epochs)
    force_exit_flip = entry_cfg.get("force_exit_on_regime_flip", False)

    exits: list[dict] = []
    for pos in positions:
        instrument = f"{pos['exchange']}:{pos['symbol']}"
        df_inst = df_ind.filter(pl.col("instrument") == instrument).sort("date_epoch")
        if df_inst.is_empty():
            print(f"  WARN: no data for {instrument} — skipping exit eval")
            continue

        epochs = df_inst["date_epoch"].to_list()
        closes = df_inst["close"].to_list()
        opens = df_inst["open"].to_list()
        next_opens = df_inst["next_open"].to_list()
        next_epochs = df_inst["next_epoch"].to_list()

        entry_epoch_user = date_to_epoch_utc(pos["entry_date"])
        # Map user's entry_date to the closest trading day >= entry_date
        start_idx = next(
            (i for i, e in enumerate(epochs) if e >= entry_epoch_user), None
        )
        if start_idx is None:
            print(
                f"  WARN: {instrument} entry_date {pos['entry_date']} is past "
                f"the data range — skipping"
            )
            continue

        result = _walk_forward_tsl(
            epochs=epochs,
            closes=closes,
            opens=opens,
            next_opens=next_opens,
            next_epochs=next_epochs,
            start_idx=start_idx,
            entry_epoch=epochs[start_idx],
            trailing_stop_pct=exit_cfg["trailing_stop_pct"],
            min_hold_days=exit_cfg.get("min_hold_time_days", 0),
            bull_epochs=bull_epochs if (use_regime and force_exit_flip) else None,
            entry_price=float(pos["avg_price"]),
            tsl_tighten_after_pct=exit_cfg.get("tsl_tighten_after_pct", 999.0),
            tsl_tight_pct=exit_cfg.get("tsl_tight_pct", 0.0),
        )
        exit_epoch, exit_price, exit_reason = result

        if exit_epoch is None or exit_price is None:
            continue

        exit_epoch_i = int(exit_epoch)
        if exit_epoch_i > int(next_day_epoch):
            # Strategy hasn't exited yet — keep holding.
            continue

        if exit_epoch_i == int(next_day_epoch):
            status = "fresh"
        else:
            status = "overdue"

        # Realistic estimate: user sells at tomorrow's MOO, so use today's
        # close (≈ tomorrow's open) for BOTH fresh and overdue cases.
        price_estimate = float(today_close.get(instrument, float(exit_price)))
        pnl_pct = (price_estimate - float(pos["avg_price"])) / float(pos["avg_price"]) * 100
        exits.append({
            "symbol": pos["symbol"],
            "exchange": pos["exchange"],
            "qty": int(pos["qty"]),
            "status": status,
            "strategy_exit_epoch": exit_epoch_i,
            "strategy_exit_date": epoch_to_iso(exit_epoch_i),
            "exit_price_estimate": price_estimate,
            "exit_reason": exit_reason,
            "entry_date": pos["entry_date"],
            "avg_price": float(pos["avg_price"]),
            "estimated_pnl_pct": pnl_pct,
        })
    return exits


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def compute_avg_txn_per_instrument(
    df_data: pl.DataFrame, signal_epoch: int, lookback: int = 125
) -> dict[str, float]:
    """Mean turnover (volume × average_price) over last `lookback` bars at signal_epoch."""
    df = df_data.filter(pl.col("date_epoch") <= signal_epoch).sort(
        ["instrument", "date_epoch"]
    )
    df = df.with_columns(
        (pl.col("volume") * pl.col("average_price")).alias("turnover")
    )
    df = df.group_by("instrument", maintain_order=True).tail(lookback)
    df = df.group_by("instrument").agg(pl.col("turnover").mean().alias("avg_txn"))
    return dict(zip(df["instrument"].to_list(), df["avg_txn"].to_list()))


def compute_entries(
    df_orders: pl.DataFrame,
    df_data: pl.DataFrame,
    signal_epoch: int,
    next_day_epoch: int,
    held_symbols: set[str],
    sim_cfg: dict,
    slots_open: int,
    cash_available: float,
    account_value: float,
    max_positions: int,
    min_order_value: float,
) -> list[dict]:
    """Filter orders to next-day entries, rank by top_gainer 180d, apply caps.

    Sizing matches the simulator (engine/simulator.py:349):
        order_value = account_value / max_positions
        order_value = min(order_value, max_order_value_cap)
        order_value = order_value * order_value_multiplier
    """
    if df_orders.is_empty() or slots_open <= 0:
        return []

    df_today = df_orders.filter(pl.col("entry_epoch") == next_day_epoch)
    if df_today.is_empty():
        return []

    # Skip already-held symbols (NSE-only universe per IR-hyst champion)
    held_instruments = {f"NSE:{s}" for s in held_symbols}
    df_today = df_today.filter(~pl.col("instrument").is_in(list(held_instruments)))
    if df_today.is_empty():
        return []

    # Rank by top_gainer (config-driven window). df_data is the time series
    # the ranking function expects; pass the synthetic-augmented df so the
    # ranking sees signal_epoch and computes the right shift.
    rwin = int(sim_cfg["order_ranking_window_days"])
    # TODO (IPO-age experiment, 2026-07-05): if a config with
    # simulation.ranking_nulls_last: [true] is ever promoted to live, pass
    # nulls_last=bool(sim_cfg.get("ranking_nulls_last", False)) here —
    # otherwise live keeps legacy nulls-FIRST while the backtest ran
    # nulls-last, and the two silently diverge on young listings.
    df_ranked = sort_orders_by_highest_gainer(df_today, df_data, rwin)

    # Capital sizing per the config's max_order_value
    max_order_cfg = sim_cfg["max_order_value"]
    max_value_lookup: dict[str, float] = {}
    fixed_max_value: float | None = None
    if max_order_cfg["type"] == "percentage_of_instrument_avg_txn":
        pct = float(max_order_cfg["value"]) / 100.0
        avg_txn_map = compute_avg_txn_per_instrument(df_data, signal_epoch)
        for inst, avg_txn in avg_txn_map.items():
            if avg_txn is not None:
                max_value_lookup[inst] = float(avg_txn) * pct
    else:
        fixed_max_value = float(max_order_cfg["value"])

    multiplier = float(sim_cfg.get("order_value_multiplier", 1.0))
    base_per_position = (account_value / max_positions) * multiplier

    cash_remaining = float(cash_available)
    entries: list[dict] = []
    rank_counter = 1
    for row in df_ranked.iter_rows(named=True):
        if len(entries) >= slots_open:
            break
        if cash_remaining < min_order_value:
            break
        instrument = row["instrument"]
        symbol = instrument.split(":", 1)[1]
        entry_price = float(row["entry_price"])
        if entry_price <= 0:
            continue

        if fixed_max_value is not None:
            cap = fixed_max_value
        else:
            cap = max_value_lookup.get(instrument)
            if cap is None or cap <= 0:
                continue

        # Simulator parity: per-position target = account/max_positions × mult,
        # capped by max_order_value, then bounded by available cash.
        order_value_target = min(base_per_position, cap, cash_remaining)
        if order_value_target < min_order_value:
            rank_counter += 1
            continue
        target_qty = int(order_value_target / entry_price)
        if target_qty < 1:
            rank_counter += 1
            continue
        actual_value = target_qty * entry_price
        cash_remaining -= actual_value

        entries.append({
            "symbol": symbol,
            "exchange": "NSE",
            "target_qty": target_qty,
            "target_value_inr": round(actual_value, 2),
            "entry_price_estimate": entry_price,
            "rank": rank_counter,
            "max_order_cap_inr": round(cap, 2),
            "per_position_target_inr": round(base_per_position, 2),
        })
        rank_counter += 1

    return entries


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_human(
    signal_date: str,
    trade_date: str,
    regime_label: str,
    breadth_score: float | None,
    exits: list[dict],
    entries: list[dict],
    cash_post_exits: float,
    slots_open: int,
    held_count: int,
    max_positions: int,
) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 78)
    lines.append(
        f"Signal date: {signal_date}   |   Trade date: {trade_date} (next open)"
    )
    lines.append(
        f"Regime: {regime_label}"
        + (f"   |   universe breadth: {breadth_score:.3f}" if breadth_score is not None else "")
    )
    lines.append(
        f"Cash (post-exits): Rs {cash_post_exits:,.0f}   "
        f"|   Held: {held_count}/{max_positions}   |   Slots open: {slots_open}"
    )
    lines.append("=" * 78)
    lines.append("")

    if exits:
        lines.append(f"SELLS ({len(exits)}) — place at MOO {trade_date}:")
        for e in exits:
            tag = "[OVERDUE since " + e["strategy_exit_date"] + "]" if e["status"] == "overdue" else ""
            lines.append(
                f"  {e['symbol']:<14} qty={e['qty']:>5}  "
                f"reason={e['exit_reason']:<14}  "
                f"entry {e['entry_date']} @ Rs {e['avg_price']:.2f}  "
                f"({e['estimated_pnl_pct']:+.2f}%) {tag}"
            )
    else:
        lines.append("SELLS: none")
    lines.append("")

    if entries:
        lines.append(f"BUYS ({len(entries)}) — place at MOO {trade_date}:")
        for ent in entries:
            lines.append(
                f"  rank {ent['rank']:>2}  {ent['symbol']:<14} "
                f"qty={ent['target_qty']:>5}  ~Rs {ent['entry_price_estimate']:.2f}  "
                f"value Rs {ent['target_value_inr']:>12,.0f}  "
                f"cap Rs {ent['max_order_cap_inr']:>12,.0f}"
            )
    else:
        lines.append("BUYS: none")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    print(f"Loading portfolio: {args.portfolio}")
    portfolio = load_portfolio(args.portfolio)
    print(
        f"  positions: {len(portfolio['positions'])}, "
        f"cash: Rs {portfolio['cash_available_inr']:,.0f}"
    )

    signal_epoch = date_to_epoch_utc(args.signal_date)
    next_day_epoch = signal_epoch + SECONDS_IN_ONE_DAY

    print(f"Loading config: {args.config}")
    config = load_config(str(args.config))
    static = config["static_config"]
    prefetch_days = int(static["prefetch_days"])

    fetch_start = signal_epoch - prefetch_days * SECONDS_IN_ONE_DAY
    fetch_end = signal_epoch  # never fetch past signal_date
    print(
        f"Fetching NSE OHLCV from {epoch_to_iso(fetch_start)} → "
        f"{epoch_to_iso(signal_epoch)} (prefetch {prefetch_days}d)..."
    )
    df_data = fetch_nse_universe(fetch_start, fetch_end)

    # Hard cutoff: drop any bar past signal_epoch. Belt-and-braces — under
    # smoke testing the cr_client provider may include a same-day-ish row
    # past our requested end_epoch; under live conditions today's close
    # might also lag, so we always force exactly "data through signal_epoch".
    df_data = df_data.filter(pl.col("date_epoch") <= signal_epoch)

    # Confirm signal_date is a trading day in the data
    nb = df_data.filter(pl.col("instrument") == "NSE:NIFTYBEES").sort("date_epoch")
    nb_epochs = set(nb["date_epoch"].to_list())
    if signal_epoch not in nb_epochs:
        raise ValueError(
            f"No NIFTYBEES bar at signal_date {args.signal_date} — "
            f"check it's a NSE trading day with available data. "
            f"Latest available: {epoch_to_iso(max(nb_epochs))}"
        )

    df_with_synth = append_synthetic_next_day(df_data, signal_epoch, next_day_epoch)

    # Build context with trimmed active range for fast signal generation
    active_start = max(
        fetch_start, signal_epoch - args.lookback_days * SECONDS_IN_ONE_DAY
    )
    context = {
        **config,
        "start_epoch": active_start,
        "end_epoch": next_day_epoch + SECONDS_IN_ONE_DAY,
        "prefetch_days": prefetch_days,
        "total_exit_configs": 1,
        "slippage_rate": static.get("slippage_rate", 0.0005),
        "multiprocessing_workers": 1,
        "anomalous_drop_threshold_pct": static.get("anomalous_drop_threshold_pct", 20),
        "audit_mode": False,
    }

    print(f"Running signal generator (active range {args.lookback_days}d)...")
    sig_gen = EodBreakoutSignalGenerator()
    df_orders = sig_gen.generate_orders(context, df_with_synth)

    # Scanner + regime (independent rerun for our exit walk)
    _, df_trimmed = run_scanner(context, df_with_synth)

    entry_cfg = next(get_entry_config_iterator(context))
    exit_cfg = next(get_exit_config_iterator(context))
    sim_cfg = next(get_simulation_config_iterator(context))

    bull_epochs = compute_regime_epochs(df_trimmed, entry_cfg)

    regime_label = "BULL" if signal_epoch in bull_epochs else "BEAR"
    if not bull_epochs:
        regime_label = "no internal regime configured"

    print(f"Computing exits for {len(portfolio['positions'])} held positions...")
    exits = compute_exits(
        portfolio["positions"], df_with_synth,
        bull_epochs, entry_cfg, exit_cfg, signal_epoch, next_day_epoch,
    )
    fresh_count = sum(1 for e in exits if e["status"] == "fresh")
    overdue_count = sum(1 for e in exits if e["status"] == "overdue")
    print(f"  {fresh_count} fresh exit(s) at next-day open, {overdue_count} overdue")

    held_symbols = {p["symbol"] for p in portfolio["positions"]}
    sold_symbols = {e["symbol"] for e in exits}
    held_after = held_symbols - sold_symbols
    max_positions = int(args.max_positions or sim_cfg["max_positions"])
    slots_open = max(0, max_positions - len(held_after))

    # Account value (cash + market value of positions retained after sells).
    # Cash adjusts upward by expected sell credits at MOO. Held-position value
    # is qty × today's close (signal_epoch close).
    today_close_lookup = dict(zip(
        df_with_synth.filter(pl.col("date_epoch") == signal_epoch)["instrument"].to_list(),
        df_with_synth.filter(pl.col("date_epoch") == signal_epoch)["close"].to_list(),
    ))
    cash_post_exits = float(portfolio["cash_available_inr"])
    for e in exits:
        cash_post_exits += e["exit_price_estimate"] * e["qty"]
    held_position_value = 0.0
    for pos in portfolio["positions"]:
        if pos["symbol"] in sold_symbols:
            continue
        instrument = f"{pos['exchange']}:{pos['symbol']}"
        last_close = today_close_lookup.get(instrument)
        if last_close is None:
            print(
                f"  WARN: no close for {instrument} on signal_date — "
                f"using avg_price for account-value computation"
            )
            last_close = float(pos["avg_price"])
        held_position_value += float(last_close) * int(pos["qty"])
    account_value = cash_post_exits + held_position_value

    print(
        f"Computing entries (slots_open={slots_open}, "
        f"cash=Rs {cash_post_exits:,.0f}, account=Rs {account_value:,.0f})..."
    )
    entries = compute_entries(
        df_orders, df_with_synth, signal_epoch, next_day_epoch,
        held_after, sim_cfg, slots_open, cash_post_exits,
        account_value, max_positions, args.min_order_value,
    )
    print(f"  {len(entries)} entry candidate(s)")

    out_text = format_human(
        signal_date=args.signal_date,
        trade_date=epoch_to_iso(next_day_epoch),
        regime_label=regime_label,
        breadth_score=None,
        exits=exits,
        entries=entries,
        cash_post_exits=cash_post_exits,
        slots_open=slots_open,
        held_count=len(held_symbols),
        max_positions=max_positions,
    )
    print(out_text)

    if args.output:
        out = {
            "signal_date": args.signal_date,
            "trade_date": epoch_to_iso(next_day_epoch),
            "regime": regime_label,
            "cash_available_post_exits_inr": round(cash_post_exits, 2),
            "max_positions": max_positions,
            "slots_open": slots_open,
            "sells": exits,
            "buys": entries,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Wrote {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

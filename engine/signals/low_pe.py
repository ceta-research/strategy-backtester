"""Low P/E signal generator with quarterly rebalancing.

Classic value strategy: buy cheapest stocks by P/E with quality filters.

Screen:
  - P/E between 0 and configurable max (default 15)
  - ROE > configurable min (default 10%)
  - D/E between 0 and configurable max (default 1.0)
  - Market cap > configurable min (local currency)

Portfolio:
  - Top N stocks by lowest P/E (default 30)
  - Equal weight
  - Cash if fewer than min_stocks qualify (default 10)

Rebalance:
  - Quarterly (first trading day of Jan/Apr/Jul/Oct)
  - MOC execution: signal at close, enter at next day open

Based on Basu (1977), Fama & French (1992), LSV (1994).
Reimplemented inside strategy-backtester to fix:
  - Currency mismatch (INR returns vs USD SPY benchmark)
  - Survivorship bias (profile table = current stocks only)
  - Same-bar entry bias (now MOC execution)
"""

import io
import time
from datetime import datetime, timezone

import polars as pl

from engine.config_loader import (
    get_scanner_config_iterator,
    get_entry_config_iterator,
    get_exit_config_iterator,
)
from engine.signals.base import register_strategy
from engine.signals.factor_composite import (
    _fmp_symbol_to_instrument,
    _epoch_to_ym,
    _empty_orders,
    _build_exchange_filter,
    EXCHANGE_SUFFIX,
    US_EXCHANGES,
    SECONDS_PER_DAY,
    FILING_LAG_DAYS,
)
from lib.cr_client import CetaResearch


REBALANCE_MONTHS = {1, 4, 7, 10}  # Jan, Apr, Jul, Oct


# -----------------------------------------------------------------------
# Fundamental data fetching
# -----------------------------------------------------------------------

def fetch_pe_fundamentals(exchanges: list[str]) -> pl.DataFrame:
    """Fetch P/E, ROE, D/E, and market cap from FMP.

    Returns DataFrame with columns:
        instrument, period_epoch, pe_ratio, roe, de_ratio, market_cap
    """
    client = CetaResearch()
    exchange_filter, needs_profile = _build_exchange_filter(exchanges)

    profile_join = "LEFT JOIN fmp.profile p ON k.symbol = p.symbol" if needs_profile else ""
    if not needs_profile:
        non_us = [e for e in exchanges if e not in US_EXCHANGES]
        suffix_clauses = []
        for e in non_us:
            s = EXCHANGE_SUFFIX.get(e, "")
            if s:
                suffix_clauses.append(f"k.symbol LIKE '%{s}'")
        exchange_filter = f"({' OR '.join(suffix_clauses)})" if suffix_clauses else "1=1"

    sql = f"""
        SELECT
            k.symbol,
            CAST(k.dateEpoch AS BIGINT) as period_epoch,
            k.returnOnEquity as roe,
            k.marketCap as market_cap,
            f.priceToEarningsRatio as pe_ratio,
            f.debtToEquityRatio as de_ratio
        FROM fmp.key_metrics k
        JOIN fmp.financial_ratios f
            ON k.symbol = f.symbol AND k.period = f.period AND k.dateEpoch = f.dateEpoch
        {profile_join}
        WHERE k.period = 'FY'
          AND k.returnOnEquity IS NOT NULL
          AND f.priceToEarningsRatio IS NOT NULL
          AND {exchange_filter}
        ORDER BY k.symbol, k.dateEpoch
    """

    print("  Fetching P/E fundamental data...")
    try:
        data = client.query(
            sql, timeout=600, limit=5000000, format="parquet",
            verbose=True, memory_mb=16384, threads=6,
        )
        if not data:
            print("  WARNING: No fundamental data returned")
            return pl.DataFrame()
        df = pl.read_parquet(io.BytesIO(data))
    except Exception as e:
        print(f"  WARNING: Fundamental fetch failed: {e}")
        return pl.DataFrame()

    if df.is_empty():
        return df

    instruments = [_fmp_symbol_to_instrument(s) for s in df["symbol"].to_list()]
    df = df.with_columns(pl.Series("instrument", instruments, dtype=pl.Utf8))

    for col in ["period_epoch", "pe_ratio", "roe", "de_ratio", "market_cap"]:
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64).alias(col))

    print(f"  Fundamentals: {df.height} rows, {df['instrument'].n_unique()} companies")
    return df


def screen_at_date(
    df_fund: pl.DataFrame,
    rebalance_epoch: int,
    eligible_instruments: set,
    pe_min: float,
    pe_max: float,
    roe_min: float,
    de_max: float,
    mktcap_min: float,
    max_stocks: int,
) -> list[str]:
    """Screen stocks at a point-in-time rebalance date.

    Returns list of instruments sorted by lowest P/E (best first).
    """
    if df_fund.is_empty():
        return []

    cutoff = rebalance_epoch - FILING_LAG_DAYS * SECONDS_PER_DAY
    df = df_fund.filter(
        (pl.col("instrument").is_in(list(eligible_instruments)))
        & (pl.col("period_epoch") <= cutoff)
    )

    if df.is_empty():
        return []

    # Most recent filing per instrument
    df = (
        df.sort(["instrument", "period_epoch"])
        .group_by("instrument")
        .agg(pl.all().last())
    )

    # Apply screens
    df = df.filter(
        (pl.col("pe_ratio") > pe_min)
        & (pl.col("pe_ratio") < pe_max)
        & (pl.col("roe") > roe_min)
        & (pl.col("de_ratio") >= 0)
        & (pl.col("de_ratio") < de_max)
        & (pl.col("market_cap") > mktcap_min)
    )

    # Sort by lowest P/E, take top N
    df = df.sort("pe_ratio").head(max_stocks)
    return df["instrument"].to_list()


# -----------------------------------------------------------------------
# Signal generator
# -----------------------------------------------------------------------

class LowPeSignalGenerator:
    """Classic Low P/E value strategy with quarterly rebalancing."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Low P/E Signal Generation ---")
        t0 = time.time()

        start_epoch = context.get("start_epoch", context["static_config"]["start_epoch"])
        end_epoch = context.get("end_epoch", context["static_config"]["end_epoch"])

        # Phase 1: Scanner (liquidity filter)
        shortlist_tracker = {}
        df = df_tick_data.clone()

        for scanner_config in get_scanner_config_iterator(context):
            df_scan = df.clone()

            filter_exprs = []
            for instrument in scanner_config["instruments"]:
                if instrument["symbols"]:
                    filter_exprs.append(
                        (pl.col("exchange") == instrument["exchange"])
                        & (pl.col("symbol").is_in(instrument["symbols"]))
                    )
                else:
                    filter_exprs.append(pl.col("exchange") == instrument["exchange"])
            if filter_exprs:
                combined = filter_exprs[0]
                for expr in filter_exprs[1:]:
                    combined = combined | expr
                df_scan = df_scan.filter(combined)

            atc = scanner_config["avg_day_transaction_threshold"]
            df_scan = df_scan.with_columns(
                (pl.col("volume") * pl.col("average_price")).alias("avg_txn_turnover")
            )
            df_scan = df_scan.sort(["instrument", "date_epoch"]).with_columns(
                pl.col("avg_txn_turnover")
                .rolling_mean(window_size=atc["period"], min_samples=1)
                .over("instrument")
                .alias("avg_txn_turnover")
            )
            df_scan = df_scan.drop_nulls()
            df_scan = df_scan.filter(pl.col("close") > scanner_config["price_threshold"])
            df_scan = df_scan.filter(pl.col("avg_txn_turnover") > atc["threshold"])

            uid_series = df_scan.select(
                (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
            )["uid"]
            shortlist_tracker[scanner_config["id"]] = set(uid_series.to_list())

        # Build scanner_config_ids lookup
        df_trimmed = df.filter(pl.col("date_epoch") >= start_epoch).drop_nulls()
        df_trimmed = df_trimmed.with_columns(
            (pl.col("instrument").cast(pl.Utf8) + pl.lit(":") + pl.col("date_epoch").cast(pl.Utf8)).alias("uid")
        )
        signal_sets = {k: set(v) for k, v in shortlist_tracker.items()}
        uids = df_trimmed["uid"].to_list()
        uid_to_signals = {}
        for uid in uids:
            signals = [str(k) for k, v in signal_sets.items() if uid in v]
            uid_to_signals[uid] = ",".join(sorted(signals)) if signals else None

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df_trimmed.height} rows")

        # Phase 2: Identify quarterly rebalance dates
        all_dates = sorted(df_trimmed["date_epoch"].unique().to_list())
        rebalance_epochs = []
        seen_quarters = set()
        for epoch in all_dates:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            if dt.month in REBALANCE_MONTHS:
                qkey = f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"
                if qkey not in seen_quarters:
                    seen_quarters.add(qkey)
                    rebalance_epochs.append(epoch)

        # Map each date to next trading day for MOC entry
        next_trading_day = {}
        for i, d in enumerate(all_dates):
            if i + 1 < len(all_dates):
                next_trading_day[d] = all_dates[i + 1]

        print(f"  Rebalance dates: {len(rebalance_epochs)} quarters")
        if len(rebalance_epochs) < 2:
            print("  Not enough rebalance dates. Aborting.")
            return _empty_orders()

        # Phase 3: Fetch fundamental data
        t1 = time.time()
        exchanges = list({inst.split(":")[0] for inst in df_trimmed["instrument"].unique().to_list()})
        df_fund = fetch_pe_fundamentals(exchanges)
        fund_elapsed = round(time.time() - t1, 2)
        print(f"  Fundamentals: {fund_elapsed}s")

        if df_fund.is_empty():
            print("  No fundamental data available. Aborting.")
            return _empty_orders()

        # Phase 4: Pre-build per-instrument price lookup
        inst_daily = {}
        for inst_tuple, group in df_tick_data.group_by("instrument"):
            inst_name = inst_tuple[0]
            g = group.sort("date_epoch")
            inst_daily[inst_name] = {
                "epochs": g["date_epoch"].to_list(),
                "opens": g["open"].to_list(),
                "closes": g["close"].to_list(),
            }

        # Phase 5: Generate orders per entry x exit config
        t2 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            pe_max = entry_config.get("pe_max", 15)
            pe_min = entry_config.get("pe_min", 0)
            roe_min = entry_config.get("roe_min", 0.10)
            de_max = entry_config.get("de_max", 1.0)
            mktcap_min = entry_config.get("mktcap_min", 1e9)
            max_stocks = entry_config.get("max_stocks", 30)
            min_stocks = entry_config.get("min_stocks", 10)

            for exit_config in get_exit_config_iterator(context):
                stop_loss_pct = exit_config.get("stop_loss_pct", 0)

                for i in range(len(rebalance_epochs) - 1):
                    reb_epoch = rebalance_epochs[i]
                    next_reb_epoch = rebalance_epochs[i + 1]

                    # Get scanner-passed instruments at rebalance date
                    eligible = set()
                    for inst in df_trimmed.filter(
                        pl.col("date_epoch") == reb_epoch
                    )["instrument"].unique().to_list():
                        uid_str = f"{inst}:{reb_epoch}"
                        if uid_to_signals.get(uid_str) is not None:
                            eligible.add(inst)

                    if len(eligible) < min_stocks:
                        continue

                    # Screen stocks
                    selected = screen_at_date(
                        df_fund, reb_epoch, eligible,
                        pe_min, pe_max, roe_min, de_max, mktcap_min, max_stocks,
                    )

                    if len(selected) < min_stocks:
                        continue  # Go to cash

                    # Stagger: entry day after rebalance, exit on next rebalance
                    entry_date = next_trading_day.get(reb_epoch)
                    exit_date = next_reb_epoch

                    if entry_date is None:
                        continue

                    for inst in selected:
                        if inst not in inst_daily:
                            continue

                        daily = inst_daily[inst]
                        epochs = daily["epochs"]
                        closes = daily["closes"]
                        opens = daily["opens"]

                        # Find entry index
                        try:
                            entry_idx = epochs.index(entry_date)
                        except ValueError:
                            continue

                        entry_price = opens[entry_idx]
                        if entry_price is None or entry_price <= 0:
                            entry_price = closes[entry_idx]
                        if entry_price is None or entry_price <= 0:
                            continue

                        # Walk forward: check stop-loss or hold until exit date
                        actual_exit_epoch = exit_date
                        actual_exit_price = None

                        if stop_loss_pct > 0:
                            stop_price = entry_price * (1 - stop_loss_pct)
                        else:
                            stop_price = 0

                        for j in range(entry_idx + 1, len(epochs)):
                            if epochs[j] > exit_date:
                                break
                            c = closes[j]
                            if c is None:
                                continue
                            if stop_price > 0 and c <= stop_price:
                                actual_exit_epoch = epochs[j]
                                actual_exit_price = c
                                break
                            if epochs[j] == exit_date:
                                actual_exit_price = c
                                break

                        if actual_exit_price is None:
                            # Find closest date to exit_date
                            for j in range(len(epochs) - 1, -1, -1):
                                if epochs[j] <= exit_date and closes[j] is not None:
                                    actual_exit_epoch = epochs[j]
                                    actual_exit_price = closes[j]
                                    break

                        if actual_exit_price is None:
                            continue

                        scanner_ids = uid_to_signals.get(f"{inst}:{reb_epoch}")

                        all_order_rows.append({
                            "instrument": inst,
                            "entry_epoch": entry_date,
                            "exit_epoch": actual_exit_epoch,
                            "entry_price": float(entry_price),
                            "exit_price": float(actual_exit_price),
                            "entry_volume": 0.0,
                            "exit_volume": 0.0,
                            "scanner_config_ids": scanner_ids or "0",
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

        elapsed = round(time.time() - t2, 2)

        if not all_order_rows:
            print(f"  Signal gen: {elapsed}s, 0 orders")
            return _empty_orders()

        df_orders = pl.DataFrame(all_order_rows)
        df_orders = df_orders.select([
            "instrument", "entry_epoch", "exit_epoch",
            "entry_price", "exit_price", "entry_volume", "exit_volume",
            "scanner_config_ids", "entry_config_ids", "exit_config_ids",
        ]).sort(["instrument", "entry_epoch", "exit_epoch"])

        print(f"  Signal gen: {elapsed}s, {df_orders.height} orders")
        total = round(time.time() - t0, 2)
        print(f"  Total signal gen: {total}s")
        return df_orders


register_strategy("low_pe", LowPeSignalGenerator)

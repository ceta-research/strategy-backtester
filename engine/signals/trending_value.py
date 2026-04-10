"""Trending Value signal generator.

Quality-first stock selection with multi-year growth ranking and trailing stop.

Algorithm:
  1. Quality screen: positive earnings, reasonable debt, positive ROE
  2. Growth ranking: sort by trailing N-year revenue + earnings CAGR
  3. Buy top N stocks, hold minimum 1 year
  4. Exit via trailing stop (after min hold) or at rebalance when stock drops out
  5. Rebalance quarterly/yearly - only add new positions, keep existing winners

Based on O'Shaughnessy "What Works on Wall Street" (Trending Value).
"""

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
    fetch_fundamentals,
    _fmp_symbol_to_instrument,
    _epoch_to_ym,
    EXCHANGE_SUFFIX,
    US_EXCHANGES,
    SECONDS_PER_DAY,
    FILING_LAG_DAYS,
    _empty_orders,
    _build_exchange_filter,
)
from lib.cr_client import CetaResearch
import io


# -----------------------------------------------------------------------
# Fundamental data: quality + growth
# -----------------------------------------------------------------------

def fetch_quality_growth_data(exchanges: list[str]) -> pl.DataFrame:
    """Fetch FMP data for quality screening and growth ranking.

    Returns DataFrame with columns:
        instrument, period_epoch, fiscal_year, net_income, revenue,
        total_debt, total_assets, equity, roe
    """
    client = CetaResearch()
    exchange_filter, needs_profile = _build_exchange_filter(exchanges)

    profile_join = "JOIN fmp.profile p ON i.symbol = p.symbol" if needs_profile else ""
    if not needs_profile:
        non_us = [e for e in exchanges if e not in US_EXCHANGES]
        suffix_clauses = []
        for e in non_us:
            s = EXCHANGE_SUFFIX.get(e, "")
            if s:
                suffix_clauses.append(f"i.symbol LIKE '%{s}'")
        exchange_filter = f"({' OR '.join(suffix_clauses)})" if suffix_clauses else "1=1"

    sql = f"""
        SELECT
            i.symbol,
            CAST(i.dateEpoch AS BIGINT) as period_epoch,
            CAST(i.fiscalYear AS INTEGER) as fiscal_year,
            i.netIncome as net_income,
            i.revenue,
            b.totalDebt as total_debt,
            b.totalAssets as total_assets,
            b.totalStockholdersEquity as equity
        FROM fmp.income_statement i
        JOIN fmp.balance_sheet b
            ON i.symbol = b.symbol AND i.period = b.period AND i.dateEpoch = b.dateEpoch
        {profile_join}
        WHERE i.period = 'FY'
          AND {exchange_filter}
        ORDER BY i.symbol, i.dateEpoch
    """

    print("  Fetching quality + growth data...")
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

    for col in ["period_epoch", "net_income", "revenue", "total_debt",
                "total_assets", "equity"]:
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64).alias(col))
    if "fiscal_year" in df.columns:
        df = df.with_columns(pl.col("fiscal_year").cast(pl.Int64).alias("fiscal_year"))

    print(f"  Quality data: {df.height} rows, {df['instrument'].n_unique()} companies")
    return df


# -----------------------------------------------------------------------
# Quality screen (pass/fail)
# -----------------------------------------------------------------------

def quality_screen(
    df_fund: pl.DataFrame,
    rebalance_epoch: int,
    eligible_instruments: set,
    max_debt_to_assets: float,
    min_roe: float,
) -> set[str]:
    """Filter for strong fundamentals. Returns set of passing instruments."""
    if df_fund.is_empty():
        return set()

    cutoff = rebalance_epoch - FILING_LAG_DAYS * SECONDS_PER_DAY
    df = df_fund.filter(
        (pl.col("instrument").is_in(list(eligible_instruments)))
        & (pl.col("period_epoch") <= cutoff)
    )
    if df.is_empty():
        return set()

    # Most recent FY per instrument
    df = df.sort(["instrument", "period_epoch"]).group_by("instrument").last()

    # Apply quality filters
    df = df.filter(
        (pl.col("net_income") > 0)                                          # positive earnings
        & (pl.col("total_assets") > 0)                                      # valid
        & (pl.col("total_debt") / pl.col("total_assets") <= max_debt_to_assets)  # debt check
        & (pl.col("equity") > 0)                                            # positive equity
        & (pl.col("net_income") / pl.col("equity") >= min_roe)              # ROE check
    )

    return set(df["instrument"].to_list())


# -----------------------------------------------------------------------
# Growth ranking (CAGR over N years)
# -----------------------------------------------------------------------

def growth_rank(
    df_fund: pl.DataFrame,
    rebalance_epoch: int,
    qualified_instruments: set,
    lookback_years: int,
    growth_weights: dict,
) -> list[str]:
    """Rank instruments by trailing revenue + earnings CAGR. Returns sorted list."""
    if df_fund.is_empty() or not qualified_instruments:
        return []

    cutoff = rebalance_epoch - FILING_LAG_DAYS * SECONDS_PER_DAY
    df = df_fund.filter(
        (pl.col("instrument").is_in(list(qualified_instruments)))
        & (pl.col("period_epoch") <= cutoff)
    )
    if df.is_empty():
        return []

    w_rev = growth_weights.get("revenue", 0.5)
    w_earn = growth_weights.get("earnings", 0.5)

    scores = {}
    for inst_tuple, group in df.group_by("instrument"):
        inst = inst_tuple[0]
        g = group.sort("fiscal_year")
        years = g["fiscal_year"].to_list()
        revenues = g["revenue"].to_list()
        earnings = g["net_income"].to_list()

        if len(years) < 2:
            continue

        # Find the most recent year and N years ago
        latest_year = years[-1]
        target_year = latest_year - lookback_years

        # Find data for target year (or closest available)
        old_idx = None
        for j, y in enumerate(years):
            if y is not None and y <= target_year:
                old_idx = j
        if old_idx is None:
            old_idx = 0  # use oldest available

        actual_years = latest_year - years[old_idx]
        if actual_years < 1:
            continue

        # Revenue CAGR
        rev_cagr = 0.0
        rev_old = revenues[old_idx]
        rev_new = revenues[-1]
        if rev_old is not None and rev_new is not None and rev_old > 0 and rev_new > 0:
            rev_cagr = (rev_new / rev_old) ** (1.0 / actual_years) - 1.0

        # Earnings CAGR
        earn_cagr = 0.0
        earn_old = earnings[old_idx]
        earn_new = earnings[-1]
        if (earn_old is not None and earn_new is not None
                and earn_old > 0 and earn_new > 0):
            earn_cagr = (earn_new / earn_old) ** (1.0 / actual_years) - 1.0

        scores[inst] = w_rev * rev_cagr + w_earn * earn_cagr

    # Sort descending by growth score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [inst for inst, _ in ranked]


# -----------------------------------------------------------------------
# Rebalance date computation
# -----------------------------------------------------------------------

def compute_rebalance_epochs(all_dates: list[int], frequency: str) -> list[int]:
    """Compute rebalance dates from sorted trading day epochs."""
    rebalance = []

    if frequency == "quarterly":
        seen = set()
        for epoch in all_dates:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            q = (dt.year, (dt.month - 1) // 3)
            if q not in seen:
                seen.add(q)
                rebalance.append(epoch)
    elif frequency == "yearly":
        seen = set()
        for epoch in all_dates:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            if dt.year not in seen:
                seen.add(dt.year)
                rebalance.append(epoch)
    elif frequency == "2yearly":
        seen = set()
        for epoch in all_dates:
            dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
            bucket = dt.year // 2
            if bucket not in seen:
                seen.add(bucket)
                rebalance.append(epoch)
    else:
        # Default: monthly
        seen = set()
        for epoch in all_dates:
            ym = _epoch_to_ym(epoch)
            if ym not in seen:
                seen.add(ym)
                rebalance.append(epoch)

    return rebalance


# -----------------------------------------------------------------------
# Signal generator
# -----------------------------------------------------------------------

class TrendingValueSignalGenerator:
    """Trending Value: quality screen -> growth ranking -> hold with trailing stop."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Trending Value Signal Generation ---")
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

        # Build uid -> scanner_config_ids mapping
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
        print(f"  Scanner: {scanner_elapsed}s")

        # Phase 2: Fetch fundamental data
        t1 = time.time()
        exchanges = list({inst.split(":")[0] for inst in df_trimmed["instrument"].unique().to_list()})
        df_fund = fetch_quality_growth_data(exchanges)
        print(f"  Fundamentals: {round(time.time() - t1, 2)}s")

        # Phase 3: Build per-instrument daily price series
        inst_daily = {}
        for inst_tuple, group in df_tick_data.group_by("instrument"):
            inst_name = inst_tuple[0]
            g = group.sort("date_epoch")
            inst_daily[inst_name] = {
                "epochs": g["date_epoch"].to_list(),
                "opens": g["open"].to_list(),
                "closes": g["close"].to_list(),
            }

        # Build next_trading_day map
        all_dates = sorted(df_trimmed["date_epoch"].unique().to_list())
        next_trading_day = {}
        for idx, d in enumerate(all_dates):
            if idx + 1 < len(all_dates):
                next_trading_day[d] = all_dates[idx + 1]

        # Phase 4: Generate orders per config
        t2 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            max_dta = entry_config["max_debt_to_assets"]
            min_roe = entry_config["min_roe"]
            lookback_yrs = entry_config["growth_lookback_years"]
            g_weights = entry_config["growth_weights"]
            top_n = entry_config["top_n_stocks"]
            reb_freq = entry_config["rebalance_frequency"]

            rebalance_epochs = compute_rebalance_epochs(all_dates, reb_freq)
            print(f"  Rebalance dates ({reb_freq}): {len(rebalance_epochs)}")

            if len(rebalance_epochs) < 2:
                continue

            # Pre-compute: eligible instruments per rebalance date
            reb_eligible = {}
            for reb in rebalance_epochs:
                eligible = set()
                for inst in df_trimmed.filter(
                    pl.col("date_epoch") == reb
                )["instrument"].unique().to_list():
                    if uid_to_signals.get(f"{inst}:{reb}") is not None:
                        eligible.add(inst)
                reb_eligible[reb] = eligible

            for exit_config in get_exit_config_iterator(context):
                min_hold_days = exit_config["min_hold_days"]
                trailing_stop_pct = exit_config["trailing_stop_pct"]

                # Position tracking across rebalance periods
                held = {}  # {instrument: {entry_epoch, entry_price, highest_close}}
                orders_this_config = []

                for ri, reb_epoch in enumerate(rebalance_epochs):
                    eligible = reb_eligible.get(reb_epoch, set())
                    if len(eligible) < 3:
                        continue

                    entry_date = next_trading_day.get(reb_epoch)
                    if entry_date is None:
                        continue

                    # --- Quality screen ---
                    qualified = quality_screen(
                        df_fund, reb_epoch, eligible, max_dta, min_roe
                    )

                    # --- Growth ranking ---
                    ranked = growth_rank(
                        df_fund, reb_epoch, qualified, lookback_yrs, g_weights
                    )
                    top_n_set = set(ranked[:top_n])

                    # --- Check trailing stop between prev rebalance and now ---
                    prev_reb = rebalance_epochs[ri - 1] if ri > 0 else start_epoch
                    stopped_out = []

                    for inst, pos in list(held.items()):
                        daily = inst_daily.get(inst)
                        if not daily:
                            continue

                        for j, ep in enumerate(daily["epochs"]):
                            if ep <= prev_reb or ep > reb_epoch:
                                continue
                            c = daily["closes"][j]
                            if c is None:
                                continue

                            # Track peak
                            if c > pos["highest_close"]:
                                pos["highest_close"] = c

                            # Trailing stop: only after min hold
                            hold_days = (ep - pos["entry_epoch"]) / SECONDS_PER_DAY
                            if hold_days >= min_hold_days:
                                stop = pos["highest_close"] * (1 - trailing_stop_pct)
                                if c <= stop:
                                    stopped_out.append((inst, ep, c))
                                    break

                    for inst, exit_ep, exit_px in stopped_out:
                        pos = held.pop(inst)
                        scanner_ids = uid_to_signals.get(f"{inst}:{pos['reb_epoch']}") or "0"
                        orders_this_config.append({
                            "instrument": inst,
                            "entry_epoch": pos["entry_epoch"],
                            "exit_epoch": exit_ep,
                            "entry_price": pos["entry_price"],
                            "exit_price": exit_px,
                            "entry_volume": 0.0,
                            "exit_volume": 0.0,
                            "scanner_config_ids": scanner_ids,
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

                    # --- Rebalance exit: stocks out of top-N and past min hold ---
                    rebalance_exits = []
                    for inst, pos in list(held.items()):
                        if inst in top_n_set:
                            continue  # still in top-N, keep
                        hold_days = (reb_epoch - pos["entry_epoch"]) / SECONDS_PER_DAY
                        if hold_days >= min_hold_days:
                            rebalance_exits.append(inst)

                    for inst in rebalance_exits:
                        pos = held.pop(inst)
                        # Exit at close on rebalance day
                        exit_px = _get_close(inst_daily, inst, reb_epoch)
                        if exit_px is None:
                            continue
                        scanner_ids = uid_to_signals.get(f"{inst}:{pos['reb_epoch']}") or "0"
                        orders_this_config.append({
                            "instrument": inst,
                            "entry_epoch": pos["entry_epoch"],
                            "exit_epoch": reb_epoch,
                            "entry_price": pos["entry_price"],
                            "exit_price": exit_px,
                            "entry_volume": 0.0,
                            "exit_volume": 0.0,
                            "scanner_config_ids": scanner_ids,
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

                    # --- Entry: new positions only ---
                    for inst in ranked[:top_n]:
                        if inst in held:
                            continue  # already holding
                        if len(held) >= top_n:
                            break  # portfolio full

                        entry_px = _get_open(inst_daily, inst, entry_date)
                        if entry_px is None or entry_px <= 0:
                            entry_px = _get_close(inst_daily, inst, entry_date)
                        if entry_px is None or entry_px <= 0:
                            continue

                        held[inst] = {
                            "entry_epoch": entry_date,
                            "entry_price": entry_px,
                            "highest_close": entry_px,
                            "reb_epoch": reb_epoch,
                        }

                # --- Final: close remaining positions via trailing stop walk to end ---
                for inst, pos in list(held.items()):
                    daily = inst_daily.get(inst)
                    if not daily:
                        continue

                    last_reb = rebalance_epochs[-1] if rebalance_epochs else start_epoch
                    exit_ep = None
                    exit_px = None

                    for j, ep in enumerate(daily["epochs"]):
                        if ep <= last_reb:
                            continue
                        if ep > end_epoch:
                            break
                        c = daily["closes"][j]
                        if c is None:
                            continue

                        if c > pos["highest_close"]:
                            pos["highest_close"] = c

                        hold_days = (ep - pos["entry_epoch"]) / SECONDS_PER_DAY
                        if hold_days >= min_hold_days:
                            stop = pos["highest_close"] * (1 - trailing_stop_pct)
                            if c <= stop:
                                exit_ep = ep
                                exit_px = c
                                break

                        exit_ep = ep
                        exit_px = c  # update to latest

                    if exit_ep is None or exit_px is None:
                        # Use last available price
                        for j in range(len(daily["epochs"]) - 1, -1, -1):
                            if daily["closes"][j] is not None:
                                exit_ep = daily["epochs"][j]
                                exit_px = daily["closes"][j]
                                break

                    if exit_ep and exit_px:
                        scanner_ids = uid_to_signals.get(f"{inst}:{pos['reb_epoch']}") or "0"
                        orders_this_config.append({
                            "instrument": inst,
                            "entry_epoch": pos["entry_epoch"],
                            "exit_epoch": exit_ep,
                            "entry_price": pos["entry_price"],
                            "exit_price": exit_px,
                            "entry_volume": 0.0,
                            "exit_volume": 0.0,
                            "scanner_config_ids": scanner_ids,
                            "entry_config_ids": str(entry_config["id"]),
                            "exit_config_ids": str(exit_config["id"]),
                        })

                all_order_rows.extend(orders_this_config)
                n_orders = len(orders_this_config)
                print(f"    Config {entry_config['id']}_{exit_config['id']}: {n_orders} orders")

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

        print(f"  Signal gen: {elapsed}s, {df_orders.height} orders total")
        print(f"  Total: {round(time.time() - t0, 2)}s")
        return df_orders

    @staticmethod
    def build_entry_config(entry_cfg: dict) -> dict:
        return {
            "max_debt_to_assets": entry_cfg.get("max_debt_to_assets", [0.6]),
            "min_roe": entry_cfg.get("min_roe", [0.0]),
            "growth_lookback_years": entry_cfg.get("growth_lookback_years", [3]),
            "growth_weights": entry_cfg.get("growth_weights", [
                {"revenue": 0.5, "earnings": 0.5}
            ]),
            "top_n_stocks": entry_cfg.get("top_n_stocks", [20]),
            "rebalance_frequency": entry_cfg.get("rebalance_frequency", ["quarterly"]),
        }

    @staticmethod
    def build_exit_config(exit_cfg: dict) -> dict:
        return {
            "min_hold_days": exit_cfg.get("min_hold_days", [365]),
            "trailing_stop_pct": exit_cfg.get("trailing_stop_pct", [0.20]),
        }


def _get_open(inst_daily: dict, inst: str, epoch: int) -> float | None:
    """Look up open price for an instrument at a specific epoch."""
    daily = inst_daily.get(inst)
    if not daily or "opens" not in daily:
        return None
    try:
        idx = daily["epochs"].index(epoch)
        return daily["opens"][idx]
    except ValueError:
        return None


def _get_close(inst_daily: dict, inst: str, epoch: int) -> float | None:
    """Look up close price for an instrument at a specific epoch."""
    daily = inst_daily.get(inst)
    if not daily:
        return None
    try:
        idx = daily["epochs"].index(epoch)
        return daily["closes"][idx]
    except ValueError:
        # Find closest earlier date
        for j in range(len(daily["epochs"]) - 1, -1, -1):
            if daily["epochs"][j] <= epoch and daily["closes"][j] is not None:
                return daily["closes"][j]
        return None


register_strategy("trending_value", TrendingValueSignalGenerator)

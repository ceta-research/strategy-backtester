"""Factor Composite signal generator with monthly rebalancing.

Multi-factor stock selection combining:
- Factor 1: Price Momentum (12-1 month return, skip recent month)
- Factor 2: Gross Profitability (GP / Total Assets)
- Factor 3: Value Composite (EBITDA/EV + inverse P/B)

Portfolio construction:
- Monthly rebalance on first trading day of each month
- Cross-sectional z-score each factor, weighted combine
- Hold top N stocks by composite score
- Regime filter: equal-weight market > 200-SMA to enter new positions
- Volatility scaling: target annualized vol

Based on:
- Novy-Marx "The Other Side of Value" (gross profitability)
- Asness et al. "Value and Momentum Everywhere"
- Barroso & Santa-Clara "Momentum Has Its Moments" (vol scaling)
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
from lib.cr_client import CetaResearch

SECONDS_PER_DAY = 86400
FILING_LAG_DAYS = 45

# FMP symbol suffix per exchange (mirrors CRDataProvider)
EXCHANGE_SUFFIX = {
    "NSE": ".NS", "BSE": ".BO", "LSE": ".L", "JPX": ".T", "HKSE": ".HK",
    "XETRA": ".DE", "KSC": ".KS", "TSX": ".TO", "SHH": ".SS",
    "SHZ": ".SZ", "TAI": ".TW", "ASX": ".AX", "SAO": ".SA",
    "SES": ".SI", "JNB": ".JO",
}
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "US"}


def _fmp_symbol_to_instrument(fmp_symbol: str) -> str:
    """Convert FMP symbol (e.g. 'RELIANCE.NS') to instrument ('NSE:RELIANCE')."""
    for exchange, suffix in EXCHANGE_SUFFIX.items():
        if suffix and fmp_symbol.endswith(suffix):
            bare = fmp_symbol[: -len(suffix)]
            return f"{exchange}:{bare}"
    return f"US:{fmp_symbol}"


def _epoch_to_ym(epoch: int) -> str:
    """Epoch -> 'YYYY-MM' string for grouping by month."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return f"{dt.year}-{dt.month:02d}"


# -----------------------------------------------------------------------
# Fundamental data fetching
# -----------------------------------------------------------------------

def _build_exchange_filter(exchanges: list[str]) -> tuple[str, bool]:
    """Build SQL WHERE clause for exchange filtering.

    Returns (where_clause, needs_profile_join).
    """
    us_exchanges = [e for e in exchanges if e in US_EXCHANGES]
    non_us = [e for e in exchanges if e not in US_EXCHANGES]

    clauses = []
    needs_profile = bool(us_exchanges)

    if us_exchanges:
        actual = set()
        for e in us_exchanges:
            if e == "US":
                actual.update(["NASDAQ", "NYSE", "AMEX"])
            else:
                actual.add(e)
        ex_list = ", ".join(f"'{e}'" for e in sorted(actual))
        clauses.append(f"p.exchange IN ({ex_list})")

    if non_us:
        suffix_clauses = []
        for e in non_us:
            s = EXCHANGE_SUFFIX.get(e, "")
            if s:
                suffix_clauses.append(f"i.symbol LIKE '%{s}'")
        if suffix_clauses:
            clauses.append(f"({' OR '.join(suffix_clauses)})")

    return " AND ".join(clauses) if clauses else "1=1", needs_profile


def fetch_fundamentals(exchanges: list[str]) -> pl.DataFrame:
    """Fetch FMP fundamental data for factor scoring.

    Returns DataFrame with columns:
        instrument, period_epoch, gross_profit, ebitda, total_assets,
        enterprise_value, book_value_per_share
    """
    client = CetaResearch()
    exchange_filter, needs_profile = _build_exchange_filter(exchanges)

    profile_join = "LEFT JOIN fmp.profile p ON i.symbol = p.symbol" if needs_profile else ""
    if not needs_profile:
        exchange_filter = exchange_filter.replace("p.exchange", "1=1 -- no profile")
        # For non-US, filter is on i.symbol suffix directly
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
            i.grossProfit as gross_profit,
            i.ebitda,
            b.totalAssets as total_assets,
            k.enterpriseValue as enterprise_value,
            k.earningsYield as earnings_yield,
            f.priceToBookRatio as price_to_book
        FROM fmp.income_statement i
        JOIN fmp.balance_sheet b
            ON i.symbol = b.symbol AND i.period = b.period AND i.dateEpoch = b.dateEpoch
        LEFT JOIN fmp.key_metrics k
            ON i.symbol = k.symbol AND i.period = k.period AND i.dateEpoch = k.dateEpoch
        LEFT JOIN fmp.financial_ratios f
            ON i.symbol = f.symbol AND i.period = f.period AND i.dateEpoch = f.dateEpoch
        {profile_join}
        WHERE i.period = 'FY'
          AND i.grossProfit IS NOT NULL
          AND b.totalAssets > 0
          AND {exchange_filter}
        ORDER BY i.symbol, i.dateEpoch
    """

    print("  Fetching fundamental data...")
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

    # Map FMP symbols to instrument format
    if df.is_empty():
        return df

    instruments = [_fmp_symbol_to_instrument(s) for s in df["symbol"].to_list()]
    df = df.with_columns(pl.Series("instrument", instruments, dtype=pl.Utf8))

    # Cast numeric columns
    for col in ["period_epoch", "gross_profit", "ebitda", "total_assets",
                "enterprise_value", "earnings_yield", "price_to_book"]:
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64).alias(col))

    print(f"  Fundamentals: {df.height} rows, {df['instrument'].n_unique()} companies")
    return df


# -----------------------------------------------------------------------
# Factor computation
# -----------------------------------------------------------------------

def compute_momentum_scores(
    df_tick: pl.DataFrame,
    rebalance_epoch: int,
    lookback_days: int,
    skip_days: int,
    eligible_instruments: set,
) -> dict[str, float]:
    """Compute 12-1 month price momentum for each instrument.

    Returns {instrument: momentum_return}.
    """
    # Window: [rebalance - lookback, rebalance - skip]
    end_epoch = rebalance_epoch - skip_days * SECONDS_PER_DAY
    start_epoch = rebalance_epoch - lookback_days * SECONDS_PER_DAY

    # Get close prices at window boundaries per instrument
    df = df_tick.filter(
        pl.col("instrument").is_in(list(eligible_instruments))
    )

    # Find closest trading day to start_epoch and end_epoch per instrument
    df_start = (
        df.filter(pl.col("date_epoch") <= start_epoch)
        .sort(["instrument", "date_epoch"])
        .group_by("instrument")
        .last()
        .select(["instrument", pl.col("close").alias("close_start")])
    )

    df_end = (
        df.filter(pl.col("date_epoch") <= end_epoch)
        .sort(["instrument", "date_epoch"])
        .group_by("instrument")
        .last()
        .select(["instrument", pl.col("close").alias("close_end")])
    )

    df_mom = df_start.join(df_end, on="instrument", how="inner")
    df_mom = df_mom.filter(
        (pl.col("close_start") > 0) & (pl.col("close_end") > 0)
    ).with_columns(
        ((pl.col("close_end") / pl.col("close_start")) - 1.0).alias("momentum")
    )

    return dict(zip(
        df_mom["instrument"].to_list(),
        df_mom["momentum"].to_list(),
    ))


def compute_fundamental_scores(
    df_fund: pl.DataFrame,
    rebalance_epoch: int,
    eligible_instruments: set,
) -> dict[str, dict]:
    """Get GP/TA and EBITDA/EV for each instrument at point-in-time.

    Returns {instrument: {"gp_ta": float, "ebitda_ev": float, "inv_pb": float}}.
    """
    if df_fund.is_empty():
        return {}

    # Point-in-time: only use filings where period_epoch + filing_lag <= rebalance
    cutoff = rebalance_epoch - FILING_LAG_DAYS * SECONDS_PER_DAY
    df = df_fund.filter(
        (pl.col("instrument").is_in(list(eligible_instruments)))
        & (pl.col("period_epoch") <= cutoff)
    )

    if df.is_empty():
        return {}

    # Most recent filing per instrument
    df = (
        df.sort(["instrument", "period_epoch"])
        .group_by("instrument")
        .last()
    )

    scores = {}
    for row in df.to_dicts():
        inst = row["instrument"]
        ta = row.get("total_assets") or 0
        gp = row.get("gross_profit") or 0
        ptb = row.get("price_to_book") or 0

        ebitda = row.get("ebitda") or 0
        ev = row.get("enterprise_value") or 0

        s = {}
        s["gp_ta"] = gp / ta if ta > 0 else None
        s["ebitda_ev"] = ebitda / ev if ev > 0 and ebitda != 0 else None
        s["inv_pb"] = 1.0 / ptb if ptb > 0 else None  # inverse P/B = cheapness
        scores[inst] = s

    return scores


def zscore(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional z-score a dict of {key: value}. Skips None."""
    valid = {k: v for k, v in values.items() if v is not None}
    if len(valid) < 3:
        return {k: 0.0 for k in values}
    vals = list(valid.values())
    mean = sum(vals) / len(vals)
    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    if std < 1e-10:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std if v is not None else 0.0 for k, v in values.items()}


def compute_composite_scores(
    mom_scores: dict[str, float],
    fund_scores: dict[str, dict],
    weights: dict,
) -> dict[str, float]:
    """Combine factor z-scores into composite."""
    w_mom = weights.get("momentum", 0.4)
    w_gp = weights.get("gross_profitability", 0.3)
    w_val = weights.get("value", 0.3)

    # All instruments that have at least momentum
    all_instruments = set(mom_scores.keys())

    # Extract per-factor dicts
    gp_raw = {}
    ebitda_ev_raw = {}
    inv_pb_raw = {}
    has_fundamentals = set()  # track which instruments have fundamental data
    for inst in all_instruments:
        fs = fund_scores.get(inst, {})
        gp_raw[inst] = fs.get("gp_ta")
        ebitda_ev_raw[inst] = fs.get("ebitda_ev")
        inv_pb_raw[inst] = fs.get("inv_pb")
        if gp_raw[inst] is not None or ebitda_ev_raw[inst] is not None or inv_pb_raw[inst] is not None:
            has_fundamentals.add(inst)

    # Z-score each factor independently (including value sub-factors)
    z_mom = zscore(mom_scores)
    z_gp = zscore(gp_raw)
    z_ebitda_ev = zscore(ebitda_ev_raw)
    z_inv_pb = zscore(inv_pb_raw)

    # Value composite = average of z-scored sub-factors (not raw values)
    z_val = {}
    for inst in all_instruments:
        ev_z = z_ebitda_ev.get(inst, 0.0) if ebitda_ev_raw.get(inst) is not None else None
        pb_z = z_inv_pb.get(inst, 0.0) if inv_pb_raw.get(inst) is not None else None
        if ev_z is not None and pb_z is not None:
            z_val[inst] = (ev_z + pb_z) / 2.0
        elif ev_z is not None:
            z_val[inst] = ev_z
        elif pb_z is not None:
            z_val[inst] = pb_z
        else:
            z_val[inst] = None

    # Weighted composite (use None to distinguish missing from zero)
    composite = {}
    for inst in all_instruments:
        score = w_mom * z_mom.get(inst, 0.0)
        if inst in has_fundamentals and gp_raw.get(inst) is not None:
            score += w_gp * z_gp.get(inst, 0.0)
        else:
            # No GP data: upweight momentum
            score += w_gp * z_mom.get(inst, 0.0)
        if z_val.get(inst) is not None:
            score += w_val * z_val[inst]
        else:
            score += w_val * z_mom.get(inst, 0.0)
        composite[inst] = score

    return composite


# -----------------------------------------------------------------------
# Signal generator
# -----------------------------------------------------------------------

class FactorCompositeSignalGenerator:
    """Multi-factor composite with monthly rebalancing."""

    def generate_orders(self, context: dict, df_tick_data: pl.DataFrame) -> pl.DataFrame:
        print("\n--- Factor Composite Signal Generation ---")
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

        # Build per-date eligible instruments from scanner
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
        df_trimmed = df_trimmed.with_columns(
            pl.Series("scanner_config_ids", [uid_to_signals.get(u) for u in uids], dtype=pl.Utf8)
        )

        scanner_elapsed = round(time.time() - t0, 2)
        print(f"  Scanner: {scanner_elapsed}s, {df_trimmed.height} rows")

        # Phase 2: Identify rebalance dates (first trading day of each month)
        # Use two dates per month: exit_date (1st trading day) and entry_date (2nd trading day)
        # This ensures old positions exit BEFORE new ones enter (simulator processes chronologically)
        all_dates = sorted(df_trimmed["date_epoch"].unique().to_list())
        all_dates_set = set(all_dates)
        rebalance_epochs = []
        seen_months = set()
        for epoch in all_dates:
            ym = _epoch_to_ym(epoch)
            if ym not in seen_months:
                seen_months.add(ym)
                rebalance_epochs.append(epoch)

        # Map each rebalance date to the next trading day (for staggered entry)
        next_trading_day = {}
        for i, d in enumerate(all_dates):
            if i + 1 < len(all_dates):
                next_trading_day[d] = all_dates[i + 1]

        print(f"  Rebalance dates: {len(rebalance_epochs)} months")
        if len(rebalance_epochs) < 2:
            print("  Not enough rebalance dates. Aborting.")
            return _empty_orders()

        # Phase 3: Fetch fundamental data
        t1 = time.time()
        exchanges = list({inst.split(":")[0] for inst in df_trimmed["instrument"].unique().to_list()})
        df_fund = fetch_fundamentals(exchanges)
        fund_elapsed = round(time.time() - t1, 2)
        print(f"  Fundamentals: {fund_elapsed}s")

        # Phase 4: Compute market SMA for regime filter (equal-weight index)
        # Compute daily equal-weight market return from scanner-passed stocks
        df_mkt = df_trimmed.filter(pl.col("scanner_config_ids").is_not_null())
        df_mkt = df_mkt.sort(["instrument", "date_epoch"]).with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("instrument") - 1.0).alias("daily_ret")
        )
        df_mkt_avg = (
            df_mkt.filter(pl.col("daily_ret").is_not_null())
            .group_by("date_epoch")
            .agg(pl.col("daily_ret").mean().alias("mkt_ret"))
            .sort("date_epoch")
        )
        # Cumulative market index
        mkt_epochs = df_mkt_avg["date_epoch"].to_list()
        mkt_rets = df_mkt_avg["mkt_ret"].to_list()
        mkt_index = [100.0]
        for r in mkt_rets:
            mkt_index.append(mkt_index[-1] * (1 + (r or 0)))
        mkt_index = mkt_index[1:]  # align with epochs
        # SMA of market index
        mkt_lookup = dict(zip(mkt_epochs, mkt_index))

        # Phase 5: Generate orders per entry x exit config
        t2 = time.time()
        all_order_rows = []

        for entry_config in get_entry_config_iterator(context):
            lookback_days = entry_config["momentum_lookback_days"]
            skip_days = entry_config["momentum_skip_days"]
            weights = entry_config["factor_weights"]
            sma_period = entry_config["regime_filter_sma"]
            top_n = entry_config["top_n_stocks"]

            # Precompute per-instrument daily price series for stop-loss walk
            inst_daily = {}
            for inst_tuple, group in df_tick_data.group_by("instrument"):
                inst_name = inst_tuple[0]
                g = group.sort("date_epoch")
                inst_daily[inst_name] = {
                    "epochs": g["date_epoch"].to_list(),
                    "opens": g["open"].to_list(),
                    "closes": g["close"].to_list(),
                }

            for exit_config in get_exit_config_iterator(context):
                vol_target = exit_config["vol_target_annual"]
                vol_lookback = exit_config["vol_lookback_days"]
                stop_loss_pct = exit_config.get("stop_loss_pct", 0.15)

                # Process each pair of consecutive rebalance dates
                for i in range(len(rebalance_epochs) - 1):
                    reb_epoch = rebalance_epochs[i]
                    next_reb_epoch = rebalance_epochs[i + 1]

                    # Need enough history for momentum lookback
                    if reb_epoch - lookback_days * SECONDS_PER_DAY < df_tick_data["date_epoch"].min():
                        continue

                    # Regime filter: market index > SMA
                    if sma_period > 0:
                        # Get market index values for SMA
                        sma_epochs = [e for e in mkt_epochs if e <= reb_epoch]
                        if len(sma_epochs) >= sma_period:
                            recent_values = [mkt_lookup[e] for e in sma_epochs[-sma_period:]]
                            sma_val = sum(recent_values) / len(recent_values)
                            current_val = mkt_lookup.get(sma_epochs[-1], 0)
                            if current_val < sma_val:
                                continue  # skip: market below SMA, stay in cash

                    # Get scanner-passed instruments for this date
                    eligible = set()
                    for uid_str in [
                        f"{inst}:{reb_epoch}"
                        for inst in df_trimmed.filter(
                            pl.col("date_epoch") == reb_epoch
                        )["instrument"].unique().to_list()
                    ]:
                        if uid_to_signals.get(uid_str) is not None:
                            inst = uid_str.rsplit(":", 1)[0]
                            eligible.add(inst)

                    if len(eligible) < 5:
                        continue

                    # Compute momentum scores
                    mom = compute_momentum_scores(
                        df_tick_data, reb_epoch, lookback_days, skip_days, eligible
                    )
                    if len(mom) < 5:
                        continue

                    # Compute fundamental scores
                    fund = compute_fundamental_scores(df_fund, reb_epoch, eligible)

                    # Combine into composite
                    composite = compute_composite_scores(mom, fund, weights)

                    # Select top N
                    ranked = sorted(composite.items(), key=lambda x: x[1], reverse=True)
                    selected = [inst for inst, _ in ranked[:top_n]]

                    # Volatility scaling (optional): compute trailing portfolio vol
                    vol_scale = 1.0
                    if vol_target > 0 and i > 3:
                        # Use trailing daily returns of equal-weight market
                        vol_epochs = [e for e in mkt_epochs if e <= reb_epoch][-vol_lookback:]
                        if len(vol_epochs) >= 20:
                            vol_rets = [mkt_lookup.get(vol_epochs[j], 0) / max(mkt_lookup.get(vol_epochs[j - 1], 1), 0.01) - 1
                                        for j in range(1, len(vol_epochs))]
                            if vol_rets:
                                daily_vol = (sum(r ** 2 for r in vol_rets) / len(vol_rets)) ** 0.5
                                annual_vol = daily_vol * (252 ** 0.5)
                                if annual_vol > 0:
                                    vol_scale = min(vol_target / annual_vol, 1.5)  # cap leverage at 1.5x

                    # Get prices for selected instruments
                    n_positions = max(1, int(len(selected) * vol_scale))
                    selected = selected[:n_positions]

                    # Stagger: entry on day AFTER rebalance, exit ON rebalance
                    # This ensures old positions exit before new ones enter
                    entry_date = next_trading_day.get(reb_epoch)
                    exit_date = next_reb_epoch  # old positions exit on rebalance day

                    if entry_date is None:
                        continue

                    for inst in selected:
                        if inst not in inst_daily:
                            continue

                        daily = inst_daily[inst]
                        epochs = daily["epochs"]
                        closes = daily["closes"]

                        # Find entry index
                        try:
                            entry_idx = epochs.index(entry_date)
                        except ValueError:
                            continue

                        entry_price = daily["opens"][entry_idx]
                        if entry_price is None or entry_price <= 0:
                            entry_price = closes[entry_idx]  # fallback to close if open missing
                        if entry_price is None or entry_price <= 0:
                            continue

                        # Walk forward: check for stop-loss hit during holding period
                        stop_price = entry_price * (1 - stop_loss_pct)
                        actual_exit_epoch = exit_date
                        actual_exit_price = None

                        for j in range(entry_idx, len(epochs)):
                            if epochs[j] > exit_date:
                                break
                            c = closes[j]
                            if c is None:
                                continue
                            if c <= stop_price:
                                # Stop loss hit
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


def _empty_orders() -> pl.DataFrame:
    column_order = [
        "instrument", "entry_epoch", "exit_epoch",
        "entry_price", "exit_price", "entry_volume", "exit_volume",
        "scanner_config_ids", "entry_config_ids", "exit_config_ids",
    ]
    return pl.DataFrame(schema={
        c: pl.Utf8 if c in ("instrument", "scanner_config_ids", "entry_config_ids", "exit_config_ids")
        else pl.Float64 for c in column_order
    })


register_strategy("factor_composite", FactorCompositeSignalGenerator)

"""Order ranking functions: top_performer, top_gainer, top_average_txn, top_dipper.

Ported from ATO_Simulator/simulator/steps/simulate_step/util.py (lines 232-399)
and simulate_step_loader.py sort_orders() dispatcher.

NOTE (corrected 2026-07-05): the inner joins do NOT drop unrankable orders.
The rank frame contains a row for every (instrument, date) in the tick data —
an instrument with fewer bars than the ranking window simply carries a NULL
gain and therefore a NULL rank (polars rank propagates nulls). The join key
matches, the order survives, and the subsequent sort places NULL ranks FIRST
(polars default nulls_last=False) — so young IPOs historically received top
priority for position slots. Set simulation.ranking_nulls_last: [true] to
put unrankable orders last instead (legacy default False is byte-identical).

🚨 CORRECTION (2026-10-09) — nulls-first is a PORT REGRESSION, not a property
of the strategy. The 07-05 note above is mechanically right and its conclusion
is wrong: it treated nulls-first as the ported behaviour and the config comment
called it a "legacy accident" to be preserved for byte-identity. It is neither
legacy nor intended. The pandas original this file was ported from does the
OPPOSITE:

    ATO/ATO_Simulator/src/ATO_Simulator/simulator/steps/simulate_step/util.py
    sort_orders_by_highest_gainer(), final line:
        df_orders.sort_values(["entry_epoch", "rank"], ascending=[True, True])
    pandas sort_values defaults to na_position="last"  ->  NaN ranks sort LAST.

polars .sort() defaults to nulls_last=False -> null ranks sort FIRST. Verified
empirically on pandas 3.0.5 / polars 1.37.1 with an identical frame: pandas
returns [M3, M2, M1, IPO_A, IPO_B]; polars returns [IPO_A, IPO_B, M3, M2, M1].
(Careful when reproducing: a float NaN is NOT a polars null and sorts LAST, so
the frame must be built with None, which is what the arithmetic produces here.)

Introduced by e1abb9a "Migrate EOD pipeline from pandas to Polars" (2026-03-18).

Consequence: `ranking_nulls_last: true` is NOT an experimental arm that removes
an edge — it is the ONLY setting faithful to the extensively-tested pre-polars
engine. Every result produced with the default False gave instruments with
fewer bars than the ranking window top slot priority every single day, which
is why eod_breakout's book became ~100% recent listings. See DECISIONS #031.

Exposure differs per function, so do not generalise this to all four:
  * top_gainer      — SYSTEMATIC. ref_close = prev_close.shift(window) with no
                      min_samples fallback, so every instrument carries a null
                      gain for its first ~181 bars. This is the one that bites.
  * top_average_txn — effectively clean. rolling_mean(min_samples=1) mirrors
                      the pandas min_periods=1, so avg_txn is null only on an
                      instrument's very first bar.
  * top_dipper      — secondary. dip_pct is null only on bar 1, BUT the rank
                      join is how="left", so any unmatched order gets a null
                      rank and sorts first.
  * top_performer   — SYSTEMATIC, and it was the WORST-hidden case. Structurally
                      it is a faithful port (same calculate_daywise_instrument_score,
                      same rank->previous_rank rename, same left join, same
                      score_priority 0/1/2, same four sort keys). But it sorts on
                      ["entry_epoch","score_priority","rank","previous_rank"] and
                      BOTH `rank` (left-joined score rank: null for any instrument
                      with no score row, i.e. no prior trades in the window) and
                      `previous_rank` (the carried-over gainer rank) are nullable.
                      pandas put those NaNs last on every key; polars put them
                      first. It had no nulls_last parameter at all until
                      2026-10-09, so the 07-05 knob never covered it -- and the
                      original ~27% champion run used order_sorting_type:
                      top_performer, so the knob never touched the very config the
                      strategy was selected on.

Fixed 2026-10-09: `nulls_last` is now plumbed into ALL FOUR functions from a
single sim_config setting and defaults to True (pandas-faithful) everywhere.
The intermediate sort inside calculate_daywise_instrument_score is left as-is
deliberately: its output is only ever consumed by a key-join, so its row order
cannot affect results.
"""

import polars as pl

from engine.constants import SECONDS_IN_ONE_DAY
from engine.utils import create_epoch_wise_instrument_stats


def sort_orders(df_config_orders: pl.DataFrame, sim_config: dict, df_tick_data: pl.DataFrame, epoch_wise_instrument_stats=None) -> pl.DataFrame:
    """Dispatch to the correct ranking function based on sim_config.

    Ported from simulate_step_loader.py lines 167-186.
    """
    if df_config_orders.is_empty():
        return df_config_orders

    order_ranking_window_days = sim_config["order_ranking_window_days"]
    order_sorting_type = sim_config["order_sorting_type"]
    default_sorting_type = sim_config.get("default_sorting_type")

    # One setting drives ALL FOUR ranking functions. It was wired only into
    # top_gainer when it was added (2026-07-05), which hid the same regression in
    # the other three -- top_performer in particular, because the original
    # champion run used order_sorting_type: top_performer and so was never
    # covered by the knob at all.
    nulls_last = bool(sim_config.get("ranking_nulls_last", True))

    if order_sorting_type == "top_average_txn" or default_sorting_type == "top_average_txn":
        df_config_orders = sort_orders_by_highest_avg_txn(
            df_config_orders, df_tick_data, order_ranking_window_days, nulls_last=nulls_last
        )
    elif order_sorting_type == "top_gainer" or default_sorting_type == "top_gainer":
        df_config_orders = sort_orders_by_highest_gainer(
            df_config_orders, df_tick_data, order_ranking_window_days, nulls_last=nulls_last
        )

    # NOTE: this mirrors the original dispatcher exactly (simulate_step_loader.py
    # 167-186) -- these are `if`, not `elif` against the block above, so with
    # default_sorting_type: top_gainer and order_sorting_type: top_performer BOTH
    # run and top_performer's sort wins. The gainer `rank` column survives as
    # `previous_rank` and is still a live tie-breaker inside top_performer.
    if order_sorting_type == "top_performer":
        if epoch_wise_instrument_stats is None:
            epoch_wise_instrument_stats = create_epoch_wise_instrument_stats(df_tick_data)
        df_config_orders = sort_orders_by_top_performer(
            df_config_orders, epoch_wise_instrument_stats, order_ranking_window_days,
            nulls_last=nulls_last,
        )
    elif order_sorting_type == "top_dipper":
        df_config_orders = sort_orders_by_deepest_dip(
            df_config_orders, df_tick_data, order_ranking_window_days, nulls_last=nulls_last
        )

    return df_config_orders


def sort_orders_by_highest_avg_txn(df_orders: pl.DataFrame, df_tick_data: pl.DataFrame, order_ranking_window_days: int, nulls_last: bool = True) -> pl.DataFrame:
    """Rank orders by rolling average transaction volume.

    Uses PREV-DAY (shifted by 1) volume and average_price, matching ATO's
    util.py:251-256. This is the look-ahead-safe form for ranking order
    entries — using same-day would peek at the current bar's volume before
    the entry decision. Contrast with `engine/scanner.py` and
    `engine/utils.py::create_epoch_wise_instrument_stats`, which use
    SAME-DAY for a universe filter / liquidity cap (different purpose, so
    different convention). See docs/archive/audit-2026-04/archive/audit-2026-04/AUDIT_FINDINGS.md entry P3.1.
    """
    df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
    df_tick_data = df_tick_data.sort(["instrument", "date_epoch"])

    df_tick_data = df_tick_data.with_columns([
        pl.col("volume").shift(1).over("instrument").alias("prev_volume"),
        pl.col("average_price").shift(1).over("instrument").alias("prev_average_price"),
    ])

    df_tick_data = df_tick_data.with_columns(
        (pl.col("prev_volume") * pl.col("prev_average_price")).alias("avg_txn")
    )
    df_tick_data = df_tick_data.with_columns(
        pl.col("avg_txn")
        .rolling_mean(window_size=order_ranking_window_days, min_samples=1)
        .over("instrument")
        .alias("avg_txn")
    )

    df_tick_data = df_tick_data.with_columns(
        pl.col("avg_txn").rank(descending=True).over("date_epoch").alias("rank")
    )

    rank_df = df_tick_data.select([
        pl.col("date_epoch").alias("entry_epoch"),
        "instrument",
        "rank",
    ])

    df_orders = df_orders.join(rank_df, on=["instrument", "entry_epoch"], how="inner")
    df_orders = df_orders.sort(["entry_epoch", "rank"], nulls_last=nulls_last)
    return df_orders


def sort_orders_by_highest_gainer(df_orders: pl.DataFrame, df_tick_data: pl.DataFrame, order_ranking_window_days: int, nulls_last: bool = True) -> pl.DataFrame:
    """Rank orders by n-day return percentage.

    Instruments with fewer bars than the window get NULL gain/rank.
    nulls_last=True (default, pandas-faithful) sorts them LAST;
    nulls_last=False reproduces the pre-2026-10-09 regression, which sorted
    them FIRST and handed every young listing top slot priority.
    """
    df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
    _df = df_tick_data.select(["date_epoch", "instrument", "close"])
    _df = _df.sort(["instrument", "date_epoch"])

    _df = _df.with_columns(
        pl.col("close").shift(1).over("instrument").alias("prev_close")
    )
    _df = _df.with_columns(
        pl.col("prev_close").shift(order_ranking_window_days).over("instrument").alias("ref_close")
    )
    _df = _df.with_columns(
        ((pl.col("prev_close") - pl.col("ref_close")) / pl.col("ref_close")).alias("gain")
    )
    _df = _df.with_columns(
        pl.col("gain").rank(descending=True).over("date_epoch").alias("rank")
    )

    rank_df = _df.select([
        pl.col("date_epoch").alias("entry_epoch"),
        "instrument",
        "rank",
    ])

    df_orders = df_orders.join(rank_df, on=["instrument", "entry_epoch"], how="inner")
    df_orders = df_orders.sort(["entry_epoch", "rank"], nulls_last=nulls_last)
    return df_orders


def calculate_daywise_instrument_score(df_orders: pl.DataFrame, instrument_day_wise_close: dict, window_size: int) -> pl.DataFrame:
    """Compute per-instrument performance scores using realized + unrealized P&L.

    IMPORTANT: Internally calls remove_overlapping_orders() which is load-bearing
    for correct top_performer scoring.
    """
    entry_epochs = sorted(df_orders["entry_epoch"].unique().to_list())
    df_orders = df_orders.sort(["instrument", "entry_epoch", "exit_epoch"])

    def remove_overlapping_orders(_df_orders: pl.DataFrame) -> pl.DataFrame:
        # maintain_order=True makes this deterministic across polars versions.
        # Without it, polars may iterate groups in hash order. The per-group
        # walk sorts by (entry_epoch, exit_epoch) inside the loop, so the
        # per-instrument dedup itself is order-independent — but the final
        # pl.DataFrame(idx_to_keep) concatenation reflects the outer group
        # order, and downstream joins in sort_orders_by_top_performer pick up
        # a specific row order for scoring. Pinning the outer order gives
        # byte-identical results across runs. See archive/audit-2026-04/AUDIT_FINDINGS.md P3.3.
        idx_to_keep = []
        for instrument_tuple, group in _df_orders.group_by("instrument", maintain_order=True):
            group = group.sort(["entry_epoch", "exit_epoch"])
            current_end = None
            for row in group.iter_rows(named=True):
                if current_end is not None and row["exit_epoch"] <= current_end:
                    continue
                current_end = row["exit_epoch"]
                idx_to_keep.append(row)
        if idx_to_keep:
            return pl.DataFrame(idx_to_keep)
        return _df_orders.clear()

    df_orders = remove_overlapping_orders(df_orders)

    df_orders = df_orders.with_columns(
        ((pl.col("exit_price") - pl.col("entry_price")) * 100.0 / pl.col("entry_price")).alias("profit")
    )

    full_scoreboard = {}
    # Convert to lists for fast iteration in the scoring loop
    order_instruments = df_orders["instrument"].to_list()
    order_entry_epochs = df_orders["entry_epoch"].to_list()
    order_exit_epochs = df_orders["exit_epoch"].to_list()
    order_entry_prices = df_orders["entry_price"].to_list()
    order_profits = df_orders["profit"].to_list()

    for epoch in entry_epochs:
        window_start = epoch - window_size
        score_map = {}

        for i in range(len(order_entry_epochs)):
            oe = order_entry_epochs[i]
            if oe >= epoch or oe < window_start:
                continue

            inst = order_instruments[i]
            # Realized P&L (sold orders)
            if order_exit_epochs[i] < epoch:
                score_map[inst] = score_map.get(inst, 0) + order_profits[i]
            else:
                # Unrealized P&L (open orders)
                entry_price = order_entry_prices[i]
                if entry_price == 0:
                    continue
                prev_epoch = epoch - SECONDS_IN_ONE_DAY
                if prev_epoch in instrument_day_wise_close and inst in instrument_day_wise_close[prev_epoch]:
                    last_close_price = instrument_day_wise_close[prev_epoch][inst]["close"]
                    score_map[inst] = score_map.get(inst, 0) + (
                        (last_close_price - entry_price) * 100 / entry_price
                    )

        full_scoreboard[epoch] = score_map

    rows = []
    for epoch, instruments in full_scoreboard.items():
        for inst, score in instruments.items():
            rows.append({"entry_epoch": epoch, "instrument": inst, "score": score})

    if not rows:
        return pl.DataFrame(schema={"entry_epoch": pl.Float64, "instrument": pl.Utf8, "score": pl.Float64, "rank": pl.Float64})

    df_score = pl.DataFrame(rows)
    df_score = df_score.with_columns(
        pl.col("score").rank(descending=True).over("entry_epoch").alias("rank")
    )
    df_score = df_score.sort(["entry_epoch", "rank"])
    return df_score


def sort_orders_by_deepest_dip(df_orders: pl.DataFrame, df_tick_data: pl.DataFrame, order_ranking_window_days: int, nulls_last: bool = True) -> pl.DataFrame:
    """Rank orders by dip depth from rolling peak (deepest first).

    For dip-buy strategies, deepest dips = most mispriced = best entries.
    Matches standalone quality_dip_buy_lib.py entry ordering (L1028-1030).

    If orders contain a 'dip_pct' column (from signal generator), uses it directly.
    Otherwise, computes dip from tick data using the ranking window as peak lookback.
    """
    if "rank" in df_orders.columns:
        df_orders = df_orders.drop("rank")

    # Use pre-computed dip_pct from signal generator if available
    if "dip_pct" in df_orders.columns:
        df_orders = df_orders.with_columns(
            pl.col("dip_pct").rank(descending=True).over("entry_epoch").alias("rank")
        )
        df_orders = df_orders.sort(["entry_epoch", "rank"], nulls_last=nulls_last)
        return df_orders

    # Fallback: compute dip from tick data
    df_tick_data = df_tick_data.with_columns(pl.col("instrument").cast(pl.Utf8))
    _df = df_tick_data.select(["date_epoch", "instrument", "close"]).sort(["instrument", "date_epoch"])

    _df = _df.with_columns(
        pl.col("close").shift(1).over("instrument").alias("prev_close")
    )
    _df = _df.with_columns(
        pl.col("prev_close")
        .rolling_max(window_size=order_ranking_window_days, min_samples=1)
        .over("instrument")
        .alias("rolling_peak")
    )
    _df = _df.with_columns(
        ((pl.col("rolling_peak") - pl.col("prev_close")) / pl.col("rolling_peak")).alias("dip_pct")
    )
    _df = _df.with_columns(
        pl.col("dip_pct").rank(descending=True).over("date_epoch").alias("rank")
    )

    rank_df = _df.select([
        pl.col("date_epoch").alias("entry_epoch"),
        "instrument",
        "rank",
    ])

    df_orders = df_orders.join(rank_df, on=["instrument", "entry_epoch"], how="left")
    df_orders = df_orders.sort(["entry_epoch", "rank"], nulls_last=nulls_last)
    return df_orders


def sort_orders_by_top_performer(df_orders: pl.DataFrame, instrument_day_wise_close: dict, order_ranking_window_days: int, nulls_last: bool = True) -> pl.DataFrame:
    """Walk-forward adaptive ranking using realized + unrealized P&L."""
    df_rank = calculate_daywise_instrument_score(
        df_orders, instrument_day_wise_close, order_ranking_window_days * SECONDS_IN_ONE_DAY
    )
    if "rank" in df_orders.columns:
        df_orders = df_orders.rename({"rank": "previous_rank"})
    else:
        df_orders = df_orders.with_columns(pl.lit(None).cast(pl.Float64).alias("previous_rank"))

    df_orders = df_orders.join(df_rank, on=["instrument", "entry_epoch"], how="left")

    # Keep +ve scores first, then nans, then -ve scores
    df_orders = df_orders.with_columns(
        pl.when(pl.col("score") > 0).then(0)
        .when(pl.col("score") <= 0).then(1)
        .otherwise(2)
        .alias("score_priority")
    )
    df_orders = df_orders.sort(["entry_epoch", "score_priority", "rank", "previous_rank"], nulls_last=nulls_last)
    df_orders = df_orders.drop("score_priority")
    return df_orders

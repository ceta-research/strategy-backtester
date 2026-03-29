#!/usr/bin/env python3
"""Monthly momentum rebalance strategy.

Pure Jegadeesh-Titman cross-sectional momentum:
  - Every N trading days, rank all stocks by trailing momentum
  - Buy top K stocks (equal weight)
  - Sell positions no longer in top K
  - Always fully invested

Key differences from dip-buy strategies:
  - No waiting for dips (always invested)
  - Periodic rebalance (not event-driven)
  - Higher turnover but captures momentum premium directly

Always runs on bhavcopy with 5 bps slippage + real charges.
"""

import sys
import os
import time
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import BacktestResult, SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_regime_epochs,
    CetaResearch,
)
from engine.charges import calculate_charges

STRATEGY_NAME = "momentum_rebalance"
SLIPPAGE = 0.0005  # 5 bps per leg


def compute_momentum_scores(price_data, lookback_days):
    """Pre-compute momentum scores for all symbols at every epoch.

    Returns:
        dict[epoch, list[(symbol, momentum_score)]] sorted by momentum desc
    """
    # Build epoch → symbol → close mapping
    all_epochs = set()
    sym_epochs = {}
    for symbol, bars in price_data.items():
        sym_epochs[symbol] = {b["epoch"]: b for b in bars}
        all_epochs.update(b["epoch"] for b in bars)

    sorted_epochs = sorted(all_epochs)
    epoch_to_idx = {e: i for i, e in enumerate(sorted_epochs)}

    scores = {}
    for i, epoch in enumerate(sorted_epochs):
        if i < lookback_days:
            continue

        lookback_epoch = sorted_epochs[i - lookback_days]
        rankings = []

        for symbol, bars_dict in sym_epochs.items():
            current = bars_dict.get(epoch)
            past = bars_dict.get(lookback_epoch)
            if current and past and past["close"] > 0 and current["close"] > 0:
                mom = (current["close"] - past["close"]) / past["close"]
                rankings.append((symbol, mom))

        rankings.sort(key=lambda x: x[1], reverse=True)
        scores[epoch] = rankings

    return scores


def simulate_rebalance(
    price_data, momentum_scores, benchmark_data,
    *, capital, num_positions, rebalance_days,
    lookback_days, exchange, regime_epochs=None,
    start_epoch=None,
):
    """Simulate monthly momentum rebalance portfolio.

    At each rebalance:
    1. Rank stocks by trailing momentum
    2. Select top num_positions
    3. Sell positions not in new top-K
    4. Buy new positions to fill to num_positions
    5. Equal-weight all positions

    Args:
        price_data: dict[symbol, list[{epoch, open, close, volume}]]
        momentum_scores: from compute_momentum_scores()
        benchmark_data: dict[epoch, close]
        capital: starting capital
        num_positions: target number of positions
        rebalance_days: trading days between rebalances
        lookback_days: momentum lookback period
        exchange: for charges calculation
        regime_epochs: set of bullish epochs (None = no filter)
        start_epoch: simulation start epoch

    Returns:
        (BacktestResult, day_wise_log)
    """
    # Build epoch → symbol → price lookup
    sym_data = {}
    all_epochs = set()
    for symbol, bars in price_data.items():
        sym_data[symbol] = {b["epoch"]: b for b in bars}
        all_epochs.update(b["epoch"] for b in bars)

    sorted_epochs = sorted(all_epochs)
    if start_epoch:
        sorted_epochs = [e for e in sorted_epochs if e >= start_epoch]

    params = {
        "num_positions": num_positions,
        "rebalance_days": rebalance_days,
        "lookback_days": lookback_days,
        "regime_filter": regime_epochs is not None,
    }

    result = BacktestResult(
        STRATEGY_NAME, params, "PORTFOLIO", exchange, capital,
        slippage_bps=5, description=f"Monthly momentum rebalance (top {num_positions})")

    # State
    cash = capital
    positions = {}  # symbol -> {qty, entry_price, entry_epoch, cost_basis}
    day_wise_log = []
    days_since_rebalance = rebalance_days  # trigger on first eligible day

    for day_idx, epoch in enumerate(sorted_epochs):
        # Skip if we don't have momentum scores for this epoch
        if epoch not in momentum_scores:
            continue

        # MTM current positions
        portfolio_value = cash
        for sym, pos in positions.items():
            bar = sym_data.get(sym, {}).get(epoch)
            if bar:
                portfolio_value += pos["qty"] * bar["close"]
            else:
                portfolio_value += pos["qty"] * pos["last_price"]

        result.add_equity_point(epoch, portfolio_value)
        day_wise_log.append({
            "log_date_epoch": epoch,
            "invested_value": portfolio_value - cash,
            "margin_available": cash,
        })

        # Check if rebalance day
        days_since_rebalance += 1
        if days_since_rebalance < rebalance_days:
            # Update last prices
            for sym in positions:
                bar = sym_data.get(sym, {}).get(epoch)
                if bar:
                    positions[sym]["last_price"] = bar["close"]
            continue

        # Regime check: skip rebalance if bear market
        if regime_epochs is not None and epoch not in regime_epochs:
            # Sell all positions in bear market
            for sym in list(positions.keys()):
                bar = sym_data.get(sym, {}).get(epoch)
                if bar and bar["close"] > 0:
                    exit_price = bar["close"]
                    pos = positions[sym]
                    sell_value = pos["qty"] * exit_price
                    sell_charges = calculate_charges(
                        exchange, sell_value, segment="EQUITY",
                        trade_type="DELIVERY", which_side="SELL_SIDE")
                    sell_slippage = sell_value * SLIPPAGE
                    cash += sell_value - sell_charges - sell_slippage

                    result.add_trade(
                        pos["entry_epoch"], epoch, pos["entry_price"], exit_price,
                        pos["qty"], charges=pos.get("entry_charges", 0) + sell_charges,
                        slippage=pos.get("entry_slippage", 0) + sell_slippage,
                        symbol=sym, exit_reason="regime_exit")

            positions.clear()
            days_since_rebalance = 0
            for sym in positions:
                bar = sym_data.get(sym, {}).get(epoch)
                if bar:
                    positions[sym]["last_price"] = bar["close"]
            continue

        days_since_rebalance = 0

        # Get momentum rankings for this epoch
        rankings = momentum_scores[epoch]
        if not rankings:
            continue

        # Select top K symbols
        target_symbols = set()
        for sym, mom in rankings[:num_positions]:
            # Only include if we have price data
            bar = sym_data.get(sym, {}).get(epoch)
            if bar and bar["close"] > 0 and bar.get("volume", 0) > 0:
                target_symbols.add(sym)
            if len(target_symbols) >= num_positions:
                break

        # Sell positions not in target
        for sym in list(positions.keys()):
            if sym not in target_symbols:
                bar = sym_data.get(sym, {}).get(epoch)
                if bar and bar["close"] > 0:
                    exit_price = bar["close"]
                    pos = positions[sym]
                    sell_value = pos["qty"] * exit_price
                    sell_charges = calculate_charges(
                        exchange, sell_value, segment="EQUITY",
                        trade_type="DELIVERY", which_side="SELL_SIDE")
                    sell_slippage = sell_value * SLIPPAGE
                    cash += sell_value - sell_charges - sell_slippage

                    result.add_trade(
                        pos["entry_epoch"], epoch, pos["entry_price"], exit_price,
                        pos["qty"], charges=pos.get("entry_charges", 0) + sell_charges,
                        slippage=pos.get("entry_slippage", 0) + sell_slippage,
                        symbol=sym, exit_reason="rebalance")

                del positions[sym]

        # Determine how many new positions to buy
        current_syms = set(positions.keys())
        new_syms = target_symbols - current_syms
        slots_available = num_positions - len(positions)

        if slots_available > 0 and new_syms:
            # Equal-weight based on total portfolio value
            total_value = cash
            for sym, pos in positions.items():
                bar = sym_data.get(sym, {}).get(epoch)
                if bar:
                    total_value += pos["qty"] * bar["close"]
                else:
                    total_value += pos["qty"] * pos["last_price"]

            target_per_position = total_value / num_positions

            for sym in sorted(new_syms)[:slots_available]:
                bar = sym_data.get(sym, {}).get(epoch)
                if not bar or bar["close"] <= 0:
                    continue

                entry_price = bar["close"]
                qty = int(target_per_position / entry_price)
                if qty <= 0:
                    continue

                buy_value = qty * entry_price
                buy_charges = calculate_charges(
                    exchange, buy_value, segment="EQUITY",
                    trade_type="DELIVERY", which_side="BUY_SIDE")
                buy_slippage = buy_value * SLIPPAGE
                total_cost = buy_value + buy_charges + buy_slippage

                if cash >= total_cost:
                    cash -= total_cost
                    positions[sym] = {
                        "qty": qty,
                        "entry_price": entry_price,
                        "entry_epoch": epoch,
                        "last_price": entry_price,
                        "entry_charges": buy_charges,
                        "entry_slippage": buy_slippage,
                    }

        # Update last prices
        for sym in positions:
            bar = sym_data.get(sym, {}).get(epoch)
            if bar:
                positions[sym]["last_price"] = bar["close"]

    # Close remaining positions at end
    last_epoch = sorted_epochs[-1] if sorted_epochs else 0
    for sym, pos in positions.items():
        bar = sym_data.get(sym, {}).get(last_epoch)
        if bar:
            exit_price = bar["close"]
        else:
            exit_price = pos["last_price"]

        sell_value = pos["qty"] * exit_price
        sell_charges = calculate_charges(
            exchange, sell_value, segment="EQUITY",
            trade_type="DELIVERY", which_side="SELL_SIDE")
        sell_slippage = sell_value * SLIPPAGE
        cash += sell_value - sell_charges - sell_slippage

        result.add_trade(
            pos["entry_epoch"], last_epoch, pos["entry_price"], exit_price,
            pos["qty"], charges=pos.get("entry_charges", 0) + sell_charges,
            slippage=pos.get("entry_slippage", 0) + sell_slippage,
            symbol=sym, exit_reason="end_of_sim")

    # Set benchmark
    bm_epochs = sorted(benchmark_data.keys())
    bm_values = [benchmark_data[e] for e in bm_epochs]
    result.set_benchmark_values(bm_epochs, bm_values)
    result.compute()

    return result, day_wise_log


def main():
    exchange = "NSE"
    start_epoch = 1262304000   # 2010-01-01
    end_epoch = 1773878400     # 2026-03-19
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Monthly momentum rebalance on BHAVCOPY")
    print("=" * 80)

    print("\nFetching universe (turnover >= 70M INR)...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nComputing regime filter...")
    regime_epochs = compute_regime_epochs(benchmark, 200)

    # Pre-compute momentum scores for all lookback periods
    lookback_periods = [63, 126, 252]
    momentum_cache = {}
    for lb in lookback_periods:
        print(f"\nComputing momentum scores ({lb}d lookback)...")
        t0 = time.time()
        momentum_cache[lb] = compute_momentum_scores(price_data, lb)
        print(f"  Done in {time.time()-t0:.0f}s ({len(momentum_cache[lb])} epochs)")

    # Sweep grid
    rebalance_days_list = [21, 42, 63]  # monthly, bi-monthly, quarterly
    num_positions_list = [5, 10, 15, 20]
    regime_options = [True, False]

    param_grid = list(product(
        lookback_periods,
        rebalance_days_list,
        num_positions_list,
        regime_options,
    ))

    total = len(param_grid)
    print(f"\n{'='*80}")
    print(f"  SWEEP: {total} configs")
    print(f"  Lookback: {lookback_periods}d")
    print(f"  Rebalance: {rebalance_days_list}d")
    print(f"  Positions: {num_positions_list}")
    print(f"  Regime: on/off")
    print(f"{'='*80}")

    description = ("Monthly momentum rebalance: buy top-K momentum stocks, "
                    "rebalance periodically, always invested.")

    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    for idx, (lb, rb_days, num_pos, use_regime) in enumerate(param_grid):
        params = {
            "lookback_days": lb,
            "rebalance_days": rb_days,
            "num_positions": num_pos,
            "regime_filter": use_regime,
        }

        r, dwl = simulate_rebalance(
            price_data, momentum_cache[lb], benchmark,
            capital=capital, num_positions=num_pos,
            rebalance_days=rb_days, lookback_days=lb,
            exchange=exchange,
            regime_epochs=regime_epochs if use_regime else None,
            start_epoch=start_epoch,
        )
        sweep.add_config(params, r)
        r._day_wise_log = dwl

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        regime_flag = "R" if use_regime else "-"
        print(f"  [{idx+1}/{total}] lb={lb}d rb={rb_days}d pos={num_pos} {regime_flag} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}")

    sweep.print_leaderboard(top_n=20)
    sweep.save("result.json", top_n=20, sort_by="calmar_ratio")

    # Top by CAGR
    print(f"\n{'='*80}")
    print("  TOP 10 BY CAGR")
    print(f"{'='*80}")
    sorted_by_cagr = sweep._sorted("cagr")
    for i, (params, r) in enumerate(sorted_by_cagr[:10]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        print(f"  #{i+1} CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades} | "
              f"lb={params['lookback_days']}d rb={params['rebalance_days']}d "
              f"pos={params['num_positions']} {'R' if params['regime_filter'] else '-'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Momentum pyramid: pyramid entries + momentum-weighted sizing + fast regime.

Three capital efficiency improvements on the cascade strategy:
1. Pyramid entries: add to winning positions (max_per_instrument > 1)
   - Cascade signal fires again for held stocks = natural pyramid
   - Each pyramid is decay^N of base size (concentrate but control risk)
   - Min gap between entries for same symbol (avoid clustering)
2. Momentum-weighted sizing: allocate more to stronger acceleration signals
3. Faster regime re-entry: 50d or 100d SMA instead of 200d

Always runs on bhavcopy with 5 bps slippage.
"""

import sys
import os
import time
import math
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.backtest_result import BacktestResult, SweepResult
from scripts.quality_dip_buy_lib import (
    fetch_universe, fetch_benchmark,
    compute_quality_universe, compute_momentum_universe,
    compute_regime_epochs,
    simulate_portfolio,
    CetaResearch,
    SLIPPAGE,
)
from scripts.quality_dip_buy_fundamental import (
    fetch_fundamentals, filter_entries_by_fundamentals,
)
from scripts.momentum_breakout_v3 import compute_cascade_entries
from engine.charges import calculate_charges

STRATEGY_NAME = "momentum_pyramid"


def simulate_pyramid_portfolio(
    entries, price_data, benchmark_data, *,
    capital, max_positions, max_per_instrument=1,
    tsl_pct, max_hold_days, exchange,
    pyramid_decay=1.0,
    pyramid_min_gap=21,
    momentum_weight_factor=1.0,
    accel_norm=0.10,
    slippage=SLIPPAGE,
    regime_epochs=None, start_epoch=None,
    max_single_return=0,
    strategy_name="", description="", params=None,
):
    """Portfolio simulator with pyramid sizing and momentum weighting.

    Changes from simulate_portfolio():
    - pyramid_decay: each Nth position in same symbol gets decay^N of base size
    - pyramid_min_gap: min days between entries for same symbol
    - momentum_weight_factor: scale order_value by 1 + (factor-1) * accel/accel_norm
    - accel_norm: acceleration value that gets full weight boost
    """
    result = BacktestResult(
        strategy_name, params or {}, "PORTFOLIO", exchange, capital,
        slippage_bps=int(slippage * 10000), description=description,
    )

    sym_close = {}
    sym_open = {}
    for sym, bars in price_data.items():
        sym_close[sym] = {b["epoch"]: b["close"] for b in bars}
        sym_open[sym] = {b["epoch"]: b["open"] for b in bars}

    all_epochs = set()
    for sym, bars in price_data.items():
        for b in bars:
            if start_epoch is None or b["epoch"] >= start_epoch:
                all_epochs.add(b["epoch"])
    trading_days = sorted(all_epochs)

    if not trading_days:
        return result, []

    entries_by_epoch = {}
    for e in entries:
        ep = e["entry_epoch"]
        if ep not in entries_by_epoch:
            entries_by_epoch[ep] = []
        entries_by_epoch[ep].append(e)

    # Sort by acceleration (highest first) for priority
    for ep in entries_by_epoch:
        entries_by_epoch[ep].sort(key=lambda x: -x.get("acceleration", 0))

    cash = capital
    positions = {}
    pending_sells = []
    day_wise_log = []

    # Track last entry epoch per symbol (for pyramid_min_gap)
    last_entry_epoch = {}

    bm_epochs = []
    bm_values = []
    if benchmark_data:
        first_bm = None
        for ep in trading_days:
            if ep in benchmark_data:
                if first_bm is None:
                    first_bm = benchmark_data[ep]
                bm_epochs.append(ep)
                bm_values.append(benchmark_data[ep] / first_bm * capital)

    for epoch in trading_days:
        # ── 1. EXECUTE pending sells at today's open ──
        if pending_sells:
            keys_to_sell = list(pending_sells)
            pending_sells.clear()
            for pos_key in keys_to_sell:
                if pos_key not in positions:
                    continue
                pos = positions[pos_key]
                sym = pos["symbol"]
                open_price = sym_open.get(sym, {}).get(epoch)
                if open_price is None or open_price <= 0:
                    open_price = sym_close.get(sym, {}).get(epoch, pos["entry_price"])

                if max_single_return > 0:
                    max_exit = pos["entry_price"] * (1 + max_single_return)
                    if open_price > max_exit:
                        open_price = max_exit

                sell_val = pos["qty"] * open_price
                sell_ch = calculate_charges(exchange, sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
                sell_sl = sell_val * slippage
                cash += sell_val - sell_ch - sell_sl

                result.add_trade(
                    entry_epoch=pos["entry_epoch"],
                    exit_epoch=epoch,
                    entry_price=pos["entry_price"],
                    exit_price=open_price,
                    quantity=pos["qty"],
                    side="LONG",
                    charges=pos["buy_charges"] + sell_ch,
                    slippage=pos["buy_slippage"] + sell_sl,
                    symbol=pos["symbol"],
                    exit_reason=pos.get("exit_reason", ""),
                )
                del positions[pos_key]

        # ── 2. EXECUTE entries at today's open ──
        day_entries = entries_by_epoch.get(epoch, [])
        for entry in day_entries:
            if len(positions) >= max_positions:
                break

            sym = entry["symbol"]

            if any(positions.get(k, {}).get("symbol") == sym for k in pending_sells):
                continue

            sym_positions = sum(1 for p in positions.values() if p["symbol"] == sym)
            if sym_positions >= max_per_instrument:
                continue

            # Pyramid min gap check
            if sym_positions > 0 and pyramid_min_gap > 0:
                last_ep = last_entry_epoch.get(sym, 0)
                days_since = (epoch - last_ep) / 86400
                if days_since < pyramid_min_gap:
                    continue

            # Regime filter
            if regime_epochs and entry["epoch"] not in regime_epochs:
                continue

            entry_price = entry["entry_price"]
            if entry_price <= 0:
                continue

            # Position sizing
            invested_value = sum(
                pos["qty"] * sym_close.get(pos["symbol"], {}).get(epoch, pos["entry_price"])
                for pos in positions.values()
            )
            account_value = cash + invested_value
            order_value = account_value / max_positions

            # Pyramid decay: Nth position in same symbol gets decay^N of base
            if sym_positions > 0 and pyramid_decay < 1.0:
                order_value *= pyramid_decay ** sym_positions

            # Momentum weighting: scale by acceleration strength
            if momentum_weight_factor > 1.0 and "acceleration" in entry:
                accel = entry["acceleration"]
                weight = 1 + (momentum_weight_factor - 1) * min(accel / accel_norm, 1.0)
                order_value *= weight

            qty = int(order_value / entry_price)
            if qty <= 0:
                continue

            cost = qty * entry_price
            buy_ch = calculate_charges(exchange, cost, "EQUITY", "DELIVERY", "BUY_SIDE")
            buy_sl = cost * slippage
            total_cost = cost + buy_ch + buy_sl

            if total_cost > cash:
                continue

            cash -= total_cost
            pos_key = f"{sym}_{epoch}_{len(positions)}"
            positions[pos_key] = {
                "symbol": sym,
                "qty": qty,
                "entry_price": entry_price,
                "entry_epoch": epoch,
                "peak_price": entry["peak_price"],
                "trail_high": entry_price,
                "reached_peak": False,
                "buy_charges": buy_ch,
                "buy_slippage": buy_sl,
                "tsl_pct": tsl_pct,
            }
            last_entry_epoch[sym] = epoch

        # ── 3. MTM all positions at today's close ──
        invested_value = 0
        for pos in positions.values():
            close = sym_close.get(pos["symbol"], {}).get(epoch)
            if close:
                invested_value += pos["qty"] * close
            else:
                invested_value += pos["qty"] * pos["entry_price"]

        result.add_equity_point(epoch, cash + invested_value)
        day_wise_log.append({
            "epoch": epoch,
            "margin_available": cash,
            "invested_value": invested_value,
        })

        # ── 4. CHECK EXIT SIGNALS at today's close ──
        for key, pos in positions.items():
            if key in pending_sells:
                continue
            sym = pos["symbol"]
            close = sym_close.get(sym, {}).get(epoch)
            if close is None:
                continue

            if close > pos["trail_high"]:
                pos["trail_high"] = close

            hold_days = (epoch - pos["entry_epoch"]) / 86400
            exit_reason = ""
            pos_tsl = pos["tsl_pct"]

            if close >= pos["peak_price"]:
                pos["reached_peak"] = True
            if pos["reached_peak"] and close <= pos["trail_high"] * (1 - pos_tsl / 100.0):
                exit_reason = "tsl"
            if max_hold_days > 0 and hold_days >= max_hold_days:
                exit_reason = "max_hold"

            if exit_reason:
                pos["exit_reason"] = exit_reason
                pending_sells.append(key)

    # Close remaining positions
    last_epoch = trading_days[-1] if trading_days else 0
    for key, pos in list(positions.items()):
        close = sym_close.get(pos["symbol"], {}).get(last_epoch, pos["entry_price"])
        sell_val = pos["qty"] * close
        sell_ch = calculate_charges(exchange, sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
        sell_sl = sell_val * slippage
        cash += sell_val - sell_ch - sell_sl
        result.add_trade(
            entry_epoch=pos["entry_epoch"],
            exit_epoch=last_epoch,
            entry_price=pos["entry_price"],
            exit_price=close,
            quantity=pos["qty"],
            side="LONG",
            charges=pos["buy_charges"] + sell_ch,
            slippage=pos["buy_slippage"] + sell_sl,
            symbol=pos["symbol"],
            exit_reason="end_of_sim",
        )

    if bm_epochs and bm_values:
        result.set_benchmark_values(bm_epochs, bm_values)

    return result, day_wise_log


def main():
    exchange = "NSE"
    start_epoch = 1262304000
    end_epoch = 1773878400
    benchmark_sym = "NIFTYBEES"
    capital = 10_000_000
    source = "bhavcopy"

    cr = CetaResearch()

    print("=" * 80)
    print(f"  {STRATEGY_NAME}: Pyramid + Momentum Weighting + Fast Regime")
    print("=" * 80)

    # Fetch data
    print("\nFetching universe (turnover >= 70M INR)...")
    t0 = time.time()
    price_data = fetch_universe(cr, exchange, start_epoch, end_epoch,
                                source=source, turnover_threshold=70_000_000)
    print(f"  Got {len(price_data)} symbols in {time.time()-t0:.0f}s")

    print(f"\nFetching {benchmark_sym} benchmark ({source})...")
    benchmark = fetch_benchmark(cr, benchmark_sym, exchange, start_epoch, end_epoch,
                                warmup_days=250, source=source)

    print("\nComputing regime filters...")
    regime_200 = compute_regime_epochs(benchmark, 200)
    regime_100 = compute_regime_epochs(benchmark, 100)
    regime_50 = compute_regime_epochs(benchmark, 50)
    regime_map = {200: regime_200, 100: regime_100, 50: regime_50}

    description = "Pyramid: cascade + pyramiding + momentum weighting + fast regime"
    sweep = SweepResult(STRATEGY_NAME, "PORTFOLIO", exchange, capital,
                        slippage_bps=5, description=description)

    # Pre-compute cascade entries for both fast lookbacks
    print("\nComputing cascade entries...")
    cascade_cache = {}
    for fast_lb in [21, 42]:
        for accel in [0.02, 0.05]:
            for min_mom in [0.15, 0.20]:
                key = (fast_lb, 126, accel, min_mom)
                cascade_cache[key] = compute_cascade_entries(
                    price_data, fast_lb, 126, accel, min_mom, start_epoch=start_epoch)

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 1: Pyramid entries (add to winners)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 1: Pyramid entries (max_per_instrument > 1)")
    print(f"{'='*80}")

    # Best cascade configs from v3: f=42d/accel>2%/mom>20% and f=21d/accel>5%/mom>15%
    p1_cascade_keys = [
        (42, 126, 0.02, 0.20),  # best Calmar (0.82)
        (21, 126, 0.05, 0.15),  # best CAGR (25.8%)
        (42, 126, 0.02, 0.15),  # wider net
    ]

    p1_grid = list(product(
        p1_cascade_keys,
        [1, 2, 3],        # max_per_instrument
        [7, 10],           # max_positions
        [12, 15],          # tsl_pct
        [1.0, 0.5],       # pyramid_decay
        [21],              # pyramid_min_gap (days)
    ))
    # Remove decay variants for max_per_instrument=1 (no effect)
    p1_grid = [(ck, mpi, pos, tsl, pd, pg) for ck, mpi, pos, tsl, pd, pg in p1_grid
               if not (mpi == 1 and pd != 1.0)]

    total_p1 = len(p1_grid)
    print(f"  {total_p1} configs")

    for idx, (ck, mpi, pos, tsl, pd, pg) in enumerate(p1_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "pyramid",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "max_per_instrument": mpi,
            "max_positions": pos, "tsl_pct": tsl,
            "pyramid_decay": pd, "pyramid_min_gap": pg,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=mpi,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            pyramid_decay=pd, pyramid_min_gap=pg,
            regime_epochs=regime_200, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        flag = " ***" if cagr > 25 else (" **" if calmar > 0.82 else "")
        print(f"  [{idx+1}/{total_p1}] f={ck[0]}d a>{ck[2]*100:.0f}% mpi={mpi} "
              f"pos={pos} tsl={tsl}% pd={pd} | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 2: Momentum-weighted sizing
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 2: Momentum-weighted position sizing")
    print(f"{'='*80}")

    p2_cascade_keys = [
        (42, 126, 0.02, 0.20),
        (21, 126, 0.05, 0.15),
    ]

    p2_grid = list(product(
        p2_cascade_keys,
        [1.5, 2.0],       # momentum_weight_factor
        [0.05, 0.10],     # accel_norm
        [7, 10],           # max_positions
        [12, 15],          # tsl_pct
    ))

    total_p2 = len(p2_grid)
    print(f"  {total_p2} configs")

    for idx, (ck, wf, an, pos, tsl) in enumerate(p2_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "momentum_weighted",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "momentum_weight_factor": wf, "accel_norm": an,
            "max_positions": pos, "tsl_pct": tsl,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=1,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            momentum_weight_factor=wf, accel_norm=an,
            regime_epochs=regime_200, start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        flag = " ***" if cagr > 25 else (" **" if calmar > 0.82 else "")
        print(f"  [{idx+1}/{total_p2}] f={ck[0]}d wf={wf} an={an} pos={pos} "
              f"tsl={tsl}% | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 3: Faster regime re-entry (50d/100d SMA)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 3: Faster regime filter (50d/100d SMA)")
    print(f"{'='*80}")

    p3_cascade_keys = [
        (42, 126, 0.02, 0.20),
        (21, 126, 0.05, 0.15),
    ]

    p3_grid = list(product(
        p3_cascade_keys,
        [50, 100],         # regime SMA
        [7, 10],           # max_positions
        [12, 15],          # tsl_pct
    ))

    total_p3 = len(p3_grid)
    print(f"  {total_p3} configs")

    for idx, (ck, sma, pos, tsl) in enumerate(p3_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "fast_regime",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "regime_sma": sma,
            "max_positions": pos, "tsl_pct": tsl,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=1,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            regime_epochs=regime_map[sma], start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        flag = " ***" if cagr > 25 else (" **" if calmar > 0.82 else "")
        print(f"  [{idx+1}/{total_p3}] f={ck[0]}d sma={sma}d pos={pos} "
              f"tsl={tsl}% | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # PHASE 4: Combined best (pyramid + weighting + fast regime)
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  PHASE 4: Combined (pyramid + momentum weight + fast regime)")
    print(f"{'='*80}")

    p4_grid = list(product(
        [(42, 126, 0.02, 0.20), (21, 126, 0.05, 0.15)],  # cascade
        [2, 3],            # max_per_instrument
        [1.5, 2.0],       # momentum_weight_factor
        [50, 100],         # regime SMA
        [10, 12],          # max_positions (more slots for pyramids)
        [12, 15],          # tsl_pct
    ))

    total_p4 = len(p4_grid)
    print(f"  {total_p4} configs")

    for idx, (ck, mpi, wf, sma, pos, tsl) in enumerate(p4_grid):
        entries = cascade_cache.get(ck)
        if not entries or len(entries) < 10:
            continue

        params = {
            "type": "combined",
            "fast_lb": ck[0], "accel": ck[2], "min_mom": ck[3],
            "max_per_instrument": mpi,
            "momentum_weight_factor": wf,
            "regime_sma": sma,
            "max_positions": pos, "tsl_pct": tsl,
            "pyramid_decay": 0.5, "pyramid_min_gap": 21,
            "max_hold_days": 504,
        }

        r, dwl = simulate_pyramid_portfolio(
            entries, price_data, benchmark,
            capital=capital, max_positions=pos, max_per_instrument=mpi,
            tsl_pct=tsl, max_hold_days=504, exchange=exchange,
            pyramid_decay=0.5, pyramid_min_gap=21,
            momentum_weight_factor=wf, accel_norm=0.10,
            regime_epochs=regime_map[sma], start_epoch=start_epoch,
            strategy_name=STRATEGY_NAME, description=description,
            params=params,
        )
        sweep.add_config(params, r)

        s = r.to_dict().get("summary", {})
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        flag = " ***" if cagr > 25 else (" **" if calmar > 0.82 else "")
        print(f"  [{idx+1}/{total_p4}] f={ck[0]}d mpi={mpi} wf={wf} "
              f"sma={sma}d pos={pos} tsl={tsl}% | "
              f"CAGR={cagr:+.1f}% MDD={mdd:.1f}% Cal={calmar:.2f} T={trades}{flag}")

    # ══════════════════════════════════════════════════════════════════════
    # RESULTS
    # ══════════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  FINAL LEADERBOARD (by Calmar)")
    print(f"{'='*80}")
    sweep.print_leaderboard(top_n=20)

    print(f"\n{'='*80}")
    print("  TOP 15 BY CAGR")
    print(f"{'='*80}")
    sorted_by_cagr = sweep._sorted("cagr")
    for i, (params, r) in enumerate(sorted_by_cagr[:15]):
        s = r.to_dict()["summary"]
        cagr = (s.get("cagr") or 0) * 100
        mdd = (s.get("max_drawdown") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        print(f"  #{i+1} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} T={trades} | "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"mpi={params.get('max_per_instrument', 1)} "
              f"wf={params.get('momentum_weight_factor', 1.0)}")

    print(f"\n{'='*80}")
    print("  TOP 10 WITH MDD < 30% (safe zone)")
    print(f"{'='*80}")
    sorted_configs = sweep._sorted("calmar_ratio")
    count = 0
    for params, r in sorted_configs:
        s = r.to_dict()["summary"]
        mdd = (s.get("max_drawdown") or 0) * 100
        if mdd < -30:
            continue
        cagr = (s.get("cagr") or 0) * 100
        calmar = s.get("calmar_ratio") or 0
        trades = s.get("total_trades") or 0
        typ = params.get("type", "?")
        count += 1
        print(f"  #{count} [{typ}] CAGR={cagr:+.1f}% MDD={mdd:.1f}% "
              f"Cal={calmar:.2f} T={trades} | "
              f"pos={params['max_positions']} tsl={params['tsl_pct']}% "
              f"mpi={params.get('max_per_instrument', 1)} "
              f"wf={params.get('momentum_weight_factor', 1.0)} "
              f"sma={params.get('regime_sma', 200)}")
        if count >= 10:
            break

    sweep.save("result.json", top_n=30, sort_by="calmar_ratio")

    if sweep.configs:
        _, best = sorted_configs[0]
        print(f"\n{'='*80}")
        print("  BEST BY CALMAR (detailed)")
        print(f"{'='*80}")
        best.print_summary()


if __name__ == "__main__":
    main()

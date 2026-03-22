#!/usr/bin/env python3
"""CORRECTED pairs strategy — fixing all biases found in audit.

Fixes applied:
  1. NEXT-DAY ENTRY: Signal at day i close → execute at day i+1 close (no same-bar)
  2. CURRENCY NORMALIZATION: Convert all indices to USD before computing ratios
  3. NO SWEEP: Fixed "reasonable" config, no cherry-picking
  4. TRAIN/TEST SPLIT: Train 2005-2015, test 2016-2026
  5. ETF COMPARISON: Also run on actual US-listed ETFs (INDA, FXI, EWU, EWG, EWJ)
  6. SLIPPAGE: Add 0.05% slippage per trade (conservative for liquid ETFs)
"""

import sys
import os
import math
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "/session" not in sys.path and os.path.isdir("/session/lib"):
    sys.path.insert(0, "/session")

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.backtest_result import BacktestResult, SweepResult


# ── Config (FIXED — no sweep) ───────────────────────────────────────────────

Z_LOOKBACK = 20        # short lookback for z-score
Z_ENTRY = 1.0          # enter when z deviates by > 1 std
Z_EXIT = -0.5          # exit when z returns past -0.5 (let winner run)
SLIPPAGE_PCT = 0.0005  # 0.05% per trade (5 bps) for ETF execution
CAPITAL = 10_000_000


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_close(cr, symbol, source, start_epoch, end_epoch):
    """Fetch close prices as {epoch: close} dict."""
    warmup = start_epoch - 500 * 86400
    if source == "nse":
        sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}' AND date_epoch >= {warmup}
                    AND date_epoch <= {end_epoch} ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
                  WHERE symbol = '{symbol}' AND dateEpoch >= {warmup}
                    AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""

    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            print(f"    Retry {attempt+1} for {symbol}: {e}")
            if attempt < 2:
                time.sleep(5)
            else:
                return {}
    if not results:
        return {}
    return {int(r["date_epoch"]): float(r["close"])
            for r in results if float(r.get("close") or 0) > 0}


def fetch_fx(cr, pair, start_epoch, end_epoch):
    """Fetch FX rate from FMP. pair like 'USDINR', returns {epoch: rate}."""
    warmup = start_epoch - 500 * 86400
    sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
              WHERE symbol = '{pair}' AND dateEpoch >= {warmup}
                AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""
    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
            else:
                return {}
    if not results:
        return {}
    return {int(r["date_epoch"]): float(r["close"])
            for r in results if float(r.get("close") or 0) > 0}


def convert_to_usd(prices, fx_rates, invert=False):
    """Convert local-currency prices to USD using FX rates.

    If invert=True, fx_rate is LOCAL/USD (e.g., USDINR=85 means 1USD=85INR),
    so USD_price = local_price / fx_rate.
    If invert=False, fx_rate is USD/LOCAL, so USD_price = local_price * fx_rate.
    """
    result = {}
    # Forward-fill FX rates for days with equity data but no FX data
    sorted_fx = sorted(fx_rates.items())
    fx_filled = {}
    last_rate = None
    for epoch in sorted(set(list(prices.keys()) + [e for e, _ in sorted_fx])):
        if epoch in fx_rates:
            last_rate = fx_rates[epoch]
        if last_rate is not None:
            fx_filled[epoch] = last_rate

    for epoch, price in prices.items():
        rate = fx_filled.get(epoch)
        if rate and rate > 0:
            if invert:
                result[epoch] = price / rate
            else:
                result[epoch] = price * rate
    return result


def align(datasets, start_epoch):
    epoch_sets = [set(d.keys()) for d in datasets]
    common = sorted(epoch_sets[0].intersection(*epoch_sets[1:]))
    return [e for e in common if e >= start_epoch]


# ── Indicators ───────────────────────────────────────────────────────────────

def compute_z(values, lookback):
    """Z-score: window EXCLUDES current bar (no look-ahead)."""
    z = [0.0] * len(values)
    for i in range(lookback, len(values)):
        w = values[i - lookback:i]  # past only
        m = sum(w) / len(w)
        v = sum((x - m) ** 2 for x in w) / len(w)
        s = math.sqrt(v) if v > 0 else 1e-9
        z[i] = (values[i] - m) / s
    return z


# ── Simulation (CORRECTED) ──────────────────────────────────────────────────

def sim_pair_corrected(epochs, closes_a, closes_b, z_lookback=Z_LOOKBACK,
                        z_entry=Z_ENTRY, z_exit=Z_EXIT, capital=CAPITAL,
                        slippage=SLIPPAGE_PCT,
                        instrument="SPY vs EWJ", exchange="US",
                        params_dict=None):
    """Corrected pair simulation.

    KEY FIX: Signal on bar i → execute on bar i+1 close.
    Returns a BacktestResult.
    """
    if params_dict is None:
        params_dict = {"z_lookback": z_lookback, "z_entry": z_entry, "z_exit": z_exit}

    result = BacktestResult(
        "pairs_nextday", params_dict, instrument, exchange, capital,
        slippage_bps=int(slippage * 10000),
    )

    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    buy_ch = 0.0
    buy_slip = 0.0

    # Pending signal: computed on bar i, executed on bar i+1
    pending_entry = None   # ("a"/"b") or None
    pending_exit = False

    for i in range(z_lookback, n):
        z = z_scores[i]

        # ── EXECUTE pending signals from YESTERDAY ──
        if pending_exit and position:
            side, qty, ep, ei = position
            exit_price = closes_a[i] if side == "a" else closes_b[i]
            sell_val = qty * exit_price
            ch = calculate_charges("US", sell_val, "EQUITY", "DELIVERY", "SELL_SIDE")
            slip = sell_val * slippage
            cash += sell_val - ch - slip
            result.add_trade(
                entry_epoch=epochs[ei], exit_epoch=epochs[i],
                entry_price=ep, exit_price=exit_price,
                quantity=qty, side="LONG",
                charges=buy_ch + ch, slippage=buy_slip + slip,
            )
            buy_ch = 0.0
            buy_slip = 0.0
            position = None
            pending_exit = False

        if pending_entry and position is None:
            buy_side = pending_entry
            buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
            invest = cash * 0.95  # keep 5% reserve
            if buy_price > 0 and invest > 0:
                qty = int(invest / buy_price)
                if qty > 0:
                    actual_cost = qty * buy_price
                    ch = calculate_charges("US", actual_cost, "EQUITY", "DELIVERY", "BUY_SIDE")
                    slip = actual_cost * slippage
                    if actual_cost + ch + slip <= cash:
                        position = (buy_side, qty, buy_price, i)
                        cash -= actual_cost + ch + slip
                        buy_ch = ch
                        buy_slip = slip
            pending_entry = None

        # ── GENERATE signals for TOMORROW ──
        # Exit signal
        if position:
            side, qty, ep, ei = position
            if side == "a" and z >= z_exit:
                pending_exit = True
            elif side == "b" and z <= -z_exit:
                pending_exit = True

        # Entry signal (only if no position and no pending exit)
        if position is None and not pending_exit:
            if z < -z_entry:
                pending_entry = "a"
            elif z > z_entry:
                pending_entry = "b"

        # Portfolio value
        if position:
            side, qty, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            result.add_equity_point(epochs[i], cash + qty * cp)
        else:
            result.add_equity_point(epochs[i], cash)

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_summary(s):
    """Format a BacktestResult summary dict into one line."""
    if not s:
        return "NO DATA"
    cagr = (s.get("cagr") or 0) * 100
    mdd = (s.get("max_drawdown") or 0) * 100
    calmar = s.get("calmar_ratio") or 0
    parts = [f"CAGR={cagr:>+6.1f}%", f"MDD={mdd:>6.1f}%", f"Calmar={calmar:.2f}"]
    sh = s.get("sharpe_ratio")
    if sh is not None:
        parts.append(f"Sharpe={sh:.2f}")
    so = s.get("sortino_ratio")
    if so is not None:
        parts.append(f"Sortino={so:.2f}")
    return ", ".join(parts)


def _fmt_bh(values, epochs):
    """Format buy-and-hold stats into one line."""
    if len(values) < 2:
        return "NO DATA"
    sv, ev = values[0], values[-1]
    yrs = (epochs[-1] - epochs[0]) / (365.25 * 86400)
    if yrs <= 0 or ev <= 0 or sv <= 0:
        return "NO DATA"
    cagr = ((ev / sv) ** (1 / yrs) - 1) * 100
    peak = sv
    mdd = 0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    return f"CAGR={cagr:>+6.1f}%, MDD={mdd:>6.1f}%"


# ── Main ─────────────────────────────────────────────────────────────────────

# Index pairs and their FX symbols for USD conversion
PAIRS = [
    # (sym_a, sym_b, label, src_a, src_b, fx_b, fx_invert)
    # fx_b = FX symbol to convert sym_b to USD. fx_invert=True means fx is LOCAL/USD
    ("^GSPC", "^NSEI", "US vs India", "fmp", "fmp", "USDINR", True),
    ("^GSPC", "^HSI", "US vs HK", "fmp", "fmp", None, False),  # HKD pegged to USD
    ("^GSPC", "^FTSE", "US vs UK", "fmp", "fmp", "GBPUSD", False),
    ("^GSPC", "^GDAXI", "US vs Germany", "fmp", "fmp", "EURUSD", False),
    ("^GSPC", "^N225", "US vs Japan", "fmp", "fmp", "USDJPY", True),
    ("^GSPC", "^BVSP", "US vs Brazil", "fmp", "fmp", "USDBRL", True),
]

# US-listed ETFs (actual tradeable instruments)
ETF_PAIRS = [
    ("SPY", "INDA", "SPY vs INDA (India)", "fmp", "fmp"),
    ("SPY", "FXI", "SPY vs FXI (China/HK)", "fmp", "fmp"),
    ("SPY", "EWU", "SPY vs EWU (UK)", "fmp", "fmp"),
    ("SPY", "EWG", "SPY vs EWG (Germany)", "fmp", "fmp"),
    ("SPY", "EWJ", "SPY vs EWJ (Japan)", "fmp", "fmp"),
    ("SPY", "EWZ", "SPY vs EWZ (Brazil)", "fmp", "fmp"),
]


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19
    split_epoch = 1451606400   # 2016-01-01 (train/test boundary)

    cr = CetaResearch()

    # ══════════════════════════════════════════════════════════════════════════
    #  TEST 1: Index pairs WITHOUT currency adjustment (reproducing original)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("  TEST 1: INDEX PAIRS — NO CURRENCY FIX (same-bar vs next-day)")
    print("  Shows impact of next-day entry fix alone")
    print("=" * 80)

    all_data = {}
    needed_syms = set()
    for sa, sb, _, srca, srcb, _, _ in PAIRS:
        needed_syms.add((sa, srca))
        needed_syms.add((sb, srcb))
    for sym, src in sorted(needed_syms):
        print(f"  Fetching {sym}...")
        data = fetch_close(cr, sym, src, start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            print(f"    {len(data)} days")

    for sa, sb, label, _, _, _, _ in PAIRS:
        if sa not in all_data or sb not in all_data:
            continue
        common = align([all_data[sa], all_data[sb]], start_epoch)
        if len(common) < 300:
            continue
        ca = [all_data[sa][e] for e in common]
        cb = [all_data[sb][e] for e in common]
        print(f"\n{'─'*70}")
        print(f"  {label}: {len(common)} days (NO currency adjustment)")

        # Full period
        result = sim_pair_corrected(common, ca, cb, instrument=label, exchange="US")
        result.compute()
        result.print_summary()

        # Train/test split
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        test_start = train_end

        if train_end > Z_LOOKBACK + 50 and test_start < len(common) - 50:
            # Train
            result_train = sim_pair_corrected(
                common[:train_end], ca[:train_end], cb[:train_end],
                instrument=f"{label} TRAIN", exchange="US")
            result_train.compute()
            print(f"\n  TRAIN (2005-2015): {_fmt_summary(result_train.to_dict()['summary'])}")

            # Test (fresh capital)
            result_test = sim_pair_corrected(
                common[test_start:], ca[test_start:], cb[test_start:],
                instrument=f"{label} TEST", exchange="US")
            result_test.compute()
            print(f"  TEST  (2016-2026): {_fmt_summary(result_test.to_dict()['summary'])}")

    # ══════════════════════════════════════════════════════════════════════════
    #  TEST 2: Index pairs WITH currency adjustment
    # ══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("  TEST 2: INDEX PAIRS — WITH CURRENCY NORMALIZATION TO USD")
    print("  Both sides in USD before computing ratio")
    print("=" * 80)

    # Fetch FX data
    fx_symbols = {"USDINR", "GBPUSD", "EURUSD", "USDJPY", "USDBRL"}
    fx_data = {}
    for fx in fx_symbols:
        print(f"  Fetching FX: {fx}...")
        data = fetch_close(cr, fx, "fmp", start_epoch, end_epoch)
        if data:
            fx_data[fx] = data
            print(f"    {len(data)} days")
        else:
            print(f"    FAILED — will skip pairs needing this")

    for sa, sb, label, _, _, fx_sym, fx_invert in PAIRS:
        if sa not in all_data or sb not in all_data:
            continue

        # A side (^GSPC) is already in USD
        prices_a_usd = all_data[sa]

        # B side needs currency conversion
        if fx_sym and fx_sym in fx_data:
            prices_b_usd = convert_to_usd(all_data[sb], fx_data[fx_sym], invert=fx_invert)
        elif fx_sym is None:
            # HKD is pegged, treat as USD
            prices_b_usd = all_data[sb]
        else:
            print(f"  Skipping {label} — no FX data for {fx_sym}")
            continue

        common = align([prices_a_usd, prices_b_usd], start_epoch)
        if len(common) < 300:
            print(f"  {label}: only {len(common)} common days after FX alignment, skipping")
            continue

        ca = [prices_a_usd[e] for e in common]
        cb = [prices_b_usd[e] for e in common]
        print(f"\n{'─'*70}")
        print(f"  {label}: {len(common)} days (USD-normalized)")

        result = sim_pair_corrected(common, ca, cb,
                                    instrument=f"{label} (USD-adj)", exchange="US")
        result.compute()
        result.print_summary()

        # Train/test
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        if train_end > Z_LOOKBACK + 50 and train_end < len(common) - 50:
            result_test = sim_pair_corrected(
                common[train_end:], ca[train_end:], cb[train_end:],
                instrument=f"{label} TEST (USD-adj)", exchange="US")
            result_test.compute()
            print(f"\n  TEST (2016-2026, USD-adjusted, OOS): {_fmt_summary(result_test.to_dict()['summary'])}")

    # ══════════════════════════════════════════════════════════════════════════
    #  TEST 3: Actual US-listed ETFs (the tradeable version)
    # ══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("  TEST 3: ACTUAL US-LISTED ETFs (tradeable instruments)")
    print("  SPY vs INDA/FXI/EWU/EWG/EWJ/EWZ — all in USD, no FX issues")
    print("=" * 80)

    etf_data = {}
    etf_needed = set()
    for sa, sb, _, srca, srcb in ETF_PAIRS:
        etf_needed.add((sa, srca))
        etf_needed.add((sb, srcb))
    for sym, src in sorted(etf_needed):
        if sym in all_data:
            etf_data[sym] = all_data[sym]
            continue
        print(f"  Fetching ETF: {sym}...")
        data = fetch_close(cr, sym, src, start_epoch, end_epoch)
        if data:
            etf_data[sym] = data
            print(f"    {len(data)} days (from {datetime.fromtimestamp(min(data.keys()), tz=timezone.utc).date()})")
        else:
            print(f"    NO DATA for {sym}")

    sweep = SweepResult("pairs_nextday_etf", "ETF Pairs", "US", CAPITAL,
                        slippage_bps=int(SLIPPAGE_PCT * 10000),
                        description="Corrected next-day pairs on US-listed ETFs")

    for sa, sb, label, _, _ in ETF_PAIRS:
        if sa not in etf_data or sb not in etf_data:
            print(f"  Skipping {label} — missing data")
            continue
        common = align([etf_data[sa], etf_data[sb]], start_epoch)
        if len(common) < 200:
            print(f"  {label}: only {len(common)} days, skipping")
            continue

        ca = [etf_data[sa][e] for e in common]
        cb = [etf_data[sb][e] for e in common]
        start_date = datetime.fromtimestamp(common[0], tz=timezone.utc).date()
        print(f"\n{'─'*70}")
        print(f"  {label}: {len(common)} days (from {start_date})")

        params = {"pair": f"{sa}/{sb}", "z_lookback": Z_LOOKBACK,
                  "z_entry": Z_ENTRY, "z_exit": Z_EXIT}
        result = sim_pair_corrected(common, ca, cb,
                                    instrument=label, exchange="US",
                                    params_dict=params)
        result.compute()
        result.print_summary()

        # B&H comparison for both sides
        bh_a = [ca[i] / ca[0] * CAPITAL for i in range(len(ca))]
        bh_b = [cb[i] / cb[0] * CAPITAL for i in range(len(cb))]
        print(f"  B&H {sa}: {_fmt_bh(bh_a, common)}")
        print(f"  B&H {sb}: {_fmt_bh(bh_b, common)}")

        sweep.add_config(params, result)

    # Print ETF sweep leaderboard and save
    if sweep.configs:
        sweep.print_leaderboard()
        sweep.save("result.json")

    # ══════════════════════════════════════════════════════════════════════════
    #  TEST 4: Multi-pair ETF portfolio
    # ══════════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("  TEST 4: MULTI-PAIR ETF PORTFOLIO (diversified, real instruments)")
    print("=" * 80)

    # Find ETF pairs with enough data
    valid_etf_pairs = []
    for sa, sb, label, _, _ in ETF_PAIRS:
        if sa in etf_data and sb in etf_data:
            valid_etf_pairs.append((sa, sb, label))

    if len(valid_etf_pairs) >= 2:
        # Find common epochs across ALL ETFs
        all_etf_syms = set()
        for sa, sb, _ in valid_etf_pairs:
            all_etf_syms.add(sa)
            all_etf_syms.add(sb)
        common = align([etf_data[s] for s in all_etf_syms if s in etf_data], start_epoch)

        if len(common) >= 200:
            n_pairs = len(valid_etf_pairs)
            per_pair = CAPITAL / n_pairs
            start_date = datetime.fromtimestamp(common[0], tz=timezone.utc).date()
            print(f"\n  {n_pairs} ETF pairs, {len(common)} common days (from {start_date})")

            # Run each pair independently, collect sub-results
            sub_results = []
            for sa, sb, label in valid_etf_pairs:
                ca = [etf_data[sa][e] for e in common]
                cb = [etf_data[sb][e] for e in common]
                sub = sim_pair_corrected(common, ca, cb, capital=per_pair,
                                         instrument=label, exchange="US")
                sub_results.append(sub)
                n_trades = len(sub.trades) if hasattr(sub, 'trades') else 0
                print(f"    {label}: computing...")

            # Build combined equity curve
            combined_result = BacktestResult(
                "pairs_nextday", {"n_pairs": n_pairs, "per_pair_capital": per_pair},
                f"Multi({n_pairs} pairs)", "US", CAPITAL,
                slippage_bps=int(SLIPPAGE_PCT * 10000),
            )

            # Sum equity curves across sub-results
            # All sub-results have same epoch sequence (same common array)
            min_len = min(len(sr.equity_curve) for sr in sub_results)
            for j in range(min_len):
                epoch = sub_results[0].equity_curve[j][0]
                combined_val = sum(sr.equity_curve[j][1] for sr in sub_results)
                combined_result.add_equity_point(epoch, combined_val)

            # Copy trades from all sub-results
            for sr in sub_results:
                for t in sr.trades:
                    combined_result.add_trade(
                        entry_epoch=t["entry_epoch"], exit_epoch=t["exit_epoch"],
                        entry_price=t["entry_price"], exit_price=t["exit_price"],
                        quantity=t["quantity"], side=t["side"],
                        charges=t["charges"], slippage=t["slippage"],
                    )

            # SPY B&H as benchmark
            spy_bh_vals = [etf_data["SPY"][e] / etf_data["SPY"][common[0]] * CAPITAL
                           for e in common if e in etf_data["SPY"]]
            spy_bh_epochs = [e for e in common if e in etf_data["SPY"]]
            if len(spy_bh_vals) == len(combined_result.equity_curve):
                combined_result.set_benchmark_values(spy_bh_epochs, spy_bh_vals)

            combined_result.compute()
            combined_result.print_summary()

            # SPY B&H one-liner
            print(f"\n  B&H SPY: {_fmt_bh(spy_bh_vals, spy_bh_epochs)}")
    else:
        print("  Not enough ETF pairs with data")


if __name__ == "__main__":
    main()

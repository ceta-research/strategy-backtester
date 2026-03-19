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

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.metrics import compute_metrics as compute_full_metrics


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
                        slippage=SLIPPAGE_PCT):
    """Corrected pair simulation.

    KEY FIX: Signal on bar i → execute on bar i+1 close.
    """
    n = len(epochs)
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_scores = compute_z(ratios, z_lookback)

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    trades = 0
    total_charges = 0.0
    total_slippage = 0.0
    values = []

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
            total_charges += ch
            total_slippage += slip
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
                        total_charges += ch
                        total_slippage += slip
                        trades += 1
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
            values.append(cash + qty * cp)
        else:
            values.append(cash)

    return values, trades, total_charges, total_slippage


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_stats(values, epochs_sub):
    if len(values) < 2:
        return None
    sv, ev = values[0], values[-1]
    yrs = (epochs_sub[-1] - epochs_sub[0]) / (365.25 * 86400)
    if yrs <= 0 or ev <= 0 or sv <= 0:
        return None
    tr = ev / sv
    cagr = (tr ** (1 / yrs) - 1) * 100
    peak = sv
    mdd = 0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    calmar = cagr / abs(mdd) if mdd != 0 else 0

    dr = [(values[i] - values[i-1]) / values[i-1] if values[i-1] > 0 else 0
          for i in range(1, len(values))]
    sharpe = sortino = vol = None
    if len(dr) >= 20:
        full = compute_full_metrics(dr, [0.0]*len(dr), periods_per_year=252)
        p = full["portfolio"]
        sharpe = p.get("sharpe_ratio")
        sortino = p.get("sortino_ratio")
        vol = p.get("annualized_volatility")

    yearly = {}
    for j, v in enumerate(values):
        yr = datetime.fromtimestamp(epochs_sub[j], tz=timezone.utc).year
        if yr not in yearly:
            yearly[yr] = {"first": v, "last": v, "peak": v, "trough": v}
        yearly[yr]["last"] = v
        yearly[yr]["peak"] = max(yearly[yr]["peak"], v)
        yearly[yr]["trough"] = min(yearly[yr]["trough"], v)

    return {"cagr": cagr, "mdd": mdd, "calmar": calmar, "tr": tr, "yrs": yrs,
            "sharpe": sharpe, "sortino": sortino, "vol": vol, "yearly": yearly}


def print_yearwise(s, label, trades=0, charges=0, slippage=0):
    if not s:
        print(f"  {label}: No data")
        return
    sh = f", Sharpe={s['sharpe']:.2f}" if s.get('sharpe') else ""
    so = f", Sortino={s['sortino']:.2f}" if s.get('sortino') else ""
    print(f"\n  {label}")
    print(f"  CAGR={s['cagr']:.1f}%, MaxDD={s['mdd']:.1f}%, Calmar={s['calmar']:.2f}{sh}{so}")
    print(f"  Growth={s['tr']:.1f}x over {s['yrs']:.1f} years, {trades} trades")
    print(f"  Charges={charges:,.0f}, Slippage={slippage:,.0f} "
          f"(total cost: {(charges+slippage)/CAPITAL*100:.2f}% of starting capital)")
    print(f"\n  {'Year':<6} {'Return':>9} {'MaxDD':>9} {'EndValue':>14}")
    print(f"  {'-'*42}")
    for yr in sorted(s["yearly"].keys()):
        y = s["yearly"][yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
        print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}% {y['last']:>14,.0f}")


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
        vals, tr, ch, slip = sim_pair_corrected(common, ca, cb)
        ep = common[Z_LOOKBACK:]
        s = compute_stats(vals, ep)
        print_yearwise(s, f"{label} — FULL (2005-2026, next-day entry, no FX fix)",
                       tr, ch, slip)

        # Train/test split
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        test_start = train_end

        if train_end > Z_LOOKBACK + 50 and test_start < len(common) - 50:
            # Train
            vals_train, tr_t, ch_t, sl_t = sim_pair_corrected(
                common[:train_end], ca[:train_end], cb[:train_end])
            s_train = compute_stats(vals_train, common[Z_LOOKBACK:train_end])
            print_yearwise(s_train, f"  TRAIN (2005-2015)", tr_t, ch_t, sl_t)

            # Test (fresh capital)
            vals_test, tr_te, ch_te, sl_te = sim_pair_corrected(
                common[test_start:], ca[test_start:], cb[test_start:])
            s_test = compute_stats(vals_test, common[test_start + Z_LOOKBACK:])
            print_yearwise(s_test, f"  TEST  (2016-2026, out-of-sample)", tr_te, ch_te, sl_te)

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

        vals, tr, ch, slip = sim_pair_corrected(common, ca, cb)
        ep = common[Z_LOOKBACK:]
        s = compute_stats(vals, ep)
        print_yearwise(s, f"{label} — FULL (USD-adjusted, next-day entry)", tr, ch, slip)

        # Train/test
        train_end = next((i for i, e in enumerate(common) if e >= split_epoch), len(common))
        if train_end > Z_LOOKBACK + 50 and train_end < len(common) - 50:
            vals_test, tr_te, ch_te, sl_te = sim_pair_corrected(
                common[train_end:], ca[train_end:], cb[train_end:])
            s_test = compute_stats(vals_test, common[train_end + Z_LOOKBACK:])
            print_yearwise(s_test, f"  TEST (2016-2026, USD-adjusted, out-of-sample)",
                           tr_te, ch_te, sl_te)

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

        vals, tr, ch, slip = sim_pair_corrected(common, ca, cb)
        ep = common[Z_LOOKBACK:]
        s = compute_stats(vals, ep)
        print_yearwise(s, f"{label} — FULL (real ETFs, next-day entry)", tr, ch, slip)

        # B&H comparison for both sides
        bh_a = [ca[i] / ca[0] * CAPITAL for i in range(len(ca))]
        bh_b = [cb[i] / cb[0] * CAPITAL for i in range(len(cb))]
        s_bh_a = compute_stats(bh_a, common)
        s_bh_b = compute_stats(bh_b, common)
        if s_bh_a:
            print(f"  B&H {sa}: CAGR={s_bh_a['cagr']:.1f}%, MDD={s_bh_a['mdd']:.1f}%")
        if s_bh_b:
            print(f"  B&H {sb}: CAGR={s_bh_b['cagr']:.1f}%, MDD={s_bh_b['mdd']:.1f}%")

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

            # Run each pair independently, sum values
            all_pair_vals = []
            total_trades = total_ch = total_sl = 0
            for sa, sb, label in valid_etf_pairs:
                ca = [etf_data[sa][e] for e in common]
                cb = [etf_data[sb][e] for e in common]
                vals, tr, ch, slip = sim_pair_corrected(common, ca, cb, capital=per_pair)
                all_pair_vals.append(vals)
                total_trades += tr
                total_ch += ch
                total_sl += slip
                print(f"    {label}: {tr} trades")

            min_len = min(len(pv) for pv in all_pair_vals)
            combined = [sum(pv[i] for pv in all_pair_vals) for i in range(min_len)]
            ep = common[Z_LOOKBACK:]
            s = compute_stats(combined, ep)
            print_yearwise(s, f"MULTI-PAIR ETF PORTFOLIO ({n_pairs} pairs, real instruments)",
                           total_trades, total_ch, total_sl)

            # B&H SPY comparison
            spy_bh = [etf_data["SPY"][e] / etf_data["SPY"][common[0]] * CAPITAL
                      for e in common if e in etf_data["SPY"]]
            s_spy = compute_stats(spy_bh, common)
            if s_spy:
                print(f"\n  B&H SPY: CAGR={s_spy['cagr']:.1f}%, MDD={s_spy['mdd']:.1f}%, "
                      f"Calmar={s_spy['calmar']:.2f}")
    else:
        print("  Not enough ETF pairs with data")


if __name__ == "__main__":
    main()

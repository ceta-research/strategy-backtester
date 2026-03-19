#!/usr/bin/env python3
"""Target: 20%+ CAGR after realistic charges.

Key insight: Trade all pairs via US-listed ETFs (SPY, INDA, FXI, EWU, EWG, EWJ).
US charges are ~0.003% vs 0.2%+ for NSE. Signals from index data, execution on ETFs.

Alpha sources layered:
  1. Multi-pair portfolio (5-6 pairs running simultaneously)
  2. Conviction sizing (deeper z-score = more capital deployed)
  3. Multi-timeframe z-score (short + long confirmation)
  4. Crash safety (speed-of-decline pause + vol scaling)
  5. Momentum overlay (only trade if 6-month momentum agrees)
  6. Regime filter (VIX-proxy via realized vol)
"""

import sys
import os
import math
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cr_client import CetaResearch
from engine.charges import calculate_charges
from lib.metrics import compute_metrics as compute_full_metrics


# ── Charge Models ────────────────────────────────────────────────────────────

def us_etf_charges(order_value, side="BUY_SIDE"):
    """US-listed ETF charges (near zero). SEC fee + FINRA TAF on sell only."""
    return calculate_charges("US", order_value, segment="EQUITY",
                             trade_type="DELIVERY", which_side=side)


def local_exchange_charges(exchange, order_value, side="BUY_SIDE"):
    """Local exchange charges (for comparison)."""
    return calculate_charges(exchange, order_value, segment="EQUITY",
                             trade_type="DELIVERY", which_side=side)


# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_data(cr, symbol, source, start_epoch, end_epoch):
    warmup_epoch = start_epoch - 500 * 86400
    if source == "nse":
        sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}' AND date_epoch >= {warmup_epoch}
                    AND date_epoch <= {end_epoch} ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
                  WHERE symbol = '{symbol}' AND dateEpoch >= {warmup_epoch}
                    AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""

    for attempt in range(3):
        try:
            results = cr.query(sql, timeout=180, limit=10000000, memory_mb=8192, threads=4)
            break
        except Exception as e:
            print(f"  Attempt {attempt+1} for {symbol}: {e}")
            if attempt < 2:
                time.sleep(5)
            else:
                return {}
    if not results:
        return {}
    data = {}
    for r in results:
        c = float(r.get("close") or 0)
        if c > 0:
            data[int(r["date_epoch"])] = c
    return data


def align_all(datasets, start_epoch):
    epoch_sets = [set(d.keys()) for d in datasets]
    common = sorted(epoch_sets[0].intersection(*epoch_sets[1:]))
    return [e for e in common if e >= start_epoch]


# ── Indicators ───────────────────────────────────────────────────────────────

def compute_z(values, lookback):
    z = [0.0] * len(values)
    for i in range(lookback, len(values)):
        w = values[i - lookback:i]
        m = sum(w) / len(w)
        v = sum((x - m) ** 2 for x in w) / len(w)
        s = math.sqrt(v) if v > 0 else 1e-9
        z[i] = (values[i] - m) / s
    return z


def compute_momentum(closes, period):
    m = [0.0] * len(closes)
    for i in range(period, len(closes)):
        if closes[i - period] > 0:
            m[i] = (closes[i] - closes[i - period]) / closes[i - period] * 100
    return m


def compute_realized_vol(closes, window):
    vol = [0.0] * len(closes)
    for i in range(1, len(closes)):
        start = max(1, i - window + 1)
        rets = []
        for j in range(start, i + 1):
            if closes[j - 1] > 0:
                rets.append(math.log(closes[j] / closes[j - 1]))
        if len(rets) >= 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            vol[i] = math.sqrt(var) * math.sqrt(252)
    return vol


# ── Single Pair Simulator ────────────────────────────────────────────────────

@dataclass
class PairCfg:
    # Z-score params
    z_lookback_short: int = 20
    z_lookback_long: int = 60
    z_entry: float = 0.75
    z_exit: float = -0.5
    use_dual_z: bool = False          # require both short + long z to agree

    # Conviction sizing: deploy more at deeper z-scores
    use_conviction: bool = False
    conviction_tiers: list = field(default_factory=lambda: [
        (1.0, 0.5),   # z >= 1.0 → 50% of allocated capital
        (1.5, 0.75),  # z >= 1.5 → 75%
        (2.0, 1.0),   # z >= 2.0 → 100%
    ])

    # Safety
    use_crash_pause: bool = False
    crash_daily_pct: float = 3.0
    crash_pause_days: int = 3

    use_vol_scaling: bool = False
    vol_window: int = 20
    vol_avg_window: int = 60
    vol_high_ratio: float = 2.0       # halve position when vol > 2x avg

    # Momentum
    use_momentum: bool = False
    momentum_period: int = 126

    # Hold
    max_hold_days: int = 0

    # Execution
    charge_model: str = "us_etf"      # "us_etf" or "local"
    local_exchange_a: str = "US"
    local_exchange_b: str = "US"


def sim_pair(epochs, closes_a, closes_b, cfg: PairCfg, capital=10_000_000):
    """Simulate a single pair with all alpha layers."""
    n = len(epochs)

    # Ratio and z-scores
    ratios = [closes_a[i] / closes_b[i] if closes_b[i] > 0 else 1.0 for i in range(n)]
    z_short = compute_z(ratios, cfg.z_lookback_short)
    z_long = compute_z(ratios, cfg.z_lookback_long) if cfg.use_dual_z else z_short

    # Optional indicators
    mom_a = compute_momentum(closes_a, cfg.momentum_period) if cfg.use_momentum else None
    mom_b = compute_momentum(closes_b, cfg.momentum_period) if cfg.use_momentum else None
    vol_a = compute_realized_vol(closes_a, cfg.vol_window) if cfg.use_vol_scaling else None
    vol_b = compute_realized_vol(closes_b, cfg.vol_window) if cfg.use_vol_scaling else None
    avg_vol_a = compute_realized_vol(closes_a, cfg.vol_avg_window) if cfg.use_vol_scaling else None
    avg_vol_b = compute_realized_vol(closes_b, cfg.vol_avg_window) if cfg.use_vol_scaling else None

    cash = capital
    position = None  # ("a"/"b", qty, entry_price, entry_idx)
    trades = 0
    total_charges = 0.0
    crash_until = -1

    warmup = max(cfg.z_lookback_short, cfg.z_lookback_long,
                 cfg.momentum_period if cfg.use_momentum else 0,
                 cfg.vol_avg_window if cfg.use_vol_scaling else 0)
    values = []

    for i in range(warmup, n):
        zs = z_short[i]
        zl = z_long[i]

        # Crash pause
        if cfg.use_crash_pause and i > 0:
            ret_a = (closes_a[i] - closes_a[i-1]) / closes_a[i-1] * 100 if closes_a[i-1] > 0 else 0
            ret_b = (closes_b[i] - closes_b[i-1]) / closes_b[i-1] * 100 if closes_b[i-1] > 0 else 0
            if ret_a < -cfg.crash_daily_pct or ret_b < -cfg.crash_daily_pct:
                crash_until = max(crash_until, i + cfg.crash_pause_days)

        # ── EXIT ──
        if position:
            side, qty, ep, ei = position
            cp = closes_a[i] if side == "a" else closes_b[i]

            should_exit = False
            if side == "a" and zs >= cfg.z_exit:
                should_exit = True
            elif side == "b" and zs <= -cfg.z_exit:
                should_exit = True
            if cfg.max_hold_days > 0 and (epochs[i] - epochs[ei]) / 86400 >= cfg.max_hold_days:
                should_exit = True

            if should_exit:
                sell_val = qty * cp
                if cfg.charge_model == "us_etf":
                    ch = us_etf_charges(sell_val, "SELL_SIDE")
                else:
                    exch = cfg.local_exchange_a if side == "a" else cfg.local_exchange_b
                    ch = local_exchange_charges(exch, sell_val, "SELL_SIDE")
                cash += sell_val - ch
                total_charges += ch
                position = None

        # ── ENTRY ──
        if position is None and i > crash_until:
            buy_side = None
            if zs < -cfg.z_entry:
                buy_side = "a"
            elif zs > cfg.z_entry:
                buy_side = "b"

            # Dual z-score confirmation
            if buy_side and cfg.use_dual_z:
                if buy_side == "a" and zl >= -cfg.z_entry * 0.5:
                    buy_side = None  # long-term doesn't confirm
                elif buy_side == "b" and zl <= cfg.z_entry * 0.5:
                    buy_side = None

            # Momentum filter
            if buy_side and cfg.use_momentum:
                m = mom_a[i] if buy_side == "a" else mom_b[i]
                if m < 0:
                    buy_side = None

            # Vol scaling
            vol_mult = 1.0
            if buy_side and cfg.use_vol_scaling:
                v = vol_a[i] if buy_side == "a" else vol_b[i]
                av = avg_vol_a[i] if buy_side == "a" else avg_vol_b[i]
                if av > 0 and v > 0:
                    ratio = v / av
                    if ratio > cfg.vol_high_ratio:
                        vol_mult = 0.5

            # Conviction sizing
            if buy_side:
                invest_pct = 1.0
                if cfg.use_conviction:
                    abs_z = abs(zs)
                    invest_pct = 0.3  # minimum
                    for z_thresh, pct in cfg.conviction_tiers:
                        if abs_z >= z_thresh:
                            invest_pct = pct
                invest_pct *= vol_mult

                buy_price = closes_a[i] if buy_side == "a" else closes_b[i]
                invest = cash * invest_pct
                if buy_price > 0 and invest > 0:
                    qty = int(invest / buy_price)
                    if qty <= 0:
                        qty = 1
                    actual_cost = qty * buy_price
                    if cfg.charge_model == "us_etf":
                        ch = us_etf_charges(actual_cost, "BUY_SIDE")
                    else:
                        exch = cfg.local_exchange_a if buy_side == "a" else cfg.local_exchange_b
                        ch = local_exchange_charges(exch, actual_cost, "BUY_SIDE")

                    if actual_cost + ch <= cash:
                        position = (buy_side, qty, buy_price, i)
                        cash -= actual_cost + ch
                        total_charges += ch
                        trades += 1

        # Value
        if position:
            side, qty, _, _ = position
            cp = closes_a[i] if side == "a" else closes_b[i]
            values.append(cash + qty * cp)
        else:
            values.append(cash)

    return values, trades, total_charges, warmup


# ── Multi-Pair Portfolio ─────────────────────────────────────────────────────

def sim_multi_pair(pair_specs, common_epochs, cfg: PairCfg, capital=10_000_000):
    """Run N pairs simultaneously with equal capital split.

    pair_specs: list of (closes_a, closes_b, label)
    """
    n_pairs = len(pair_specs)
    per_pair = capital / n_pairs

    pair_values = []
    total_trades = 0
    total_charges = 0.0
    warmup = 0

    for ca, cb, label in pair_specs:
        vals, tr, ch, wu = sim_pair(common_epochs, ca, cb, cfg, capital=per_pair)
        pair_values.append(vals)
        total_trades += tr
        total_charges += ch
        warmup = max(warmup, wu)

    # Sum all pair values
    min_len = min(len(pv) for pv in pair_values)
    combined = [sum(pv[i] for pv in pair_values) for i in range(min_len)]

    return combined, total_trades, total_charges, warmup


# ── Metrics ──────────────────────────────────────────────────────────────────

def stats(values, epochs_sub):
    if len(values) < 2 or len(epochs_sub) < 2:
        return None
    sv, ev = values[0], values[-1]
    yrs = (epochs_sub[-1] - epochs_sub[0]) / (365.25 * 86400)
    if yrs <= 0 or ev <= 0 or sv <= 0:
        return None
    tr = ev / sv
    cagr = (tr ** (1 / yrs) - 1) * 100
    peak = values[0]
    mdd = 0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak * 100)
    calmar = cagr / abs(mdd) if mdd != 0 else 0

    # Daily returns for Sharpe/Sortino
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


def print_top(results, label, n=15):
    print(f"\n{'='*140}")
    print(f"  {label} — TOP {min(n, len(results))}")
    print(f"{'='*140}")
    print(f"  {'#':<3} {'Zs':>3} {'Zl':>3} {'Zi':>5} {'Zo':>5} {'Dual':>4} "
          f"{'Conv':>4} {'Crsh':>4} {'Vol':>3} {'Mom':>3} {'MaxH':>4} "
          f"{'CAGR':>7} {'MDD':>7} {'Calm':>6} {'Shrp':>5} {'Sort':>5} {'Grwth':>6} "
          f"{'Chg%':>5} {'Tr':>4}")
    print(f"  {'-'*120}")

    for i, r in enumerate(results[:n]):
        c = r["cfg"]
        ch_pct = r["charges"] / 10_000_000 * 100
        shrp = f"{r.get('sharpe') or 0:.2f}"
        sort = f"{r.get('sortino') or 0:.2f}"
        print(f"  {i+1:<3} {c.z_lookback_short:>3} {c.z_lookback_long:>3} "
              f"{c.z_entry:>5.2f} {c.z_exit:>5.1f} "
              f"{'Y' if c.use_dual_z else 'N':>4} "
              f"{'Y' if c.use_conviction else 'N':>4} "
              f"{'Y' if c.use_crash_pause else 'N':>4} "
              f"{'Y' if c.use_vol_scaling else 'N':>3} "
              f"{'Y' if c.use_momentum else 'N':>3} "
              f"{c.max_hold_days:>4} "
              f"{r['cagr']:>6.1f}% {r['mdd']:>6.1f}% {r['calmar']:>6.2f} "
              f"{shrp:>5} {sort:>5} {r['tr']:>5.1f}x "
              f"{ch_pct:>4.2f}% {r['trades']:>4}")


def print_yearwise(r, label):
    yearly = r["yearly"]
    c = r["cfg"]
    shrp = f", Sharpe={r.get('sharpe') or 0:.2f}"
    sort = f", Sortino={r.get('sortino') or 0:.2f}"
    print(f"\n  {label}: CAGR={r['cagr']:.1f}%, MDD={r['mdd']:.1f}%, "
          f"Calmar={r['calmar']:.2f}{shrp}{sort}")
    print(f"  Charges: {r['charges']:,.0f} ({r['charges']/10_000_000*100:.2f}% of capital)")
    print(f"  {'Year':<6} {'Return':>9} {'MaxDD':>9}")
    print(f"  {'-'*28}")
    for yr in sorted(yearly.keys()):
        y = yearly[yr]
        ret = (y["last"] - y["first"]) / y["first"] * 100
        dd = (y["trough"] - y["peak"]) / y["peak"] * 100 if y["peak"] > 0 else 0
        print(f"  {yr:<6} {ret:>+8.1f}% {dd:>8.1f}%")


# ── Sweep Engine ─────────────────────────────────────────────────────────────

def build_configs():
    """Build sweep configs — all combos of safety layers."""
    configs = []

    for zs in [15, 20, 30]:
        for zl in [45, 60, 90]:
            for zi in [0.5, 0.75, 1.0]:
                for zo in [-1.0, -0.5, 0.0]:
                    for dual in [False, True]:
                        for conv in [False, True]:
                            for crash in [False, True]:
                                for vol in [False, True]:
                                    for mom in [False, True]:
                                        for mh in [0, 60]:
                                            configs.append(PairCfg(
                                                z_lookback_short=zs,
                                                z_lookback_long=zl,
                                                z_entry=zi, z_exit=zo,
                                                use_dual_z=dual,
                                                use_conviction=conv,
                                                use_crash_pause=crash,
                                                use_vol_scaling=vol,
                                                use_momentum=mom,
                                                max_hold_days=mh,
                                                charge_model="us_etf",
                                            ))

    return configs


def build_focused_configs():
    """Smaller sweep focused on best parameter ranges."""
    configs = []

    for zs in [15, 20, 30]:
        for zl in [60, 90]:
            for zi in [0.5, 0.75, 1.0]:
                for zo in [-1.0, -0.5, 0.0]:
                    for mh in [0, 60]:
                        # 8 safety combos (most promising)
                        for dual, conv, crash, vol, mom in [
                            (False, False, False, False, False),  # naked
                            (False, False, True, False, False),   # crash only
                            (False, False, True, True, False),    # crash + vol
                            (False, True, False, False, False),   # conviction only
                            (False, True, True, True, False),     # conviction + safety
                            (True, False, False, False, False),   # dual-z only
                            (True, True, True, True, False),      # all except mom
                            (False, False, False, False, True),   # momentum only
                        ]:
                            configs.append(PairCfg(
                                z_lookback_short=zs, z_lookback_long=zl,
                                z_entry=zi, z_exit=zo,
                                use_dual_z=dual, use_conviction=conv,
                                use_crash_pause=crash, use_vol_scaling=vol,
                                use_momentum=mom, max_hold_days=mh,
                                charge_model="us_etf",
                            ))
    return configs


def sweep(pair_specs, common_epochs, label, configs, multi=False):
    """Sweep configs on single pair or multi-pair."""
    results = []
    for i, cfg in enumerate(configs):
        if multi:
            vals, tr, ch, wu = sim_multi_pair(pair_specs, common_epochs, cfg)
        else:
            ca, cb, _ = pair_specs[0]
            vals, tr, ch, wu = sim_pair(common_epochs, ca, cb, cfg)
        ep = common_epochs[wu:]
        if len(vals) > len(ep):
            vals = vals[:len(ep)]
        s = stats(vals, ep)
        if s and abs(s["mdd"]) > 0.01:
            results.append({"cfg": cfg, "trades": tr, "charges": ch, **s})
        if (i + 1) % 1000 == 0:
            print(f"    {i+1}/{len(configs)} done...")

    results.sort(key=lambda r: r["calmar"], reverse=True)

    print_top(results, f"{label} (by Calmar)")

    # Also show by CAGR
    by_cagr = sorted(results, key=lambda r: r["cagr"], reverse=True)
    print_top(by_cagr, f"{label} (by CAGR)", n=10)

    above_20 = [r for r in results if r["cagr"] >= 20.0]
    if above_20:
        above_20.sort(key=lambda r: r["calmar"], reverse=True)
        print(f"\n  ** {len(above_20)} configs with CAGR >= 20% (after charges) **")
        print_yearwise(above_20[0], "Best 20%+ by Calmar")
    else:
        print(f"\n  No configs reached 20%. Best: {results[0]['cagr']:.1f}% CAGR")
        print_yearwise(results[0], "Best overall")

    # Compare charge models: US ETF vs local
    if not multi and results:
        best_cfg = results[0]["cfg"]
        # Re-run with local charges
        local_cfg = PairCfg(**{k: v for k, v in best_cfg.__dict__.items()})
        local_cfg.charge_model = "local"
        local_cfg.local_exchange_a = "US"
        # Guess exchange B from pair
        ca, cb, lbl = pair_specs[0]
        if "India" in label or "NSEI" in label:
            local_cfg.local_exchange_b = "NSE"
        elif "HK" in label or "HSI" in label:
            local_cfg.local_exchange_b = "HKSE"
        elif "UK" in label or "FTSE" in label:
            local_cfg.local_exchange_b = "LSE"
        else:
            local_cfg.local_exchange_b = "US"

        vals_local, tr_l, ch_l, wu_l = sim_pair(common_epochs, ca, cb, local_cfg)
        ep_l = common_epochs[wu_l:]
        s_l = stats(vals_local, ep_l)
        if s_l:
            print(f"\n  Charge model comparison (best config):")
            print(f"    US ETF:  CAGR={results[0]['cagr']:>6.1f}%, charges={results[0]['charges']:>10,.0f}")
            print(f"    Local:   CAGR={s_l['cagr']:>6.1f}%, charges={ch_l:>10,.0f}")
            print(f"    Savings: {results[0]['cagr'] - s_l['cagr']:>+5.1f}pp CAGR from US ETF execution")

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

PAIRS_TO_TEST = [
    # (sym_a, sym_b, label, src_a, src_b)
    ("^GSPC", "^NSEI", "US vs India", "fmp", "fmp"),
    ("^GSPC", "^HSI", "US vs HK", "fmp", "fmp"),
    ("^GSPC", "^FTSE", "US vs UK", "fmp", "fmp"),
    ("^GSPC", "^GDAXI", "US vs Germany", "fmp", "fmp"),
    ("^GSPC", "^N225", "US vs Japan", "fmp", "fmp"),
    ("^GSPC", "^BVSP", "US vs Brazil", "fmp", "fmp"),
    ("SPY", "QQQ", "SPY vs QQQ", "fmp", "fmp"),
]


def main():
    start_epoch = 1104537600   # 2005-01-01
    end_epoch = 1773878400     # 2026-03-19

    cr = CetaResearch()

    # Fetch all data
    all_data = {}
    all_symbols = set()
    for sa, sb, _, srca, srcb in PAIRS_TO_TEST:
        all_symbols.add((sa, srca))
        all_symbols.add((sb, srcb))

    for sym, src in sorted(all_symbols):
        print(f"  Fetching {sym}...")
        data = fetch_data(cr, sym, src, start_epoch, end_epoch)
        if data:
            all_data[sym] = data
            print(f"    {len(data)} days")
        else:
            print(f"    FAILED")

    configs = build_focused_configs()
    print(f"\n  Sweep size: {len(configs)} configs per pair/portfolio")

    # ── Part 1: Single pairs (US ETF charges) ──
    print("\n" + "=" * 80)
    print("  PART 1: SINGLE PAIRS (US ETF charges ~0.003%)")
    print("=" * 80)

    pair_results = {}
    for sa, sb, label, _, _ in PAIRS_TO_TEST:
        if sa not in all_data or sb not in all_data:
            continue
        common = align_all([all_data[sa], all_data[sb]], start_epoch)
        if len(common) < 300:
            continue
        ca = [all_data[sa][e] for e in common]
        cb = [all_data[sb][e] for e in common]
        print(f"\n  {label}: {len(common)} common days")
        pair_results[label] = sweep([(ca, cb, label)], common, label, configs)

    # ── Part 2: Multi-pair portfolios (best combos) ──
    print("\n" + "=" * 80)
    print("  PART 2: MULTI-PAIR PORTFOLIOS (US ETF charges)")
    print("=" * 80)

    multi_combos = [
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI"), ("^GSPC", "^FTSE")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI"), ("^GSPC", "^FTSE"), ("^GSPC", "^GDAXI")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI"), ("^GSPC", "^FTSE"),
         ("^GSPC", "^GDAXI"), ("^GSPC", "^N225")],
        [("^GSPC", "^NSEI"), ("^GSPC", "^HSI"), ("^GSPC", "^FTSE"),
         ("^GSPC", "^GDAXI"), ("^GSPC", "^N225"), ("^GSPC", "^BVSP")],
    ]

    for combo in multi_combos:
        all_syms = set()
        for a, b in combo:
            all_syms.add(a)
            all_syms.add(b)
        if not all(s in all_data for s in all_syms):
            continue
        datasets = [all_data[s] for s in all_syms]
        common = align_all(datasets, start_epoch)
        if len(common) < 300:
            continue

        combo_label = "+".join(b.replace("^", "") for _, b in combo)
        pair_specs = []
        for sa, sb in combo:
            ca = [all_data[sa][e] for e in common]
            cb = [all_data[sb][e] for e in common]
            pair_specs.append((ca, cb, f"{sa}v{sb}"))

        print(f"\n  {len(combo)} pairs ({combo_label}): {len(common)} days")
        sweep(pair_specs, common, f"Multi({combo_label})", configs, multi=True)

    # ── Part 3: Cross-comparison summary ──
    print("\n" + "=" * 80)
    print("  SUMMARY: Best per strategy (all US ETF charges)")
    print("=" * 80)
    print(f"  {'Strategy':<30} {'CAGR':>7} {'MDD':>7} {'Calm':>6} {'Shrp':>5} {'Sort':>5} {'Chg%':>5}")
    print(f"  {'-'*75}")

    for label, results in pair_results.items():
        if results:
            b = results[0]
            ch_pct = b["charges"] / 10_000_000 * 100
            print(f"  {label:<30} {b['cagr']:>6.1f}% {b['mdd']:>6.1f}% {b['calmar']:>6.2f} "
                  f"{b.get('sharpe') or 0:>5.2f} {b.get('sortino') or 0:>5.2f} {ch_pct:>4.2f}%")


if __name__ == "__main__":
    main()

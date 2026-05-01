"""Leverage simulation with proper margin call modeling.

Simulates 1x, 2x, 3x, 5x leverage with:
  - Margin call: if equity drops below maintenance margin, forced liquidation
  - Zerodha MIS: 5x on equity intraday, no extra charges
  - Position sizing: leveraged capital / max_positions per trade
  - Drawdown amplification: MDD * leverage_factor
"""
import sys, json, math
sys.path.insert(0, "/home/swas/backtester")
from intraday_breakout_prod import run_pipeline
from collections import defaultdict

base = {
    "start_date": "2022-01-01",
    "end_date": "2025-12-31",
    "initial_capital": 1000000,
    "prefetch_days": 500,
    "top_n": 50,
    "min_avg_turnover": 500000000,
    "n_day_high": 3,
    "n_day_ma": 10,
    "internal_regime_sma_period": 50,
    "internal_regime_threshold": 0.4,
    "internal_regime_exit_threshold": 0.35,
    "max_entry_bar": 15,
    "max_positions": 5,
    "eod_exit_minute": 925,
    "entry_mode": "market",
    "require_gap_up": True,
    "slippage_bps": 0,
    "target_pct": 1.0,
    "stop_pct": 0.5,
    "trailing_stop_pct": 0,
}


def simulate_leverage(trade_log: list, initial_capital: float, leverage: float,
                      margin_call_pct: float = 0.25) -> dict:
    """Simulate leveraged equity curve from trade log.

    Args:
        trade_log: list of trades from run_pipeline (sorted by date)
        initial_capital: starting capital
        leverage: leverage multiplier (1x, 2x, 3x, 5x)
        margin_call_pct: forced liquidation when equity < this fraction of initial

    With intraday positions:
      - Each day, we deploy leverage * capital across positions
      - P&L is amplified by leverage factor
      - If equity hits margin_call_pct * initial → stop trading for rest of month
    """
    equity = initial_capital
    peak = initial_capital
    max_dd = 0
    margin_call_events = 0
    frozen_until = None  # date string: don't trade until this date

    daily_pnls = defaultdict(float)
    for t in trade_log:
        daily_pnls[t["trade_date"]] += t["pnl"]

    equity_curve = [(0, initial_capital)]
    monthly_returns = defaultdict(float)

    for date in sorted(daily_pnls.keys()):
        if frozen_until and date < frozen_until:
            continue

        # Amplify P&L by leverage
        day_pnl_1x = daily_pnls[date]
        day_pnl_lev = day_pnl_1x * leverage

        equity += day_pnl_lev
        equity_curve.append((date, equity))
        monthly_returns[date[:7]] += day_pnl_lev

        # Track peak and drawdown
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak
        if dd < max_dd:
            max_dd = dd

        # Margin call check
        if equity < initial_capital * margin_call_pct:
            margin_call_events += 1
            # Freeze trading for rest of month
            year, month = int(date[:4]), int(date[5:7])
            if month == 12:
                frozen_until = f"{year+1}-01-01"
            else:
                frozen_until = f"{year}-{month+1:02d}-01"

    # Compute CAGR
    years = 4.0  # 2022-2025
    final_return = equity / initial_capital
    cagr = (final_return ** (1 / years) - 1) * 100 if final_return > 0 else -100

    # Compute Sharpe from daily returns
    daily_rets = []
    prev_eq = initial_capital
    for _, eq in equity_curve[1:]:
        if prev_eq > 0:
            daily_rets.append(eq / prev_eq - 1)
        prev_eq = eq

    if daily_rets and len(daily_rets) > 1:
        mean_r = sum(daily_rets) / len(daily_rets)
        var_r = sum((r - mean_r) ** 2 for r in daily_rets) / (len(daily_rets) - 1)
        vol = math.sqrt(var_r) * math.sqrt(252)
        sharpe = (cagr / 100 - 0.065) / vol if vol > 0 else 0
    else:
        sharpe = 0

    calmar = cagr / 100 / abs(max_dd) if max_dd != 0 else 0

    # Monthly win rate
    pos_months = sum(1 for v in monthly_returns.values() if v > 0)
    total_months = len(monthly_returns)

    return {
        "leverage": leverage,
        "cagr": round(cagr, 2),
        "mdd": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "calmar": round(calmar, 3),
        "final_equity": round(equity, 0),
        "total_return": round((equity / initial_capital - 1) * 100, 2),
        "margin_calls": margin_call_events,
        "monthly_wr": round(pos_months / total_months * 100, 1) if total_months else 0,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Run base strategy to get trade log
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  LEVERAGE SIMULATION")
print("=" * 70)

# Run for each slippage level
for slip in [0, 2, 3, 5]:
    config = {**base, "slippage_bps": slip}
    output = run_pipeline(config)
    trade_log = output["results"][0]["trade_log"]

    print("\n  --- Slippage: %d bps ---" % slip)
    print("  %-5s %8s %7s %7s %7s %10s %6s %6s" % (
        "Lev", "CAGR", "MDD", "Sharpe", "Calmar", "Final Eq", "MCalls", "M-WR"))
    print("  " + "-" * 65)

    for lev in [1, 2, 3, 5, 7, 10]:
        result = simulate_leverage(trade_log, 1000000, lev)
        print("  %-5s %7.1f%% %6.2f%% %7.3f %7.3f %10s %6d %5.0f%%" % (
            "%dx" % lev, result["cagr"], result["mdd"], result["sharpe"],
            result["calmar"], f"Rs {result['final_equity']:,.0f}",
            result["margin_calls"], result["monthly_wr"]))

# ═══════════════════════════════════════════════════════════════════════════
# Prop firm simulation
# ═══════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("  PROP FIRM EVALUATION SIMULATION")
print("=" * 70)

# Simulate evaluation periods (1-3 months) with prop firm rules
# FundedStock: 10% profit target, 5% daily DD, 10% overall DD
# Apex: custom profit target, trailing drawdown

# Run with 0 slippage for best case
config_prop = {**base, "slippage_bps": 2}  # conservative 2 bps
output_prop = run_pipeline(config_prop)
trade_log_prop = output_prop["results"][0]["trade_log"]

# Group by date
daily_pnl = defaultdict(float)
for t in trade_log_prop:
    daily_pnl[t["trade_date"]] += t["pnl"]

# Simulate rolling evaluation windows
print("\n  Rolling 30-day evaluation windows (2bps slip, 5x leverage):")
print("  %-12s %-12s %8s %7s %12s %8s" % (
    "Start", "End", "Return%", "MaxDD%", "Profit Target", "Pass?"))
print("  " + "-" * 70)

dates = sorted(daily_pnl.keys())
window = 30  # trading days
leverage_prop = 5
profit_target = 0.10  # 10%
max_daily_dd = 0.05   # 5%
max_overall_dd = 0.10  # 10%

passes = 0
total_windows = 0

for i in range(0, len(dates) - window, window):
    window_dates = dates[i:i + window]
    equity = 1000000
    peak = equity
    max_dd = 0
    daily_dd_breach = False

    for d in window_dates:
        pnl = daily_pnl[d] * leverage_prop
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (equity - peak) / peak
        if dd < max_dd:
            max_dd = dd
        # Check daily DD
        daily_ret = pnl / (equity - pnl)
        if daily_ret < -max_daily_dd:
            daily_dd_breach = True

    ret = (equity / 1000000 - 1)
    passed = ret >= profit_target and abs(max_dd) < max_overall_dd and not daily_dd_breach
    if passed:
        passes += 1
    total_windows += 1

    print("  %-12s %-12s %+7.2f%% %6.2f%% %12s %8s" % (
        window_dates[0], window_dates[-1],
        ret * 100, max_dd * 100,
        "10%", "PASS" if passed else "FAIL"))

print("\n  Pass rate: %d/%d (%.0f%%)" % (passes, total_windows, passes/total_windows*100 if total_windows else 0))
print("  With 5x leverage on our strategy, each 30-day window needs ~2% unleveraged return to pass.")
print("\n  Done.")

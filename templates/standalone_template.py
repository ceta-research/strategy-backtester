"""
Standalone Backtest Template (BYOC Mode 2)
==========================================
Use this template if you want full control over your backtest.
Your code runs on CR compute and must output a result.json file
following the BacktestResult schema.

The results viewer will display your backtest if result.json matches
the expected format. You can use our lib/backtest_result.py helper
or produce the JSON directly.

Usage:
  1. Create a new project via the Projects API
  2. Upload this file as your entry point
  3. Run via POST /projects/{id}/run
  4. result.json is auto-uploaded to R2 and displayed in the UI
"""

import json
import os
import sys

# Option A: Use our BacktestResult helper (recommended)
# Add the strategy-backtester lib to your path if you cloned it
# sys.path.insert(0, "/path/to/strategy-backtester")
# from lib.backtest_result import BacktestResult

# Option B: Use the cr_client for data access
# CR_API_KEY is available in the container environment (injected by Nomad)
from cr_client import CetaResearch


def run_backtest():
    """Your backtest logic goes here."""
    cr = CetaResearch()  # Uses CR_API_KEY from environment

    # Fetch data via SQL
    prices = cr.query("""
        SELECT symbol, date, open, high, low, close, volume
        FROM stock_eod
        WHERE exchange = 'NSE'
          AND symbol = 'RELIANCE'
          AND date >= '2020-01-01'
        ORDER BY date
    """)

    # Your strategy logic here...
    # This is a placeholder - implement your own strategy
    capital = 1000000.0
    equity_curve = []
    trades = []
    current_value = capital

    # Example: buy and hold
    if prices and len(prices) > 0:
        buy_price = prices[0]["close"]
        sell_price = prices[-1]["close"]
        returns = (sell_price - buy_price) / buy_price
        current_value = capital * (1 + returns)

        for row in prices:
            equity_curve.append({
                "epoch": int(row["date"].timestamp()) if hasattr(row["date"], "timestamp") else 0,
                "date": str(row["date"]),
                "value": capital * (1 + (row["close"] - buy_price) / buy_price),
            })

        trades.append({
            "entry_epoch": equity_curve[0]["epoch"],
            "exit_epoch": equity_curve[-1]["epoch"],
            "entry_price": buy_price,
            "exit_price": sell_price,
            "quantity": int(capital / buy_price),
            "side": "LONG",
            "pnl": current_value - capital,
            "pnl_pct": returns,
            "hold_days": len(prices),
            "charges": 0.0,
            "slippage": 0.0,
            "net_pnl": current_value - capital,
            "instrument": "RELIANCE",
        })

    # Build result.json in the expected format
    import math

    days = len(equity_curve)
    years = days / 252 if days > 0 else 1
    total_return = (current_value - capital) / capital
    cagr = (current_value / capital) ** (1 / years) - 1 if years > 0 else 0

    result = {
        "version": "1.0",
        "type": "single",
        "strategy": {
            "name": "my_standalone_strategy",
            "description": "Custom standalone backtest",
            "params": {},
            "instrument": "RELIANCE",
            "exchange": "NSE",
            "capital": capital,
            "slippage_bps": 5,
        },
        "summary": {
            "cagr": round(cagr, 4),
            "max_drawdown": 0,  # Compute from equity curve
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "calmar_ratio": 0,
            "total_trades": len(trades),
            "win_rate": 1.0 if trades and trades[0]["pnl"] > 0 else 0.0,
            "profit_factor": 0,
            "expectancy": 0,
            "annualized_volatility": 0,
            "total_return": round(total_return, 4),
            "kelly_fraction": 0,
            "avg_trade_days": days,
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "monthly_returns": {},
        "yearly_returns": [],
        "costs": {
            "total_charges": 0,
            "total_slippage": 0,
            "total_cost": 0,
            "cost_pct_of_capital": 0,
        },
    }

    return result


if __name__ == "__main__":
    result = run_backtest()

    # Save to result.json - this file is auto-uploaded by the executor
    output_path = os.environ.get("OUTPUT_DIR", ".")
    with open(os.path.join(output_path, "result.json"), "w") as f:
        json.dump(result, f, indent=2)

    print(f"Backtest complete. CAGR: {result['summary']['cagr']:.2%}")
    print(f"Trades: {result['summary']['total_trades']}")
    print(f"Result saved to result.json")

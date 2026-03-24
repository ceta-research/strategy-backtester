# strategy-backtester

Position-level trading backtester with a pipeline architecture: YAML config, data fetch, signal generation, ranking, simulation, and standardized metrics output.

22 built-in strategies, 4 data providers, 11 exchanges. Pluggable signal generators so you can add your own.

## Setup

```bash
pip install -r requirements.txt
```

You need a [Ceta Research](https://cetaresearch.com) API key to fetch data. Sign up, then go to Settings > API Keys.

```bash
export CR_API_KEY=your_key_here
```

The free tier includes $1/mo in credits, 100k row limit on financial data, 3 GB RAM, and 120s query timeout. Most single-stock and single-ETF backtests fit within these limits. See [cetaresearch.com/pricing](https://cetaresearch.com/pricing) for details.

## Quick start

```bash
# Run a standalone strategy
python scripts/buy_2day_high.py

# Run a pipeline strategy (YAML-configured, sweepable params)
python run.py --strategy ibs_mean_reversion

# Run on cloud compute
python run_remote.py scripts/buy_2day_high.py
```

## Project structure

```
engine/             Pipeline engine (data fetch, simulation, ranking)
engine/signals/     Signal generators (one per strategy)
lib/                Shared libraries (API client, metrics, output format)
strategies/         YAML configs for pipeline strategies
scripts/            Standalone strategy scripts
docs/               Development guide and output schema
```

## Writing a strategy

See [docs/BACKTEST_GUIDE.md](docs/BACKTEST_GUIDE.md) for the full guide: architecture, writing strategies (standalone and pipeline), execution rules, output schema, and metrics reference.

## License

MIT

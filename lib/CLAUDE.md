# lib/

Shared modules for `engine/`, `scripts/` and the `run_*.py` entry points.

## Modules

- `cr_client.py`: the `CetaResearch` client for SQL queries, code execution and cloud projects. It takes the key from the `api_key` argument, then `CR_API_KEY`, then `TS_API_KEY`, then a `CR_API_KEY=` line in `./.env` or `/session/.env`. `CR_BASE_URL` overrides the API host.
- `cloud_orchestrator.py`: project sync, run submission, polling and result download for `run_remote.py`, `scripts/run_all_cloud.py` and `scripts/cloud_sweep.py`.
- `backtest_result.py`, `metrics.py`, `equity_curve.py`: the result JSON and its metrics. `metrics.py` imports only `math` and `equity_curve.py`. CAGR uses wall-clock years from the first and last epoch, and the curve's frequency is used only to annualize volatility.
- `ensemble_curve.py`: combines strategy equity curves for `scripts/run_ensemble.py`.
- `audit_io.py`: schemas and parquet writers for the audit drill in `scripts/run_audit_drill.py`.
- `data_utils.py`, `data_fetchers.py`, `indicators.py`: helpers for standalone strategy scripts. Polars indicators for signal generators live in `engine/signals/base.py`.

## Test

From the repo root: `.venv/bin/python -m pytest tests/test_metrics.py tests/test_equity_curve.py tests/test_backtest_result.py tests/test_ensemble_curve.py tests/test_cr_client_tier_clamp.py -q`. None of them calls the API.

## Gotchas

- `CloudOrchestrator.make_wrapper` writes the API key in plain text into the wrapper script it uploads. Never print, log or save a wrapper.
- `cr_client` clamps each query's timeout, memory, threads and disk to the key's tier caps, fetched once per process. A larger `cr_api_*` value in a config is cut down with a printed note, and the server rejects values above the caps.

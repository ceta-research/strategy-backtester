# engine/

The backtest pipelines. Strategy logic lives in `signals/` (see `signals/CLAUDE.md`); this folder holds what every strategy shares.

## Entry points

- `pipeline.py`: `run_pipeline(config_path, data_provider=None)` runs the EOD flow. It loads the config, fetches OHLCV, calls the generator named by `static.strategy_type`, validates and sanitizes the orders, then ranks (`ranking.sort_orders`) and simulates (`simulator.process`) once per scanner × entry × exit × simulation config.
- `config_loader.py`: builds the scanner, entry, exit, simulation and static sections with their defaults. Each scanner, entry, exit and simulation value is a list, and `config_sweep.create_config_iterator` sweeps the Cartesian product. Config ids count from 1, and an empty list raises.
- `intraday_pipeline.py`: `static.strategy_name` picks the SQL builder (`orb`, `vwap_mr`), and `static.pipeline_version` picks the simulator (default `v2`; `v1` and `intraday_simulator.py` are deprecated).
- `intraday_breakout_pipeline.py`: `strategy_type: intraday_breakout` configs run only as `.venv/bin/python -m engine.intraday_breakout_pipeline <config.yaml> [--output <json>]` (not run here: needs `CR_API_KEY`). Under `run.py` they fail in `config_loader.validate_config`.
- `data_provider.py`: YAML selects one of three providers with `static.data_provider`: `cr` (default), `bhavcopy` or `nse_charting`. Any other value, `parquet` included, silently runs `cr`. The parquet and DuckDB providers work only when passed as `run_pipeline(config_path, data_provider=...)`.

## Which code a strategy runs

- `eod_technical` is the only generator on `scanner.py` and `order_generator.py`.
- Every other generator uses `signals/base.run_scanner`, which skips the n-day-gain filter unless the config sets `scanner.apply_n_day_gain: [true]`.

## Conventions

- A new knob defaults to the old behaviour, so existing configs reproduce byte for byte (see the comments in `config_loader.py`). The 2026-10-09 `ranking_nulls_last` flip was a deliberate exception.
- Pass `maintain_order=True` to every polars `group_by`. `tests/test_determinism.py` scans `engine/*.py` and `engine/signals/*.py` for it.
- Audit hooks in `scanner.py`, `order_generator.py` and `signals/eod_breakout.py` stay behind an `audit_mode and audit_collector is not None` check (with `self.` inside `OrderGenerationUtil`). `tests/test_audit_noninvasive.py` greps the source for these guards.
- `tests/test_charges.py` pins every fee constant in `charges.py`. A rate change edits both in the same commit, with a rate-vintage note in the `charges.py` docstring. `prod_runner/` holds hand copies of code from `charges.py`, `internal_regime.py` and `intraday_breakout_pipeline.py` that a change here doesn't reach: see `prod_runner/CLAUDE.md`.

## Test

From the repo root, one module at a time: `.venv/bin/python -m pytest tests/test_ranking.py -q`. Most modules have a `tests/test_<module>*.py`.

## Gotchas

- The simulator is long-only, credits sale proceeds the same day and scales slippage linearly. Read the `simulator.py` docstring before trusting a capital-constrained result.
- A multi-exchange config sums equity in each exchange's local currency with no FX conversion (`pipeline.py` docstring).
- A config with no `strategy_type` runs `eod_technical`.

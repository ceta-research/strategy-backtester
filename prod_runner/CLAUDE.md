# prod_runner/

Standalone intraday runners for the machine that holds the NSE daily and minute parquet data. They call no API and import nothing from `engine/` or `lib/`: only polars, pyyaml and the standard library.

## Entry points
- `intraday_breakout_prod.py`: the intraday breakout pipeline over local parquet. Run: `python3 intraday_breakout_prod.py config_r0.yaml --output r0.json` from this folder (not run here: needs the local parquet data).
- `open_low_meanrev_prod.py`: open-low mean reversion on Nifty 50. Imports its loaders and charges from `intraday_breakout_prod.py`.
- `config_r0.yaml` (one config) and `config_r1_sweep.yaml` (lists, swept as a cartesian product). The CLI flattens the sections `static`, `universe`, `daily_signal`, `intraday_entry`, `intraday_exit` and `simulation` into one dict. This is not the engine's config format, so `run.py` can't read these.

## What it mirrors from engine/
Hand copies; nothing keeps them in step and no test checks them.
- `nse_intraday_charges` and its seven `NSE_*` rate constants copy `engine/charges.py`. Identical today.
- `compute_regime_hysteresis` copies `compute_internal_regime_epochs_hysteresis` in `engine/internal_regime.py`. Same bull days today; it takes a boolean `scanner_pass` column where the engine takes `scanner_config_ids`.
- `select_universe`, `compute_daily_signals` and `simulate_intraday_day` rework the functions of the same name in `engine/intraday_breakout_pipeline.py`; `run_pipeline` reworks its `run_intraday_breakout_pipeline`. They have drifted: only the prod copy models `slippage_bps`, and its `compute_daily_signals` takes precomputed regime days instead of regime parameters.
- A change to engine charges or the regime doesn't reach this folder. Make it in both places.

## Check (from the repo root)
- `python -m py_compile prod_runner/*.py`
- Charges parity after touching either copy:
  `python -c "import sys; sys.path.insert(0, 'prod_runner'); import intraday_breakout_prod as p, engine.charges as c; assert all(p.nse_intraday_charges(v) == c.nse_intraday_charges(v) for v in range(0, 5_000_000, 997)); print('charges match')"`

## What depends on it
- `open_low_meanrev_prod.py`, three scripts in `strategies/pair_trading/` and the scripts in `strategies/intraday_breakout/tuning/` import `intraday_breakout_prod` by module name. Renaming it, or changing `load_daily_data`, `load_minute_data`, `run_pipeline`, `nse_intraday_charges` or `SECONDS_IN_ONE_DAY`, breaks them.

## Gotchas
- The data paths at the top of `intraday_breakout_prod.py` and the absolute paths in `open_low_meanrev_prod.py` are machine-specific: never quote them.
- `intraday_breakout_prod.py` loads the whole date range into memory with no chunking. The engine version fetches month by month.

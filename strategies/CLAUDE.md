# strategies/

YAML configs for the engine, one folder per active strategy, plus each strategy's optimization record. Setup and tests: see the root CLAUDE.md.

## Config pattern
- `static.strategy_type` picks the signal generator. It must be registered through an import in `engine/signals/__init__.py`. `breakout_pullback` has a signal module but no import, so its config fails with `Unknown strategy_type`.
- Every parameter under `scanner`, `entry`, `exit` and `simulation` is a list. The engine runs the cartesian product of all lists, so a single run uses one-element lists.
- File names follow the rounds in `docs/OPTIMIZATION_RUNBOOK.md`: `config_baseline.yaml` (R0), `config_round1*.yaml`, `config_round2.yaml`, `config_round3.yaml`, `config_r4_*.yaml` (OOS, walk-forward, other data sources, other exchanges), `config_champion.yaml` (current pick).
- `<name>/OPTIMIZATION.md` logs each round and the champion. `OPTIMIZATION_QUEUE.yaml` tracks status per strategy: 13 COMPLETE, 17 AUDIT_RETIRED, 0 PENDING.
- `schema.yaml` and `_shared_schema.yaml` hold UI labels and ranges. No code in this repo reads them; defaults live in `engine/config_loader.py` and the signal classes.

## Run and check (from the repo root)
- Run: `python run.py --config strategies/eod_breakout/config_champion.yaml --output results/eod_breakout/champion.json` (not run here: needs `CR_API_KEY`).
- `python run.py --strategy <name>` and `--list` only see folders with a literal `config.yaml`: factor_composite, momentum_cascade, momentum_top_gainers, quality_dip_buy, trending_value. Use `--config` for the rest.
- Check a config offline (parses it and resolves its `strategy_type`, no key needed):
  `python -c "import sys, yaml; from engine.config_loader import load_config; from engine.signals.base import get_signal_generator; p = sys.argv[1]; load_config(p); get_signal_generator(yaml.safe_load(open(p))['static']['strategy_type']); print('ok', p)" strategies/eod_breakout/config_champion.yaml`

## Folders that differ
- `intraday_breakout/` configs use scalar params and don't load through `run.py`. Run them with `python -m engine.intraday_breakout_pipeline strategies/intraday_breakout/config_r0_baseline.yaml` (not run here: needs `CR_API_KEY`).
- `open_low_meanrev/` and `pair_trading/` have no engine configs. Their code is standalone scripts, `prod_runner/open_low_meanrev_prod.py` and `pair_trading/*.py`; `prod_runner/CLAUDE.md` says how they depend on it.
- `retired/`: 20 strategies moved out on 2026-04-29. Their signal modules are still registered (`orb`, `orb_us` and `vwap_mr` use `strategy_type: intraday` instead), so they run with `--config strategies/retired/<name>/<file>.yaml`. `--strategy` never looks in `retired/`.
- `ensembles/<name>/config.yaml` (18 of them) combine finished runs. Run: `python scripts/run_ensemble.py --ensemble strategies/ensembles/eod_lowpe_5050/config.yaml --output results/ensembles/eod_lowpe_5050.json` (not run here: each leg's `result_path` JSON must exist under `results/`). Schema and weighting modes: `docs/ENSEMBLE_GUIDE.md` and the docstring of `scripts/run_ensemble.py`.

## What depends on these files
- `tests/test_audit_noninvasive.py` (slow, gated) runs `eod_breakout/config_champion.yaml` and `eod_technical/config_champion.yaml`.
- `eod_breakout/config_champion.yaml` and `config_ir_hyst_best.yaml` are identical. `config_ir_hyst_live.yaml` differs only in `end_epoch`, a far-future sentinel its header explains. `scripts/eod_breakout_shortlist.py` reads the live file and `scripts/live_daily_runner.py` defaults to the best file. Change all three together.

## Gotchas
- Outside `experiments/`, every number in config header comments, `OPTIMIZATION.md` files and the queue predates the 2026-10-09 ranking fix. Current figures are in the banner at the top of `docs/LEADERBOARD.md`. Example: `eod_breakout/config_champion.yaml` says CAGR 22.26%; the re-run gives 17.37%.
- `OPTIMIZATION.md` can lag `config_champion.yaml`. `eod_breakout/OPTIMIZATION.md` still describes the 2026-04-27 champion; the config holds the 2026-04-30 internal-regime one. Trust the config.
- `run_pipeline` in `engine/pipeline.py` knows two `data_provider` values, `nse_charting` and `bhavcopy`. Anything else, `cr`, `parquet` or a typo, silently uses the CR API provider. `scripts/measure_bias_impact.py` passes its own provider, so the field is ignored there; the three `config_phase_8a_local.yaml` files that say `parquet` only get local data that way.
- `data_provider: bhavcopy` reads unadjusted NSE prices with no corporate-action handling and no oscillation filter (docstring of `BhavcopyDataProvider` in `engine/data_provider.py`). A split or bonus day looks like a crash to any signal that compares price levels over time. Every config with `bhav` in its file name uses it.

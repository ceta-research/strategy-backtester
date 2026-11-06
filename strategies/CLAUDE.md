# strategies/

YAML configs for the engine, one folder per active strategy, plus each strategy's optimization record.

## Config pattern
- `static.strategy_type` names the signal generator. How one is registered, and why `breakout_pullback/` fails with `Unknown strategy_type`: `engine/signals/CLAUDE.md`.
- File names follow the rounds in `docs/OPTIMIZATION_RUNBOOK.md`: `config_baseline.yaml` (R0), `config_round1*.yaml`, `config_round2.yaml`, `config_round3.yaml`, `config_r4_*.yaml` (validation). `config_champion.yaml` is the current pick.
- `<name>/OPTIMIZATION.md` logs each round and the champion. `OPTIMIZATION_QUEUE.yaml` tracks status per strategy; no item is PENDING.
- `schema.yaml` and `_shared_schema.yaml` hold UI labels and ranges. No code in this repo reads them; defaults live in `engine/config_loader.py` and the signal classes.

## Run and check (from the repo root)
- Run: `mkdir -p results/eod_breakout && .venv/bin/python run.py --config strategies/eod_breakout/config_champion.yaml --output results/eod_breakout/champion.json` (not run here: needs `CR_API_KEY`). `run.py` doesn't create the output folder, so without the `mkdir` the save fails after the whole backtest.
- Check an EOD config offline (parses it and resolves its `strategy_type`, no key needed). It doesn't cover `intraday` or `intraday_breakout` configs:
  `.venv/bin/python -c "import sys, yaml; from engine.config_loader import load_config; from engine.signals.base import get_signal_generator; p = sys.argv[1]; load_config(p); get_signal_generator(yaml.safe_load(open(p))['static'].get('strategy_type', 'eod_technical')); print('ok', p)" strategies/eod_breakout/config_champion.yaml`

## Folders that differ
- `intraday_breakout/` configs use scalar params and need their own command: `engine/CLAUDE.md`.
- `open_low_meanrev/` and `pair_trading/` have no engine configs. Their code is standalone scripts, `prod_runner/open_low_meanrev_prod.py` and `pair_trading/*.py`; `prod_runner/CLAUDE.md` says how they depend on it.
- `retired/`: 20 strategies moved out on 2026-04-29. Run them with `--config strategies/retired/<name>/<file>.yaml`. `orb`, `orb_us` and `vwap_mr` are `strategy_type: intraday`.
- `ensembles/<name>/config.yaml` (18 of them) combine finished runs through `scripts/run_ensemble.py`. Usage: its docstring (not run here: each leg's `result_path` JSON must exist under `results/`). Schema: `docs/ENSEMBLE_GUIDE.md`.

## What depends on these files
- `tests/test_audit_noninvasive.py` (slow, gated) runs `eod_breakout/config_champion.yaml` and `eod_technical/config_champion.yaml`.
- `eod_breakout/config_champion.yaml` and `config_ir_hyst_best.yaml` hold the same settings; only their comments differ. `config_ir_hyst_live.yaml` differs only in `end_epoch`, a far-future sentinel its header explains. `scripts/eod_breakout_shortlist.py` reads the live file and `scripts/live_daily_runner.py` defaults to the best file. Change all three together.

## Gotchas
- Outside `experiments/`, every number in config header comments, `OPTIMIZATION.md` files and the queue predates the 2026-10-09 ranking fix. Example: `eod_breakout/config_champion.yaml` says CAGR 22.26%; the re-run in `docs/LEADERBOARD.md` gives 17.37%.
- `OPTIMIZATION.md` can lag `config_champion.yaml`. `eod_breakout/OPTIMIZATION.md` still describes the 2026-04-27 champion; the config holds the 2026-04-30 internal-regime one. Trust the config.
- The three `config_phase_8a_local.yaml` files say `data_provider: parquet`, which `run.py` runs as `cr` (see `engine/CLAUDE.md`). They get local data only through `scripts/measure_bias_impact.py`, which passes its own provider.
- Every config with `bhav` in its file name uses `data_provider: bhavcopy`. That provider reads unadjusted prices; see the root CLAUDE.md gotcha.

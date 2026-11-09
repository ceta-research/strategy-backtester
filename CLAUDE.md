# strategy-backtester

## Before you change anything

- Any run that fetches data needs a Ceta Research key: `export CR_API_KEY=...`, or a `CR_API_KEY=...` line in `.env` in the working directory. `.env` holds credentials: never quote it.

<!-- shared:public-rules v2. Every public repo of ours carries this block word for word; change it in all of them at once, never in one repo alone. -->
## Rules for every change

- Never commit a secret, an API key, a server address, or anything that isn't meant to be public.
- Treat the default branch as released code: it can reach users without a further step.
- No AI attribution anywhere: no Co-Authored-By trailer, no "Generated with Claude Code" line and no session link in commits, PR bodies, issues or comments.
- One copy of everything. Never copy a block of code: reuse it, or move it into a shared helper. Instructions follow the same rule: each rule lives in one file and other files point to it, so nothing can drift apart. Keep files and functions small.
- Fix this repo's CLAUDE.md files in passing. When the code contradicts a line, or you had to dig for a fact a session needs, fix the nearest CLAUDE.md, outside this block, in the PR you're on. Write the current fact only, never its history: git keeps that. Add a folder CLAUDE.md only when the folder has facts its parent can't hold in a line or two.
- Write plainly: short sentences, lead with the point, no em-dash connectors. Code comments are one short line, only where the code needs one.
<!-- /shared:public-rules -->

## What this is

A position-level backtester for trading strategies. A YAML config drives a pipeline: fetch data from the Ceta Research API, generate signals, rank orders, simulate the portfolio and write a standard metrics JSON. It runs EOD and intraday strategies. It is for anyone with a Ceta Research API key who wants to run the built-in strategies or add their own signal generators.

## Layout

- `engine/`: the EOD and intraday pipelines, scanner, ranking, simulator, charges and data providers. See `engine/CLAUDE.md`.
- `engine/signals/`: one signal generator per strategy, registered by import. See `engine/signals/CLAUDE.md`.
- `lib/`: API client, result format and metrics, cloud orchestration. See `lib/CLAUDE.md`.
- `tests/`: unit tests, the regression snapshot tool, verification scripts. See `tests/CLAUDE.md`.
- `strategies/`: per-strategy YAML configs and notes, with `retired/` and `ensembles/`. See `strategies/CLAUDE.md`.
- `scripts/`: sweep, walk-forward, ensemble, audit, cloud and live observe scripts. See `scripts/CLAUDE.md`.
- `prod_runner/`: standalone intraday runners for a machine with local NSE parquet data, with hand copies of engine code. See `prod_runner/CLAUDE.md`.
- `docs/`: guides, status, leaderboard, inspection reports. See `docs/CLAUDE.md`.
- `templates/`: starter configs (`eod/config.yaml`, `intraday/config.yaml`) and strategy templates.
- `run.py`: local CLI, `python run.py --list | --strategy <name> | --config <yaml> [--output <json>]` (`--strategy` and `--config` not run here: need `CR_API_KEY`). It sends `static.strategy_type: intraday` to `engine/intraday_pipeline.py` and everything else to `engine/pipeline.py`.
- `run_global.py`: runs `darvas_box`, `swing_master`, `squeeze` and `holp_lohp`, all four retired, across 15 exchanges.
- `run_remote.py`: runs a script or pipeline YAML on Ceta Research cloud compute. It writes the gitignored `.remote_project.json` and `.remote_hashes.json`.

## Setup from a fresh clone

No Python version is pinned. Use 3.12 or later. On 3.11 one more test fails (`tests/test_metrics_edge.py::TestDualSharpe::test_zero_vol_returns_none_for_both`), because float `sum()` rounds differently before 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

- `pytest` is not in `requirements.txt`.
- `.venv/` is not in `.gitignore`. Stage files by name, never with `git add -A` or `git add .`.
- `polars==1.37.1` in `requirements.txt` must match `DEFAULT_DEPENDENCIES` in `lib/cloud_orchestrator.py`, which sets the packages of the cloud project.
- Without a key you can run the tests, `.venv/bin/python run.py --list` and `.venv/bin/python -m py_compile run.py run_global.py run_remote.py`.

## Tests and CI gates

There is no CI: no `.github/workflows/`, Makefile or pyproject. The gate is the test suite, run from the repo root:

```bash
.venv/bin/python -m pytest tests -q
```

- It collects 496 tests, needs no key and makes no API calls.
- Use pytest, not `python -m unittest discover -s tests`. unittest skips the 61 plain `def test_*` functions in six files, `tests/test_charges.py` among them.
- On `b45c980` two tests fail before any change: `tests/test_determinism.py::TestGroupByMaintainOrderAudit::test_engine_root_group_by_has_maintain_order` and `tests/test_edge_cases.py::TestConfigLoaderFallbacks::test_simulation_config_defaults_for_missing_keys`. Any other failure comes from your change.
- The four skips are a slow regression: `STRATEGY_BACKTESTER_AUDIT_REGRESSION=1 .venv/bin/python -m pytest tests/test_audit_noninvasive.py` (not run here: needs `CR_API_KEY` and the gitignored `results/<strategy>/champion_pre_audit_baseline.json` files).

## How changes ship

- No CI runs on a pull request or a merge. A merge to `main` builds and deploys nothing.
- `run_remote.py` uploads files from the local working tree to a Ceta Research cloud project when it runs (unless `--no-sync`), so a remote run uses your checkout, not `main`.

## Gotchas

- `README.md` is stale. `scripts/buy_2day_high.py` doesn't exist and `ibs_mean_reversion` moved to `strategies/retired/`, so its quick-start commands fail. The usage examples in `run_remote.py` also point to missing paths.
- `run.py --strategy <name>` reads only `strategies/<name>/config.yaml`, and five strategies have one (`run.py --list` shows them). For the rest pass `--config strategies/<name>/<file>.yaml`.
- `simulation.ranking_nulls_last` defaults to `true` since 2026-10-09. Before that, orders with no rank (instruments with fewer bars than the ranking window) took position slots first. Every result and doc figure produced before that date used the old order, including the main table in `docs/LEADERBOARD.md`. The exceptions are runs of `strategies/eod_breakout/config_ir_hyst_armD_nulls_last.yaml` and `config_ir_hyst_bhav_armD.yaml` (added 2026-07-05), which set `ranking_nulls_last: [true]`. To reproduce an old figure, set `ranking_nulls_last: [false]` and say so beside it. Background: the `engine/ranking.py` docstring.
- `docs/STATUS.md` was last updated 2026-05-03. It marks `engine/pipeline.py`, `utils.py`, `simulator.py`, `ranking.py`, `charges.py`, `exits.py`, `order_key.py` and `lib/metrics.py`, `backtest_result.py`, `equity_curve.py` as protected: no change without a re-audit. Its `git diff fbcd36a HEAD -- <those files> | wc -l` check no longer prints 0: seven of them have changed since, most recently on 2026-10-09.
- `static.data_provider: bhavcopy` reads unadjusted NSE bhavcopy prices, applies no corporate actions and skips the price oscillation filter (see the `engine/data_provider.py` docstring). A signal that compares price levels across time sees a split or bonus day as a crash. `nse_charting` and the default `cr` are split-adjusted, and `cr` is not dividend-adjusted.
- `lib/cloud_orchestrator.py` hand-lists the engine and lib files that `run_remote.py`, `scripts/run_all_cloud.py` and `scripts/cloud_sweep.py` upload (`EOD_ENGINE_FILES`, `INTRADAY_ENGINE_FILES`, `LIB_FILES`). `engine/exits.py`, `engine/order_key.py`, `engine/internal_regime.py` and `lib/equity_curve.py` are imported by the engine but missing from those lists. Add any new engine or lib module there. Signal modules are picked up by glob.

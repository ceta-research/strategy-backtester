# scripts/

Research harnesses (walk-forward, R4 validation, sweeps), result analysis, the audit-drill tools and the live observe tools.

- `portfolio_helper.py` reads holdings from a live Kite brokerage account through `KITE_API_KEY` and `KITE_ACCESS_TOKEN`, and needs `kiteconnect`, which isn't in `requirements.txt`. Never run it from a session.
- `analyze_feature_pnl.py` puts the absolute path of your clone in the `_Source:` line of `FEATURE_PNL_<strategy>.md`. Make it repo-relative before committing.

## Conventions
- Run every script from the repo root as `.venv/bin/python scripts/<name>.py`. Several shell out to `run.py` by relative path, `backtest_main.py` and `cloud_main*.py` put the working directory on `sys.path`, and 18 of the 51 use relative `results/` or `strategies/` paths.
- `--help` is safe only on scripts that use argparse. The rest don't parse it and may start work: `scripts/rebuild_leaderboard.py --help` rewrites `docs/LEADERBOARD.md`.
- These analysis tools need no key: `analyze_*.py`, `decode_config_id.py`, `optimization_status.py`, `recompute_metrics.py`, `run_ensemble.py`, `audit_sanity_checks.py`, `pattern_queries.py`, `spot_check_audits.py`. All but `decode_config_id.py` read earlier output under `results/`.
- `decode_config_id.py <config.yaml> <config_id>` maps an id such as `1_1_1_2` in a sweep result back to its parameter values. Runs offline.

## Check (from the repo root)
- `.venv/bin/python -m py_compile scripts/*.py scripts/benchmark/*.py`
- `.venv/bin/python scripts/<name>.py --help` for the argparse scripts. It works offline for all of them except `portfolio_helper.py`, which exits at import without `kiteconnect` (see above).

## Live and cloud scripts
- `eod_breakout_shortlist.py`: observe only, places no orders. Each run appends to an observation log; pass `--no-log` when testing. Not run here: needs `CR_API_KEY`.
- `momentum_cascade_daily.py`: observe only, places no orders. Runs `strategies/momentum_cascade/config_live.yaml` through the latest bar and prints the book and the next day's orders. Each run appends to an observe log and a paper ledger under `results/momentum_cascade/`; `--no-log` skips both but still rewrites `daily_latest.json`, which the next run's history-rewrite check compares against. Not run here: needs `CR_API_KEY`.
- `live_daily_runner.py`: writes BUY/SELL recommendations from a portfolio JSON; a person places the orders. Not run here: needs `CR_API_KEY`.
- `run_all_cloud.py` and `cloud_sweep.py` upload code to Ceta Research cloud compute and run it there; `cloud_main_eod.py` and `cloud_main.py` are the entry points they upload. Not run here: need `CR_API_KEY` and use that account's cloud compute.
- `benchmark_catalog.py run` measures each case in `benchmark/suite.json` (the config in `benchmark/configs/`, plus a 2x5 and a 5x10 entry-by-exit sweep) at 1, 10 and 50 configs: rows per query, fetch, signal and simulation seconds, peak memory of the process tree and `result.json` bytes, one JSON line per run. Re-run it on a new engine release with the same command, then `benchmark_catalog.py report <runs.jsonl>`. Both take `--platform <file>`, the hosted container's shape and the platform's rates and caps, kept outside the repo; `benchmark/report.py`'s docstring lists its fields. Linux only (`/proc`, `taskset`). Not run here: needs `CR_API_KEY`, and each run reads the full price history once.
- `benchmark/configs/` is a pinned copy of what the platform's form submits per strategy when nothing is edited, close to each `config_champion.yaml` but not loaded from it, so runs stay comparable across engine releases. Update the copies only when the form's defaults change.

## Gotchas
- `rebuild_leaderboard.py` overwrites all of `docs/LEADERBOARD.md` from result JSONs under `results/`. It drops the hand-written 2026-10-09 re-run banner, and without `results/` every row is `—`. Don't run it to refresh the leaderboard. If you must, restore the banner by hand.
- `analyze_feature_pnl.py`, `pattern_queries.py`, `audit_sanity_checks.py` and `spot_check_audits.py` write their reports into `docs/inspection/`.
- `archive/` is dead code. Several of its scripts import modules that no longer exist, such as `scripts.quality_dip_buy_fundamental`. Don't run them.
- `quality_dip_buy_lib.py` is a library, not a script; `debug_signal_gen.py` imports it.

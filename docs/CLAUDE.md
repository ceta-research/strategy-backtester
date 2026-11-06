# docs/

Guides for the engine and the research method, plus a record of past research. Most files here are history: read them for context, don't treat their numbers or plans as current.

## Live: keep in step with the code
- `ENGINE_STRATEGY_GUIDE.md` §2: adding a pipeline strategy (signal module, import line in `engine/signals/__init__.py`, YAML config). Its §1b example `scripts/momentum_dip_buy.py` no longer exists.
- `OPTIMIZATION_RUNBOOK.md`: the R0 to R4 round method that the `strategies/` config names follow.
- `ENSEMBLE_GUIDE.md`: ensemble YAML schema, weighting and rebalancing math. It omits the `convex_max_sharpe` and `convex_target_vol` modes in `lib/ensemble_curve.py`.
- `LEADERBOARD.md`: the banner at the top is the 2026-10-09 re-run under the ranking fix, with its data in `leaderboard_rerun_2026-10-09.tsv`. The table below the banner is superseded. Before regenerating the file, read the `rebuild_leaderboard.py` gotcha in `scripts/CLAUDE.md`.

## Partly stale
- `BACKTEST_GUIDE.md`: standalone scripts, execution rules and the `result.json` schema. Its Quick Start runs `scripts/buy_2day_high.py`, which no longer exists, and its schema shows version `1.0` where `lib/backtest_result.py` writes `1.1`.

## History: don't update
- `STATUS.md`: last updated 2026-05-03. None of the 13 later commits has touched it, and its tables predate the ranking fix.
- `OPTIMIZATION_PROMPT.md`: describes the queue before it emptied; `strategies/OPTIMIZATION_QUEUE.yaml` now has 0 PENDING.
- `LIVE_TRADING_INTEGRATION.md`: a 2026-04-28 plan. The Phase 1 scripts it lists were never built; the live tools that exist are `scripts/eod_breakout_shortlist.py` and `scripts/live_daily_runner.py`.
- `R4C_R4D_BACKFILL_2026-04-28.md`: a one-off results write-up.
- `inspection/`: the 2026-04-28 audit drill on eod_breakout and eod_technical. `FINDINGS_*.md` and `PIPELINE_MAP.md` are hand-written. `FEATURE_PNL_*.md`, `PATTERNS_*.md`, `SANITY_CHECKS.md` and `SPOT_CHECKS.md` are script output (see `scripts/CLAUDE.md`).
- `sessions/`: per-session handovers. `archive/`: audit-era (2026-04) and pre-engine (2026-03) docs.

## Gotchas
- Paths in older docs drift: files they name moved to `scripts/archive/`, were deleted, or were never written. Check a path exists before following it.

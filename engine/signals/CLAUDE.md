# engine/signals/

One signal generator per strategy. `static.strategy_type` in a config names the generator by the name passed to `register_strategy`. Retired strategies stay registered, because their configs under `strategies/retired/` still use them.

## Add a signal

Follow `docs/ENGINE_STRATEGY_GUIDE.md` section 2. Its skeleton is current. Beyond it:

- Registration runs only on import, so the guide's `__init__.py` line is required. `breakout_pullback.py` lacks it, so its config fails with `ValueError: Unknown strategy_type 'breakout_pullback'`.
- Without `build_entry_config` and `build_exit_config`, `config_loader` falls back to `_build_entry_config_default` and `_build_exit_config_default`. Every current generator has both.
- The guide's section 4 walk-forward example passes `trailing_stop_pct=`, but the parameter is `trailing_stop_fraction`.

Fast checks for a new generator, from the repo root: `.venv/bin/python -m pytest tests/test_signal_audit.py tests/test_determinism.py::TestGroupByMaintainOrderAudit::test_engine_signals_group_by_has_maintain_order -q`.

## Rules the code and tests enforce

- Enter at the next bar's open. `add_next_day_values` adds `next_epoch` and `next_open`. `tests/test_signal_audit.py` fails on `entry_price = row["close"]` unless the file is in its `FLAGGED` or `FALLBACK_OK` set.
- `walk_forward_exit` takes `require_peak_recovery` by keyword with no default: `True` for dip-buy, `False` for breakout and momentum.
- `walk_forward_exit` takes `trailing_stop_fraction` in [0, 1] and raises outside that range. Divide a YAML `trailing_stop_pct` by 100.

## Helpers in `base.py`

- `run_scanner(context, df)` applies the liquidity and price filter and returns `(shortlist_tracker, df_trimmed)` tagged with `scanner_config_ids`. It doesn't insert rows for missing days or back-fill `close`; `apply_liquidity_filter` does both.
- Shared indicators: `build_regime_filter`, `compute_direction_score`, `compute_rsi`, `fetch_fundamentals`. Regime from the strategy's own universe: `engine/internal_regime.py`.

## Gotchas

- Don't start from `templates/strategy_template.py`. It reads `context["entry_config"]` and an `epoch` column, but configs arrive through `get_entry_config_iterator(context)` and the time column is `date_epoch`. It also skips the `__init__.py` import, and its run command passes a path to `scripts/backtest_main.py`, which reads `CONFIG_FILE` from the environment.

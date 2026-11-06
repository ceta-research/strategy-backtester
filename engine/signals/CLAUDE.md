# engine/signals/

One signal generator per strategy. `static.strategy_type` in a config names the generator. Retired strategies stay registered, because their configs under `strategies/retired/` still use them.

## Add a signal

1. Create `engine/signals/<name>.py` with a class whose `generate_orders(self, context, df_tick_data)` returns a polars DataFrame with the `ORDER_COLUMNS` from `base.py`. `finalize_orders(rows, elapsed)` builds and sorts the frame, and `empty_orders()` covers the no-signal case.
2. Add `@staticmethod build_entry_config(entry_cfg)` and `build_exit_config(exit_cfg)`, each returning `{param: [default, ...]}`. Every current generator has both. Without them `config_loader` falls back to its `_build_entry_config_default` and `_build_exit_config_default`.
3. End the module with `register_strategy("<name>", <Class>)`.
4. Add `from engine.signals import <name>  # noqa: F401` to `__init__.py`. Registration runs only on import. `breakout_pullback.py` lacks this line, so its config fails with `ValueError: Unknown strategy_type 'breakout_pullback'`.
5. Set `static.strategy_type: <name>` in the config.

`docs/ENGINE_STRATEGY_GUIDE.md` section 2 has a current skeleton. Its walk-forward example passes `trailing_stop_pct=`, but the parameter is `trailing_stop_fraction`.

Fast checks for a new generator, from the repo root: `.venv/bin/python -m pytest tests/test_signal_audit.py tests/test_determinism.py -q`.

## Rules the code and tests enforce

- Enter at the next bar's open. `add_next_day_values` adds `next_epoch` and `next_open`. `tests/test_signal_audit.py` fails on `entry_price = row["close"]` unless the file is in its `FLAGGED` or `FALLBACK_OK` set.
- `walk_forward_exit` takes `require_peak_recovery` by keyword with no default: `True` for dip-buy, `False` for breakout and momentum.
- `walk_forward_exit` takes `trailing_stop_fraction` in [0, 1] and raises on a percent. Divide a YAML `trailing_stop_pct` by 100.

## Helpers in `base.py`

- `run_scanner(context, df)` applies the liquidity and price filter and returns `(shortlist_tracker, df_trimmed)` tagged with `scanner_config_ids`. It doesn't insert rows for missing days or back-fill `close`; `apply_liquidity_filter` does both.
- Shared indicators: `build_regime_filter`, `compute_direction_score`, `compute_rsi`, `fetch_fundamentals`. Regime from the strategy's own universe: `engine/internal_regime.py`.

## Gotchas

- Don't start from `templates/strategy_template.py`. It reads `context["entry_config"]` and an `epoch` column, but configs arrive through `get_entry_config_iterator(context)` and the time column is `date_epoch`. It also skips step 4, and its run command passes a path to `scripts/backtest_main.py`, which reads `CONFIG_FILE` from the environment.

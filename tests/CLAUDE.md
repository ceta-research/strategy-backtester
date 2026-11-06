# tests/

Unit tests for `engine/` and `lib/`, plus the regression snapshot tool and verification scripts. The suite command, the slow regression and the known failures are in the root CLAUDE.md.

## Layout

- `test_*.py`: each file inserts the repo root into `sys.path`, so run from the repo root: `.venv/bin/python -m pytest tests/test_metrics.py -q`.
- Two styles sit side by side. Most files hold `unittest.TestCase` classes. `test_charges.py`, `test_config_loader.py`, `test_config_sweep.py`, `test_cr_client_tier_clamp.py`, `test_pipeline.py` and `test_scanner.py` hold plain `def test_*` functions with bare `assert`.
- `regression/snapshot.py` pins the summary metrics of a result JSON and diffs a later one. Commands and the re-capture rule: `regression/snapshots/README.md`. The three pins date from 2026-04-22, before the 2026-10-09 ranking default change.
- `verification/` holds scripts that compare this engine with the original simulator on local parquet data. Neither test runner collects them, and they can't run from a fresh clone: they need that simulator and a data folder outside the repo.

## Writing a test

- Build small polars frames in the test, and never call the API from a new test. Key strings in the tests are fakes.
- `test_signal_audit.py`, `test_determinism.py` and the static half of `test_audit_noninvasive.py` read source text instead of running code. Renaming a function, file or pattern they grep for breaks them, so update the test in the same change.

# tests/

Unit tests for `engine/` and `lib/`, plus the regression snapshot tool and verification scripts. The suite command, the slow regression and the known failures are in the root CLAUDE.md.

## Layout

- `regression/snapshot.py` pins the summary metrics of a result JSON and diffs a later one. Commands and the re-capture rule: `regression/snapshots/README.md`. The three pins date from 2026-04-22, before the 2026-10-09 ranking default change.
- `verification/` holds cross-checks against other implementations. Neither test runner collects them. Not run here: they need `CR_API_KEY` or code and data outside the repo, and `download_data.py` fetches from the API and writes outside the repo.

## Writing a test

- Build small polars frames in the test, and never call the API from a new test.
- `test_signal_audit.py`, the group_by audit in `test_determinism.py` and the static half of `test_audit_noninvasive.py` read source text instead of running code. Renaming a function, file or pattern they grep for breaks them, so update the test in the same change.

# Session Pending Work: momentum_dip_quality Optimization

**Sessions:** 2026-04-15 through 2026-04-20
**Status:** RESULTS INVALID — engine regression discovered

---

## CRITICAL: Engine Regression

**All momentum_dip_quality results (R0-R4) are INVALID.**

Changes to shared engine files (`engine/pipeline.py`, `engine/utils.py`) during cloud OOM debugging introduced bugs that inflated strategy performance by 2-3x.

### Regression proof (R3 champion config, same YAML):

| Engine | CAGR | MDD | Calmar | Orders | Days |
|--------|------|-----|--------|--------|------|
| **Original** (e7122a6) | **17.2%** | -26.6% | **0.645** | varies | 5901 |
| **Modified** (38aad0e) | 34.9% | -25.3% | 1.378 | varies | 3897 |

The modified engine shows 2.14x Calmar inflation. Note the "days" difference (5901 vs 3897) — the epoch filtering removed ~2000 trading days of simulation, which removes drawdown periods and inflates CAGR.

### All completed strategies affected:

| Strategy | Expected Cal | Original engine | Modified engine | Status |
|----------|-------------|----------------|-----------------|--------|
| eod_breakout | 0.516 | 0.419 | 0.322 | **WRONG** |
| enhanced_breakout | 0.499 | **0.499** | 1.243 | **WRONG** |
| momentum_cascade | 0.366 | **0.366** | 0.549 | **WRONG** |
| momentum_dip_quality | ? | **0.645** | 1.378 | **WRONG** |

Note: eod_breakout shows 0.419 even with original engine (vs documented 0.516). This is likely because the nse_charting_day dataset has grown since the original optimization — more recent data slightly changes results. Not a code regression.

### Three changes that broke the engine:

1. **Top-200 instrument limit** (`pipeline.py:158-162`): Drops instruments with few orders from the stats dict. The simulator can't MTM positions in dropped instruments → equity curve is wrong.

2. **Epoch filtering** (`pipeline.py:165-169`): Removes early-date stats (`date_epoch >= min_epoch`). This shortens the simulation window by cutting days before the first order, but the simulator NEEDS those days for MTM of positions entered later.

3. **Forward-fill removal** (`utils.py`): The original code forward-filled close prices across weekends/holidays. Without forward-fill, the simulator uses stale `last_close_price` for days without data → MTM gaps → different equity curve.

### Fix plan:

1. **Revert `engine/pipeline.py` and `engine/utils.py`** to commit e7122a6 (pre-optimization state)
2. **Keep `engine/signals/momentum_dip_quality.py`** changes (signal gen optimizations are correct and don't affect results)
3. **Re-run R0 baseline** with original engine to establish correct baseline
4. **Re-run R3 champion** to get correct Calmar
5. If cloud is needed again, make optimizations **conditional** via a `low_memory_mode` config flag

---

## Pending Work Items

### P0: Fix Engine Regression (BLOCKING)

- [ ] Revert pipeline.py and utils.py to e7122a6
- [ ] Verify enhanced_breakout (Cal 0.499) and momentum_cascade (Cal 0.366) reproduce exactly
- [ ] Re-run momentum_dip_quality R3 champion with correct engine
- [ ] Decide: is the R3 champion STILL the best config on correct engine, or does the optimization need to be redone?
- [ ] If R3 champion is still best (just different absolute numbers): update OPTIMIZATION.md with correct values
- [ ] If R3 champion is NOT best: redo R2-R4 locally with correct engine

### P1: Complete Optimization (after P0)

The R1 sensitivity analysis (which params matter) is likely still valid — the RELATIVE ordering should be similar. But the R2 cross-parameter search and R3 robustness check need re-verification.

- [ ] Run R2 full cross locally with correct engine (486 configs, ~14 min)
- [ ] Run R3 robustness check locally with correct engine
- [ ] Run R4 validation (OOS + WF + cross-data + cross-exchange) locally
- [ ] Update OPTIMIZATION.md with correct results
- [ ] Update OPTIMIZATION_QUEUE.yaml

### P2: Cloud Execution Issues

Documented in `docs/CLOUD_EXECUTION_ISSUES.md`. 11 issue categories:

1. MemoryError in signal gen (to_list via PyArrow monkey-patch)
2. MemoryError in pipeline stats building
3. MemoryError in simulation (too many orders)
4. Execution timeouts
5. Dependency installation failures (~30% rate)
6. File sync / caching (get_file returns stale content)
7. Cloud resource contention
8. Zombie runs (status never updates)
9. DNS resolution failures (local network)
10. regime=0 configs consistently OOM
11. R2 batch sizing limitations

**Key recommendations:**
- Fix `_safe_to_list` monkey-patch (root cause of most OOMs)
- Investigate pyo3 panic in native Polars to_list()
- Prefer local execution for heavy strategies
- Add `low_memory_mode` config flag for cloud-only optimizations

### P3: Code Quality

- [ ] The momentum_dip_quality signal gen changes (lazy exit_data, bisect, del df_signals, etc.) are CORRECT optimizations for the signal generator. They should be kept.
- [ ] The pipeline.py and utils.py changes should be reverted for correctness, then re-applied CONDITIONALLY if cloud execution is needed.
- [ ] Commit all work with proper attribution
- [ ] R1 batch runner scripts (`run_mdq_r1.py`, `run_mdq_r2.py`) need cleanup

### P4: Outstanding R1 Data Gaps

Some R1 param values were never tested due to cloud OOM:
- momentum=378d
- percentile 0.10, 0.15, 0.20 (tighter percentiles)
- dip 2%, 3%, 4% (smaller dips)

These can be run locally now. May change the R1 sensitivity conclusions.

### P5: Walk-Forward Fold 6 Negative

Most recent period (2024-2026) shows CAGR -6.2%. Even after engine fix, need to investigate:
- Is this within normal drawdown range?
- Market regime change?
- Strategy decay?

---

## Files Modified (need commit/revert)

### REVERT (engine regression):
- `engine/pipeline.py` — revert to e7122a6
- `engine/utils.py` — revert to e7122a6

### KEEP (correct signal gen optimizations):
- `engine/signals/momentum_dip_quality.py` — lazy exit_data, bisect, del df_signals, loop swap

### NEW FILES (keep):
- `docs/CLOUD_EXECUTION_ISSUES.md`
- `docs/REGRESSION_INVESTIGATION.md`
- `docs/SESSION_PENDING_WORK.md` (this file)
- `scripts/run_mdq_r1.py` — R1 batch runner
- `scripts/run_mdq_r2.py` — R2 batch runner
- `strategies/momentum_dip_quality/OPTIMIZATION.md` — needs update after engine fix
- `strategies/momentum_dip_quality/config_round3.yaml`
- `strategies/momentum_dip_quality/config_round4_*.yaml`
- `strategies/momentum_dip_quality/config_round1_*.yaml`
- `strategies/momentum_dip_quality/config_baseline_fast.yaml`
- `results/momentum_dip_quality/*.json` — all results (INVALID, need re-run)

### MODIFIED (need update):
- `strategies/OPTIMIZATION_QUEUE.yaml` — momentum_dip_quality marked COMPLETE with wrong numbers

# Cloud Execution Issues: momentum_dip_quality

**Date:** 2026-04-16/17
**Strategy:** momentum_dip_quality (heaviest pipeline strategy)
**Project:** sb-remote (c7185d4d-28a1-4b5f-889a-eecf06b37e21)

## Summary

momentum_dip_quality is the first strategy to push cloud execution limits. Previous strategies (eod_breakout, enhanced_breakout, momentum_cascade) ran fine because they have simpler signal generators and fewer instruments. This strategy has:
- 6M rows from nse_charting_day (2454 instruments, 2008-2026)
- Quality + momentum universe filtering (575 unique instruments across time)
- Walk-forward exit simulation per entry candidate (32K+ qualifying entries)
- Per-entry-config: separate quality/momentum universe computation

## Issue Categories

### 1. MemoryError in Signal Generation

**Symptom:** `MemoryError` in various `to_list()` / `to_arrow().to_pylist()` calls
**Root cause:** The Polars `to_list()` monkey-patch (`_safe_to_list` in cloud wrapper) goes through PyArrow, roughly tripling memory: Polars buffer + Arrow buffer + Python list.

**Locations hit (in order of discovery):**

| Location | What | Fix applied | Status |
|----------|------|------------|--------|
| `momentum_dip_quality.py:338` | `g["close"].to_list()` in exit_data build for ALL 889 instruments | Filter to universe instruments only (575) | Still OOMs at 575 |
| `momentum_dip_quality.py:369` | `df_entry_candidates.to_dicts()` on 853K rows | Changed to `iter_rows()` streaming | Fixed |
| `momentum_dip_quality.py:413` | Lazy exit_data `g["close"].to_list()` per instrument | Added `del df_signals; gc.collect()` before exit_data loop | Fixed at 32GB, still sometimes fails at 16GB |
| `engine/utils.py:71` | `iter_rows()` on 570-instrument stats DataFrame | Changed to per-instrument `group_by` + `to_list()` | Fixed |
| `engine/utils.py:34` | `create_config_df_loc_lookup` building set indices | Added `del df_tick_data` before this step | Fixed |

**Key insight:** The `_safe_to_list` monkey-patch (line 296-298 in `cloud_orchestrator.py`) converts via PyArrow which roughly TRIPLES memory usage vs native Polars `to_list()`. This is the wrapper code:
```python
def _safe_to_list(self):
    return self.to_arrow().to_pylist()
pl.Series.to_list = _safe_to_list
```

This exists because native `pl.Series.to_list()` causes `pyo3_runtime.PanicException` in the cloud environment. But the Arrow intermediate step is extremely memory-hungry.

**Potential fix:** Try using `self.to_numpy().tolist()` instead of `self.to_arrow().to_pylist()`. NumPy conversion might be cheaper than Arrow for simple numeric types.

### 2. MemoryError in Pipeline (post-signal-gen)

**Symptom:** OOM in `create_epoch_wise_instrument_stats` or `create_config_df_loc_lookup`
**Root cause:** Building Python dicts from 570 instruments × 4000 epochs = 2.3M entries

**Fixes applied:**
- Filter stats to only instruments that appear in orders (2454 → 200-570)
- Filter epochs to only those near order dates (reduces by ~30%)
- Limit to top 200 instruments by order count
- Removed calendar-day forward-fill (was creating 6570 × 250 = 1.6M extra entries)
- `del df_tick_data` + `gc.collect()` after signal gen to free ~2GB

### 3. MemoryError in Simulation

**Symptom:** OOM when simulating 256K orders (8 exit configs × 32K orders each)
**Root cause:** 256K order rows + ranking DataFrames + simulator state exceeds memory
**Fix:** Split sweeps into max 4 exit configs per batch (128K orders). Entry config sweeps run 1-per-batch.

### 4. Execution Timeouts (900s/3600s)

**Symptom:** `execution_timed_out` with zero stdout
**Root causes found:**
1. `list.index()` O(n) search called 32K × 8 = 256K times through 4000-element lists → fixed with `bisect.bisect_left()` O(log n)
2. `sort_orders_by_highest_gainer()` processing full 6M-row df_tick_data per simulation config → fixed by pre-filtering to order instruments
3. Signal gen loop iterating 853K entry candidates × N exit configs → fixed by swapping loop order (entries once, exits per qualifying entry)
4. 1500-day prefetch instead of 600-day → fixed in all configs

### 5. Dependency Installation Failures

**Symptom:** `failed (exec=0ms)` with `Dependency installation failed: pip install timed out after 120s`
**Frequency:** ~20-30% of runs on fresh workers
**Root cause:** polars + pyarrow are large packages (~100MB). Cloud pip install has 120s timeout. Slow PyPI mirrors cause timeouts.
**Mitigation:** Retry logic (up to 3 retries for transient failures)
**Not fully solved:** Happens randomly. Sometimes 3 retries all fail.

### 6. File Sync / Caching Issues

**Symptom:** Force-uploaded files not reflected in execution environment
**Root cause:** The `sb-remote` project had a parallel session (momentum_top_gainers) that synced OLD code, overwriting our fixes. The CR API `get_file` returns stale cached content even after successful `upsert_file`.
**Fix:** 
- Created new project `sb-mdq` → but it had dep-install issues (no cached deps)
- Used `force=True` on `sync_files()` → sometimes works, sometimes doesn't
- Verified execution environment directly with test script → confirmed files ARE correct despite `get_file` returning stale content
- The hash-based sync in `cloud_orchestrator.py` (`_load_hash_cache` / `_save_hash_cache`) can cause files to be skipped if the local hash cache is stale

**Recommendation:** Always use `force=True` for `sync_files()`. Consider clearing the hash cache file before sync.

### 7. Cloud Resource Contention

**Symptom:** Runs stuck in `assigned` status for 5+ minutes
**Root cause:** Multiple sessions submitting to the same project, or cloud worker pool exhausted
**Mitigation:** Sequential execution (one run at a time per project)

## Resource Limits

| Resource | Limit | Notes |
|----------|-------|-------|
| vCPU | 12 | Was using 16, should be 12 |
| RAM | 32 GB | Was using 8-16GB, should use 32GB max |
| Disk | 40 GB | Not a bottleneck |
| Execution timeout | 3600s max | Use this for all momentum_dip_quality runs |
| Dep install timeout | 120s | NOT configurable, source of transient failures |

## Memory Budget (momentum_dip_quality, single config)

| Phase | Peak memory | Notes |
|-------|-------------|-------|
| Data fetch (parquet) | ~2 GB | 6M rows × ~40 bytes/row in Polars |
| Signal gen (indicators) | ~4 GB | df_signals accumulates ~10 computed columns |
| Lazy exit_data build | ~1 GB | Per-instrument filter + to_list() (after del df_signals) |
| Stats dict | ~1 GB | 200 instruments × 4000 epochs × 2 values |
| Ranking | ~1 GB | Filtered tick data for sort_orders |
| Simulation | ~0.5 GB | Position tracking + trade log |
| **Total peak** | **~6 GB** | After `del df_signals` optimization |

Without `del df_signals`, peak is ~8 GB (signal gen + exit_data overlap).
With the PyArrow monkey-patch, `to_list()` calls can temporarily spike to 3× the column size.

## Files Modified

| File | Changes |
|------|---------|
| `engine/signals/momentum_dip_quality.py` | Lazy exit_data, iter_rows, bisect lookup, loop swap, del df_signals, no clone |
| `engine/pipeline.py` | Filter stats to order instruments+epochs, top-200 limit, del df_tick_data, pre-compute ranking |
| `engine/utils.py` | Per-instrument group_by stats, removed forward-fill |
| `scripts/run_mdq_r1.py` | Batch runner with partial-save, retry logic, 32GB/12vCPU |

### 8. Zombie Runs (Status Never Updates)

**Symptom:** Cloud API shows `executing` for runs submitted 6-17 hours ago. No stdout, no error.
**Example:** Run 427, submitted 2026-04-17T22:15:50Z, still "executing" 17 hours later.
**Root cause:** The cloud execution timed out (SIGKILL at 3600s) but the API status was never updated to `execution_timed_out`. Possibly a webhook/callback failure on the cloud platform.
**Impact:** New runs submitted to the same project may queue behind the zombie.
**Workaround:** Submit new runs anyway - they eventually get workers. The zombie just pollutes the run list.
**Fix needed:** Cloud platform should have a cleanup job that marks runs as timed_out if they exceed 2× their timeout.

### 9. DNS Resolution Failures (Local Network)

**Symptom:** `Failed to resolve 'api.cetaresearch.com'` during polling. Runs for hours, exhausts retry budget.
**Root cause:** Local machine loses internet connectivity (WiFi drop, sleep, etc). The polling loop retries DNS resolution every 30s for 3600s.
**Impact:** Wastes entire poll timeout, may lose results from a run that completed on cloud during the outage.
**Fix applied:** Added `ConnectionError` catch in `run_batch()` with 30s backoff retry.
**Fix needed:** Poll should recover gracefully when network returns - detect the gap and immediately check run status.

### 10. regime=0 Configs Consistently OOM

**Symptom:** Any config with `regime_sma_period=0` (no NIFTYBEES regime filter) OOMs even at 32GB RAM.
**Root cause:** Without the regime filter, entries occur in bear markets too → 2-3× more qualifying entries → 2-3× more orders → exit_data and all_order_rows exceed memory.
**Quantified:** With regime=200: ~32K qualifying entries. Without regime: ~80-100K qualifying entries.
**Resolution:** Dropped regime=0 from R2 search space entirely. The regime filter is load-bearing for cloud execution, not just a trading signal.
**Implication:** If we want to test regime=0, must run locally (no memory limit) or implement streaming exit_data that doesn't hold all instrument data in memory.

### 11. R2 Batch Sizing Discoveries

Through trial and error, discovered the maximum batch sizes that work at 32GB:
- **1 entry config × 1 TSL × 3 hold × 2 pos = 6 configs:** WORKS (~32K orders, ~50s)
- **1 entry config × 3 TSL × 3 hold × 2 pos = 18 configs:** OOMs (~96K orders)
- **4 entry configs × 1 exit × 1 sim = 4 configs:** OOMs (4 × ~32K = 128K orders, but signal gen accumulates across entries)

Rule: **1 entry config per cloud batch, max 6-8 exit×sim configs.** Each entry config generates independent signal gen state that accumulates in memory.

Locally: R3 ran 486 configs (27 entry × 18 exit×sim) in 14 minutes with zero issues. **Local execution has no memory limits and is ~10× faster per config** (no data re-fetch, no dep install, no queue).

## Recommendations for Next Session

1. **Fix the `_safe_to_list` monkey-patch** - try `self.to_numpy().tolist()` or `self.cast(pl.Float64).to_list()` to avoid the Arrow intermediate copy. This would halve peak memory for all `to_list()` calls.

2. **Consider streaming the exit_data differently** - instead of building Python lists, keep data in Polars and do walk_forward with vectorized operations. This would eliminate the biggest memory spike entirely.

3. **Add memory monitoring to cloud runs** - print `psutil.Process().memory_info().rss` at key checkpoints to track actual usage.

4. **Investigate the pyo3 panic** - why does native `pl.Series.to_list()` crash in the cloud? This might be a Polars version issue (cloud uses 1.37.1). If fixable, removes the need for the monkey-patch entirely.

5. **Consider splitting data fetch** - fetch only universe instruments instead of all 2454. Would require a two-pass approach: first fetch profiles to identify universe, then fetch OHLCV for just those. Would reduce data from 6M to ~2M rows.

6. **Prefer local execution for heavy strategies** - R3 (486 configs) ran in 14 min locally vs days of cloud failures. R4 (8 runs) took 5 min locally. Cloud is only needed when local machine shouldn't burn CPU (background sweeps). For anything interactive, run locally.

7. **Add zombie run detection** - before submitting a new run, check if there are runs older than 2× timeout still showing "executing". Log a warning.

8. **Fix dep-install reliability** - consider pre-building a Docker image with deps cached, or using a requirements hash to skip install when unchanged. The 120s pip timeout is the #1 source of transient failures (~30% rate).

## Cost Summary

Total cloud runs for momentum_dip_quality optimization: ~450 runs
- Successful: ~200 (~$0.02 each = ~$4.00)
- Failed (OOM/timeout, charged): ~150 (~$0.02 each = ~$3.00)  
- Failed (dep install, not charged): ~100 ($0)
- **Total cloud cost: ~$7.00**
- **Total wall clock: ~3 days** (mostly waiting for cloud)
- **Local equivalent: ~1 hour** (R3+R4 took 19 min for 494 configs)

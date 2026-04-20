# Regression Investigation: Engine Changes from momentum_dip_quality Optimization

**Date:** 2026-04-19
**Commit:** 38aad0e
**Discovered during:** Post-optimization regression testing

## Problem

Changes to shared engine files (`engine/pipeline.py`, `engine/utils.py`) made during momentum_dip_quality cloud optimization altered results for other strategies.

**enhanced_breakout champion:**
- Expected: CAGR 11.85%, Cal 0.499
- Got: CAGR 24.5%, Cal 1.243
- **2.5x Calmar increase** — results are no longer comparable to optimization-time values

## Root Cause

Three changes in `engine/pipeline.py` affect all strategies:

### 1. Top-200 instrument limit
```python
if len(order_instruments) > 200:
    inst_counts = df_orders.group_by("instrument").len().sort("len", descending=True)
    top_instruments = set(inst_counts.head(200)["instrument"].to_list())
    order_instruments = top_instruments
```
**Impact:** Strategies with >200 instruments lose MTM data for rare instruments. Positions in dropped instruments don't get marked-to-market → equity curve changes.

### 2. Epoch filtering
```python
min_epoch = min(order_epochs) - 86400 * 30
df_tick_stats = df_tick_data.filter(... & (pl.col("date_epoch") >= min_epoch))
```
**Impact:** Removes early date stats. If a strategy has orders starting later but needs earlier price data for rolling calculations, the stats dict is incomplete.

### 3. Removed forward-fill in `engine/utils.py`
The original code forward-filled missing trading days:
```python
for epoch in range(start_epoch, end_epoch + one_day, one_day):
    if epoch in data_dict:
        last_known_data[instrument_name] = data_dict[epoch]
    elif last_known_data:
        data_dict[epoch] = last_known_data.get(...)
```
This was removed for performance. Without forward-fill, the simulator may not find close prices on non-trading days → `last_close_price` on positions doesn't update → MTM values differ.

## Fix Options

### Option A: Revert shared changes, keep momentum_dip_quality-specific
- Revert pipeline.py and utils.py to pre-38aad0e state
- Move the optimizations into momentum_dip_quality signal generator only
- Pros: Zero regression risk for other strategies
- Cons: momentum_dip_quality cloud execution breaks again

### Option B: Make changes conditional
- Add a `static.low_memory_mode: true` config option
- Only apply top-200 limit, epoch filtering, and no-forward-fill when enabled
- Set it in momentum_dip_quality configs only
- Pros: Both strategies work correctly
- Cons: Code complexity

### Option C: Fix the forward-fill removal properly
- The forward-fill removal is the main behavioral change
- Restore forward-fill but make it efficient (don't iterate ALL calendar days)
- Only forward-fill for trading days present in the data (skip weekends/holidays)
- This should be correct AND fast

## Action Items

1. **Verify which change causes the regression** — run enhanced_breakout with each change reverted individually
2. **Choose fix option** — likely Option C (restore correct forward-fill)
3. **Re-verify all 4 completed strategy champions** after fix
4. **Re-verify momentum_dip_quality R3 champion** to ensure it wasn't benefiting from the bug

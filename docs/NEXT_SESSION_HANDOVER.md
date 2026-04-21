# Next-Session Handover — Critical Findings from Audit P1 + Phase 8A

**Source session:** 2026-04-21 (P1 audit execution + Phase 8A bias A/B)
**Audit status:** 17/17 P0 + 50/50 P1 closed. Remaining work is strategy-level, not audit-level.
**Test baseline:** 381 passing at session end (will be higher if concurrent P2 batch commits land).

The items below are the **only critical follow-ups** surfaced by this audit.
Everything else is either already fixed or deliberately deferred as P2/P3.

Read this first in the next session.

---

## 🔴 CRITICAL 1 — momentum_dip_quality champion is invalid

**What we found:** Ran the named champion config
(`strategies/momentum_dip_quality/config_nse_champion.yaml`, 2010-2026,
full NSE via nse_charting provider) with legacy vs honest universe:

| | CAGR | Calmar | Sharpe | MDD | Trades |
|---|---:|---:|---:|---:|---:|
| Legacy (`full_period`) | **+22.71%** | 0.55 | 1.20 | −41.2% | 297 |
| Honest (`point_in_time`) | **+5.08%** | 0.14 | 0.19 | −35.6% | 225 |
| **Δ** | **−17.63pp** | −0.41 | −1.00 | +5.6pp | −72 |

Honest CAGR (5.08%) is **below buy-and-hold NIFTYBEES** (~12-13% over same window).

**Root cause:** `period_universe_set` (static full-period avg_turnover
filter, computed once over 2010-2026) is used as the primary universe
filter. The scanner's per-day output is computed but discarded.

**Full A/B report:** `docs/audit_phase_8a/momentum_dip_quality.md`

### Decision required

Pick one of three paths:

- **Path A — Retire.** Remove from OPTIMIZATION_QUEUE, gate publishing,
  add to `docs/audit_phase_8a/momentum_dip_quality_retirement.md`.
  Cost: ~30 min.
- **Path B — Re-optimize as-is.** Run R1-R4 with `universe_mode:
  "point_in_time"` baked in. 10-30h compute. Probably still underperforms
  index.
- **Path C — Architecture cleanup + re-optimize** (my recommendation).
  Delete `period_universe_set` entirely; use the scanner's
  `scanner_config_ids.is_not_null()` as the universe filter instead.
  Fix the `or "1"` fallback at line 492. THEN re-optimize. Cost: 2-4h
  code + re-opt compute.

### Next session starter commands

```bash
# Option A: immediate retire
cat >> docs/audit_phase_8a/momentum_dip_quality_retirement.md <<EOF
# momentum_dip_quality — audit-retired 2026-04-??
Honest CAGR 5.08% < NIFTYBEES ~12%. See docs/audit_phase_8a/momentum_dip_quality.md.
EOF

# Option C: run the "third variant" A/B first (scanner-only vs period_avg point_in_time)
# to confirm scanner alone is sufficient. ~2-3min compute.
# This decides whether Path C is mechanically viable.
```

**Also pending:** same decision tree applies to `momentum_top_gainers`
(same architecture). Local fixture gave 0pp delta — fixture artifact.
Run with CR API to measure real impact:

```bash
python scripts/measure_bias_impact.py momentum_top_gainers \
  strategies/momentum_top_gainers/config_champion.yaml --provider cr
```

---

## 🔴 CRITICAL 2 — momentum_rebalance same-bar entry (load-bearing bias)

**What we found:** Local fixture A/B (NSE 30-stock, 2020-2021):

| | CAGR | Calmar | Sharpe | Win rate |
|---|---:|---:|---:|---:|
| Legacy (same-bar, `moc_signal_lag_days=0`) | +12.13% | 1.34 | 1.41 | 61.78% |
| Honest (T-1 signal, `=1`) | +6.59% | 0.50 | 0.55 | 58.44% |
| **Δ** | **−5.54pp** | −0.84 | −0.87 | −3.34pp |

**Root cause:** `momentum_return = close[T] / close[T-N] - 1` used for
ranking AND `entry_price = close[T]` for execution — signal and fill
share the same close, not achievable in live MOC trading.

**Full report:** `docs/audit_phase_8a/momentum_rebalance.md`

### Decision required

- Same Path A/B/C options as Critical 1.
- Full NSE A/B (via CR or nse_charting) pending to confirm the 5.54pp
  is representative or understated.

### Next session starter

```bash
python scripts/measure_bias_impact.py momentum_rebalance \
  strategies/momentum_rebalance/config_nse.yaml --provider nse_charting
```

---

## 🟡 CRITICAL 3 — Scanner architecture smell (touches 2 strategies)

**Finding:** `engine.scanner.process` already runs per-bar with dynamic
liquidity/price/gain filters. But `momentum_dip_quality` and
`momentum_top_gainers` compute their OWN static `period_universe_set`
alongside, and then use that as the hard universe filter while
*discarding* the scanner's per-day output.

**Architectural implications:**

1. `period_universe_set` is redundant with scanner — the scanner
   threshold is the same 70M avg_txn. The only difference is
   full-period average (biased) vs rolling (correct).
2. The `or "1"` fallback at line 492 (both strategies) silently
   reassigns scanner config when stocks fail the per-day scanner —
   masking the strategy's reliance on `period_universe_set`.

**Proposed cleanup (Path C above):**

```python
# In the universe filter:
day_data = df_signals.filter(
    (pl.col("date_epoch") == epoch)
    & pl.col("scanner_config_ids").is_not_null()  # ← use scanner output
    & (pl.col("is_quality") == True)
)

# Delete period_universe_set and _universe_at entirely.

# At order emission (line 492):
if row.get("scanner_config_ids") is None:
    continue  # don't emit orders for stocks that didn't pass scanner
```

**Sanity check before committing:** run A/B of
(legacy `period_universe_set`) vs (`scanner_config_ids` only). If
`scanner_config_ids` alone produces ~5% CAGR (close to point-in-time
honest), the cleanup is mechanically sound. If it produces 15-20%
CAGR, the scanner threshold is different from what period_avg was
enforcing and more analysis is needed.

---

## 🟡 CRITICAL 4 — Cross-exchange results invalidated by Phase 3 revisit

**Not a bug to fix — a publishing gate:**

49 cross-exchange result files in `results_v2/` were generated with
pre-Phase-3-revisit charges (flat 0.05%/side fallback instead of the
detailed per-exchange schedules). Impact per exchange:

| Exchange | Old per-side | New per-side | Shift |
|---|---:|---:|---:|
| LSE | 0.05% | 0.55% buy / 0.05% sell | ~10× buy |
| HKSE | 0.05% | ~0.29% symmetric | ~6× |
| KSC | 0.05% | ~0.05% buy / ~0.45% sell | ~9× sell |
| XETRA/JPX/TSX/ASX | 0.05% | ~0.06-0.11% | ~1.2-2× |

**Policy (already committed in `docs/CROSS_EXCHANGE_STALE_RATES.md`):**
do NOT mass-re-run. Re-run on-demand only when content publishing
requires a specific number. ~25-50 hours of compute for the full mass
re-run is not worth the informational value.

### Action needed in next session

- Before publishing ANY blog / video / strategy card that cites a
  non-NSE/US cross-exchange CAGR: re-run that specific cell and
  update the source. Harness command:

  ```bash
  python scripts/measure_bias_impact.py <strategy> <config_path> --provider cr
  ```

  (Note: this runs A/B, but the legacy pass tells you nothing new —
  the new rates are honest.)

---

## 🟢 DOCUMENTED QUIRK — simulator `exit_before_entry` override

**Not critical for next session, but worth knowing.**

`engine/simulator.py:365` in the `exit_before_entry=True` branch
overrides `sim_config["order_value"]` with `current_account_value /
max_positions` — so callers relying on fixed-value sizing get the
wrong behavior under `exit_before_entry`.

Documented in `docs/AUDIT_FINDINGS.md` (Phase 7 P7.6a). Tests work
around it. Not fixed to preserve historical parity. Track as P2.

---

## Infrastructure you can rely on

| Deliverable | Location |
|---|---|
| Bias A/B measurement harness | `scripts/measure_bias_impact.py` |
| Opt-in bias flags | `entry.moc_signal_lag_days` (momentum_rebalance), `entry.universe_mode` (mdq, mtg) |
| Champion regression config | `tests/verification/config_ato_match.yaml` (byte-identical pre/post audit) |
| Audit write-ups | `docs/AUDIT_FINDINGS.md` (canonical log of every fix) |
| Strategy-specific A/B reports | `docs/audit_phase_8a/*.md` |
| Cross-exchange stale-rate inventory | `docs/CROSS_EXCHANGE_STALE_RATES.md` |

## Safety checks before any code change

```bash
# 1. Test suite must be green
source ../.venv/bin/activate && pytest -q

# 2. Champion config must byte-match (CAGR 25.76%, Calmar 1.2792521)
python tests/verification/run_strategy_backtester.py 2>&1 | \
  grep -E "CAGR:|Calmar:"
```

## Suggested session-1 ordering (90 minutes)

1. **~5min** — Read this doc end-to-end, confirm test baseline green
2. **~2min** — Commit or ignore the 10 uncommitted P2-batch files (see session review; they're not audit work from this session)
3. **~15min** — Run CR-provider A/B for `momentum_top_gainers` + `momentum_rebalance` champion configs. Get real deltas.
4. **DECISION GATE** — based on all 3 strategies' deltas:
   - All > 15pp → retire all three, spend compute on other strategies
   - 5-15pp → Path C cleanup + re-optimize winner(s)
   - < 5pp → Path B re-optimize on existing architecture
5. **~15min** — Run Critical 3 "scanner-only" A/B to confirm Path C's architecture is viable
6. **~60min** — Execute chosen path for at least ONE strategy end-to-end (retire with doc, or land architecture cleanup PR, or kick off R1 re-optimization)

## Explicitly deferred (do not touch in next session unless you want to)

- Flipping flag defaults from legacy to honest — wait until all 3
  strategies are resolved
- Cross-exchange mass re-runs — on-demand only per publishing pipeline
- Open P2 items (~49, listed in AUDIT_CHECKLIST with source line refs)
- Open P3 items (~32, mostly hygiene)

## If you have < 30 minutes

Do only Critical 1 path decision (retire momentum_dip_quality or not).
That single decision unblocks the publishing pipeline and sets the
pattern for the other two strategies.

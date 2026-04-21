# Cross-Exchange Results: Stale Rate Warning

**Created:** 2026-04-21 (Phase 8A step 1)
**Scope:** every `*xc*.json` / `*xex*.json` result file under `results_v2/`
(which is gitignored; this sidecar lives in `docs/` for tracked history).

## TL;DR

Every cross-exchange result in this directory was generated BEFORE the
Phase 3 revisit of `engine/charges.py` landed on 2026-04-21 (commit
`ba95a05`). The revisit added detailed per-exchange fee schedules that
materially shift the cost assumptions on non-NSE/US exchanges.

**All reported CAGR / Sharpe / Calmar figures for non-NSE/US exchanges
in these files are overstated.** Do not publish or cite these numbers
without re-running the affected strategy against the current engine.

## What changed

Pre-P3.5 every non-NSE/US exchange used a flat 0.05%/side cost
fallback. Post-P3.5 detailed schedules (using `max(current,
historical)` rates to be conservative) apply:

| Exchange | Pre-P3.5 per-side | Post-P3.5 per-side (approx) | Direction |
|----------|-------------------|-----------------------------|-----------|
| LSE | 0.05% | 0.55% buy / 0.05% sell (UK SDRT 0.5% buy-only) | **~10× buy** |
| HKSE | 0.05% | ~0.29% symmetric (stamp 0.13% + reg + broker) | ~6× |
| KSC | 0.05% | ~0.05% buy / ~0.45% sell (sec tax 0.25%, agri 0.15%) | ~9× sell |
| XETRA | 0.05% | ~0.06% symmetric (no stamp) | ~1.2× |
| JPX | 0.05% | ~0.105% symmetric | ~2× |
| TSX | 0.05% | ~0.105% symmetric | ~2× |
| ASX | 0.05% | ~0.103% symmetric | ~2× |

Exchanges without detailed helpers (SAO, SHH, SHZ, TAI, PAR, etc.)
remain on the 0.05%/side fallback — those results are still
directionally correct but under-priced if the real local rates are
higher. A one-time warning now logs when any fallback exchange is
used (`engine/charges.py` — audit P3.5).

## Affected file inventory (49 files, 6 strategies)

### results_v2/earnings_dip/
_(none — cross-exchange runs not committed for this strategy)_

### results_v2/enhanced_breakout/
- `round4_xc_Canada.json`  → TSX
- `round4_xc_China_SHH.json`  → SHH (fallback, understated)
- `round4_xc_Germany.json`  → XETRA
- `round4_xc_Hong_Kong.json`  → HKSE
- `round4_xc_South_Korea.json`  → KSC
- `round4_xc_Taiwan.json`  → TAI (fallback)
- `round4_xc_UK.json`  → LSE
- `round4_xc_US.json`  → US (unaffected, US schedule unchanged)

### results_v2/eod_breakout/
- `r4v2_xc_hkse.json`, `round4_xc_hong_kong.json`  → HKSE
- `r4v2_xc_ksc.json`, `round4_xc_south_korea.json`  → KSC
- `r4v2_xc_lse.json`, `round4_xc_uk.json`  → LSE
- `r4v2_xc_tsx.json`, `round4_xc_canada.json`  → TSX
- `r4v2_xc_xetra.json`, `round4_xc_germany.json`, `round4_xc_euronext.json`  → XETRA
- `r4v2_xc_par.json`  → PAR (fallback)
- `r4v2_xc_shh.json`, `round4_xc_china_shh.json`  → SHH (fallback)
- `r4v2_xc_shz.json`, `round4_xc_china_shz.json`  → SHZ (fallback)
- `r4v2_xc_tai.json`, `round4_xc_taiwan.json`  → TAI (fallback)
- `r4v2_xc_us.json`  → US (unaffected)

### results_v2/momentum_cascade/
- `round4_xex_Canada.json`  → TSX
- `round4_xex_China_SHH.json`, `round4_xex_China_SHZ.json`  → fallback
- `round4_xex_Euronext.json`  → fallback
- `round4_xex_Germany.json`  → XETRA
- `round4_xex_Hong_Kong.json`  → HKSE
- `round4_xex_South_Korea.json`  → KSC
- `round4_xex_Taiwan.json`  → fallback
- `round4_xex_UK.json`  → LSE

### results_v2/momentum_dip_quality/
- `round4_xc_HKSE.json`  → HKSE
- `round4_xc_KSC.json`  → KSC
- `round4_xc_LSE.json`, `round4_xc_LSE_corrected.json`  → LSE
- `round4_xc_PAR.json`  → fallback
- `round4_xc_SHH.json`, `round4_xc_SHZ.json`  → fallback
- `round4_xc_TAI.json`, `round4_xc_TAI_corrected.json`  → fallback
- `round4_xc_TSX.json`  → TSX
- `round4_xc_US.json`, `round4_xc_US_corrected.json`  → US (unaffected)
- `round4_xc_XETRA.json`  → XETRA

### results_v2/momentum_top_gainers/
_(none — cross-exchange runs not committed for this strategy)_

## Re-run policy

**Do not mass-re-run.** Cross-exchange results are Round 4 robustness
checks — not production decisions. NSE champions are unaffected.

Re-run ONLY when:
1. Publishing content (blog, video, strategy card) that cites a
   specific non-NSE/US result, OR
2. Evaluating whether a strategy's edge generalizes beyond NSE for
   live-trading purposes.

When you do re-run, pull from the current engine (post `ba95a05`).
The numbers will drop materially — that's the intended, honest
outcome.

## Cross-references

- Phase 3 revisit details: `docs/AUDIT_FINDINGS.md` (section
  "Phase 3 revisit — NSE rate + per-exchange schedules").
- Rate constants: `engine/charges.py` (module docstring + named
  constants per exchange).
- Content pipeline warning: add a reminder to
  `ts-content-creator/BLOG_PUBLISHING_ORDER.md` for any wave referencing
  cross-exchange robustness.

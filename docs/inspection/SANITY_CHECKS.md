# Audit sanity checks — Phase 3b

Cross-foots the audit parquets against each other and the simulator. See `scripts/audit_sanity_checks.py` for the checks themselves.

**Overall:** ✅ all hard checks pass

## eod_breakout

_Source: `results/eod_breakout/audit_drill_20260428T124754Z`_

### Counters

- `entry_audit_rows` = `5,637,686`
- `trade_log_audit_rows` = `207,435`
- `simulator_rows` = `1,795`
- `all_clauses_pass_count` = `207,435`
- `is_audit_rows` = `4,949,736`
- `oos_audit_rows` = `687,950`
- `is_sim_trades` = `1,631`
- `oos_sim_trades` = `164`

### Checks

- [OK] **C1 filter_marginals.passed_in_combination ≡ entry_audit AND** — filter_marginals=207,435 vs count(all_clauses_pass)=207,435
- [OK] **C2 entry_audit.clause_scanner_pass <= scanner_snapshot.scanner_pass** — snapshot pass=1,332,458 >= entry_audit pass=1,331,502 (gap=956 = warm-up rows)
- [OK] **C3 trade_log_audit > simulator_trade_log (capacity-constraint)** — audit=207,435 vs simulator=1,795 (ratio 115.6×)
- [OK] **C4 every simulator entry has a matching audit entry** — all 1,795 simulator entries covered
- [WARN] **C5 simulator exit_reasons ⊆ audit exit_reasons** — simulator has reasons not in audit: ['natural'] — 'natural' is the simulator's default when entry_order does not carry exit_reason; the audit's exit_reason is authoritative for this strategy
- [OK] **C6 IS and OOS both populated** — entry_audit: {'OOS': 687950, 'IS': 4949736}; trade_log_audit: {'OOS': 34720, 'IS': 172715}
- [OK] **C7 no NULL period rows** — all rows period-tagged
- [OK] **C8 all_clauses_pass ≡ AND(clause_*)** — AND consistent over 5,637,686 rows, 8 clauses

## eod_technical

_Source: `results/eod_technical/audit_drill_20260428T124832Z`_

### Counters

- `entry_audit_rows` = `5,637,686`
- `trade_log_audit_rows` = `193,334`
- `simulator_rows` = `1,303`
- `all_clauses_pass_count` = `193,691`
- `is_audit_rows` = `4,949,736`
- `oos_audit_rows` = `687,950`
- `is_sim_trades` = `1,187`
- `oos_sim_trades` = `116`

### Checks

- [OK] **C1 filter_marginals.passed_in_combination ≡ entry_audit AND** — filter_marginals=193,691 vs count(all_clauses_pass)=193,691
- [OK] **C2 entry_audit.clause_scanner_pass <= scanner pass_count** — scanner pass_count=1,496,568 >= entry_audit clause_scanner_pass=1,483,115
- [OK] **C3 trade_log_audit > simulator_trade_log (capacity-constraint)** — audit=193,334 vs simulator=1,303 (ratio 148.4×)
- [OK] **C4 every simulator entry has a matching audit entry** — all 1,303 simulator entries covered
- [OK] **C5 simulator exit_reasons ⊆ audit exit_reasons** — audit=['anomalous_drop', 'end_of_data', 'trailing_stop'] sim=['anomalous_drop', 'end_of_data', 'trailing_stop']
- [OK] **C6 IS and OOS both populated** — entry_audit: {'OOS': 687950, 'IS': 4949736}; trade_log_audit: {'IS': 160023, 'OOS': 33311}
- [OK] **C7 no NULL period rows** — all rows period-tagged
- [OK] **C8 all_clauses_pass ≡ AND(clause_*)** — AND consistent over 5,637,686 rows, 5 clauses


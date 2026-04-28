# Audit spot-checks — Phase 3c

Sampled 20 traded entries + 5 capacity-blocked entries per strategy. The full join of audit↔simulator (rows that made it to the simulator) is also summarised. Sampling is deterministic (`random.seed(0)`).

## eod_breakout

_Source: `results/eod_breakout/audit_drill_20260428T124754Z`_

### Audit↔Simulator agreement (full join)

- joined rows: **1,795**
- entry_price match (|Δ|<1e-3, ignores 4-decimal sim truncation): **1,795** (100.00%)
- exit_price match: **1,795** (100.00%)
- exit_epoch match: **1,795** (100.00%)

### 20 stratified entries (audit vs simulator)

| instrument | period | entry_date | audit_entry_price | sim_entry_price | audit_exit_date | sim_exit_date | audit_exit_price | sim_exit_price | audit_exit_reason | sim_exit_reason | sim_pnl_pct | sim_hold_days |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NSE:ALKEM | IS | 2016-06-02 | 1278.1000 | 1278.1000 | 2016-10-03 | 2016-10-03 | 1700.0000 | 1700.0000 | trailing_stop | natural | 33.0099 | 123.0000 |
| NSE:APARINDS | IS | 2022-11-14 | 1509.0000 | 1509.0000 | 2022-11-24 | 2022-11-24 | 1479.8500 | 1479.8500 | trailing_stop | natural | -1.9317 | 10.0000 |
| NSE:ASKAUTOLTD | IS | 2023-12-14 | 295.0500 | 295.0500 | 2024-02-13 | 2024-02-13 | 299.1000 | 299.1000 | trailing_stop | natural | 1.3726 | 61.0000 |
| NSE:ATLANTAELE | OOS | 2026-02-04 | 819.7000 | 819.7000 | 2026-02-16 | 2026-02-16 | 865.0000 | 865.0000 | regime_flip | natural | 5.5264 | 12.0000 |
| NSE:BAJAJFINSV | IS | 2019-01-16 | 650.0000 | 650.0000 | 2019-01-29 | 2019-01-29 | 601.5000 | 601.5000 | regime_flip | natural | -7.4615 | 13.0000 |
| NSE:BERGEPAINT | IS | 2018-12-20 | 283.5000 | 283.5000 | 2018-12-28 | 2018-12-28 | 274.1667 | 274.1667 | regime_flip | natural | -3.2922 | 8.0000 |
| NSE:BLUEJET | IS | 2024-03-19 | 363.9500 | 363.9500 | 2024-04-24 | 2024-04-24 | 383.0000 | 383.0000 | trailing_stop | natural | 5.2342 | 36.0000 |
| NSE:BOMDYEING | IS | 2018-02-09 | 229.2500 | 229.2500 | 2018-02-21 | 2018-02-21 | 284.5000 | 284.5000 | regime_flip | natural | 24.1003 | 12.0000 |
| NSE:CDSL | IS | 2017-08-17 | 163.9250 | 163.9250 | 2017-09-26 | 2017-09-26 | 175.0000 | 175.0000 | trailing_stop | natural | 6.7561 | 40.0000 |
| NSE:DRREDDY | IS | 2010-12-14 | 364.4000 | 364.4000 | 2010-12-23 | 2010-12-23 | 336.0100 | 336.0100 | trailing_stop | natural | -7.7909 | 9.0000 |
| NSE:DWARKESH | IS | 2017-08-08 | 750.0000 | 750.0000 | 2017-08-10 | 2017-08-10 | 533.9200 | 533.9200 | anomalous_drop | natural | -28.8107 | 2.0000 |
| NSE:GUJTHEM | OOS | 2025-10-06 | 422.9000 | 422.9000 | 2025-11-21 | 2025-11-21 | 422.8500 | 422.8500 | trailing_stop | natural | -0.0118 | 46.0000 |
| NSE:KTKBANK | IS | 2012-11-02 | 102.7347 | 102.7347 | 2012-12-14 | 2012-12-14 | 117.8862 | 117.8862 | trailing_stop | natural | 14.7482 | 42.0000 |
| NSE:KTKBANK | IS | 2013-06-07 | 110.5691 | 110.5691 | 2013-06-17 | 2013-06-17 | 103.3999 | 103.3999 | regime_flip | natural | -6.4840 | 10.0000 |
| NSE:LTTS | IS | 2018-05-28 | 1329.7000 | 1329.7000 | 2018-06-28 | 2018-06-28 | 1221.0000 | 1221.0000 | trailing_stop | natural | -8.1748 | 31.0000 |
| NSE:MANAPPURAM | IS | 2019-09-24 | 135.0000 | 135.0000 | 2019-10-04 | 2019-10-04 | 134.3000 | 134.3000 | regime_flip | natural | -0.5185 | 10.0000 |
| NSE:MONTECARLO | IS | 2015-03-13 | 540.0000 | 540.0000 | 2015-03-24 | 2015-03-24 | 478.0500 | 478.0500 | regime_flip | natural | -11.4722 | 11.0000 |
| NSE:NAM-INDIA | IS | 2018-01-15 | 327.0000 | 327.0000 | 2018-01-23 | 2018-01-23 | 295.5000 | 295.5000 | trailing_stop | natural | -9.6330 | 8.0000 |
| NSE:SHANKARA | IS | 2017-05-10 | 720.0000 | 720.0000 | 2017-07-17 | 2017-07-17 | 994.5500 | 994.5500 | trailing_stop | natural | 38.1319 | 68.0000 |
| NSE:SRF | IS | 2019-02-21 | 441.4000 | 441.4000 | 2019-07-10 | 2019-07-10 | 559.7600 | 559.7600 | trailing_stop | natural | 26.8147 | 139.0000 |

### 5 capacity-blocked entries (no simulator counterpart)

| instrument | period | entry_date | exit_date | entry_price | exit_price | exit_reason | entry_close_signal | entry_n_day_high | entry_direction_score |
|---|---|---|---|---|---|---|---|---|---|
| NSE:AEROFLEX | IS | 2024-08-20 | 2024-09-20 | 161.9000 | 184.2400 | trailing_stop | 159.6900 | 159.6900 | 0.7003 |
| NSE:AIAENG | IS | 2024-08-09 | 2024-08-23 | 4638.6000 | 4423.5000 | trailing_stop | 4593.3000 | 4593.3000 | 0.4340 |
| NSE:CEMPRO | IS | 2024-03-05 | 2024-03-13 | 353.0000 | 309.1000 | trailing_stop | 351.4000 | 351.4000 | 0.5022 |
| NSE:PNBHOUSING | IS | 2019-11-20 | 2019-12-03 | 465.8333 | 428.8750 | trailing_stop | 469.4167 | 469.4167 | 0.4125 |
| NSE:SIEMENS | OOS | 2025-05-15 | 2025-06-24 | 3010.0000 | 3109.0000 | trailing_stop | 3006.8000 | 3006.8000 | 0.9193 |

### Manual OHLCV deep-dive recipe

Run these to fetch ±5 bars around each entry epoch:

```bash
python3 scripts/fetch_ohlcv_window.py --instrument NSE:DWARKESH --epoch 1502150400 --window-days 5
python3 scripts/fetch_ohlcv_window.py --instrument NSE:SHANKARA --epoch 1494374400 --window-days 5
```

## eod_technical

_Source: `results/eod_technical/audit_drill_20260428T124832Z`_

### Audit↔Simulator agreement (full join)

- joined rows: **1,303**
- entry_price match (|Δ|<1e-3, ignores 4-decimal sim truncation): **1,303** (100.00%)
- exit_price match: **1,303** (100.00%)
- exit_epoch match: **1,303** (100.00%)

### 20 stratified entries (audit vs simulator)

| instrument | period | entry_date | audit_entry_price | sim_entry_price | audit_exit_date | sim_exit_date | audit_exit_price | sim_exit_price | audit_exit_reason | sim_exit_reason | sim_pnl_pct | sim_hold_days |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NSE:ACI | IS | 2022-11-24 | 468.0000 | 468.0000 | 2023-04-17 | 2023-04-17 | 582.8000 | 582.8000 | trailing_stop | trailing_stop | 24.5299 | 144.0000 |
| NSE:ADANIENT | IS | 2018-07-24 | 166.0194 | 166.0194 | 2018-09-06 | 2018-09-06 | 163.8447 | 163.8447 | anomalous_drop | anomalous_drop | -1.3099 | 44.0000 |
| NSE:AUROPHARMA | IS | 2012-10-15 | 80.4000 | 80.4000 | 2013-02-13 | 2013-02-13 | 90.5500 | 90.5500 | trailing_stop | trailing_stop | 12.6244 | 121.0000 |
| NSE:BLUEJET | OOS | 2025-02-20 | 774.0000 | 774.0000 | 2025-04-03 | 2025-04-03 | 765.0000 | 765.0000 | trailing_stop | trailing_stop | -1.1628 | 42.0000 |
| NSE:BOROSCI | IS | 2024-07-02 | 225.8800 | 225.8800 | 2024-07-15 | 2024-07-15 | 199.1000 | 199.1000 | trailing_stop | trailing_stop | -11.8559 | 13.0000 |
| NSE:CGPOWER | IS | 2021-10-27 | 154.7000 | 154.7000 | 2021-12-21 | 2021-12-21 | 169.7000 | 169.7000 | trailing_stop | trailing_stop | 9.6962 | 55.0000 |
| NSE:DLF | IS | 2013-01-29 | 267.0000 | 267.0000 | 2013-02-18 | 2013-02-18 | 251.0500 | 251.0500 | trailing_stop | trailing_stop | -5.9738 | 20.0000 |
| NSE:GRAVITA | IS | 2010-12-01 | 58.0000 | 58.0000 | 2010-12-10 | 2010-12-10 | 33.1100 | 33.1100 | trailing_stop | trailing_stop | -42.9138 | 9.0000 |
| NSE:HYUNDAI | IS | 2024-11-21 | 1819.9500 | 1819.9500 | 2025-01-23 | 2025-01-23 | 1707.0000 | 1707.0000 | trailing_stop | trailing_stop | -6.2062 | 63.0000 |
| NSE:JINDALPOLY | OOS | 2026-03-11 | 861.0500 | 861.0500 | 2026-03-18 | 2026-03-18 | 1007.3500 | 1007.3500 | end_of_data | end_of_data | 16.9909 | 7.0000 |
| NSE:JISLJALEQS | IS | 2016-09-16 | 99.3000 | 99.3000 | 2016-09-30 | 2016-09-30 | 85.4500 | 85.4500 | trailing_stop | trailing_stop | -13.9476 | 14.0000 |
| NSE:KECL | IS | 2023-05-12 | 118.2500 | 118.2500 | 2023-05-23 | 2023-05-23 | 104.9500 | 104.9500 | trailing_stop | trailing_stop | -11.2474 | 11.0000 |
| NSE:LIQUIDBEES | IS | 2011-09-28 | 1000.0000 | 1000.0000 | 2026-03-18 | 2026-03-18 | 1000.0000 | 1000.0000 | end_of_data | end_of_data | 0.0000 | 5285.0000 |
| NSE:LUPIN | IS | 2013-06-17 | 782.0500 | 782.0500 | 2013-08-12 | 2013-08-12 | 806.0000 | 806.0000 | trailing_stop | trailing_stop | 3.0625 | 56.0000 |
| NSE:MIRZAINT | IS | 2022-05-17 | 192.5000 | 192.5000 | 2022-06-21 | 2022-06-21 | 198.0000 | 198.0000 | trailing_stop | trailing_stop | 2.8571 | 35.0000 |
| NSE:NILKAMAL | IS | 2016-01-25 | 1292.9000 | 1292.9000 | 2016-02-08 | 2016-02-08 | 1251.0000 | 1251.0000 | trailing_stop | trailing_stop | -3.2408 | 14.0000 |
| NSE:PRAJIND | IS | 2018-09-17 | 108.5000 | 108.5000 | 2018-09-24 | 2018-09-24 | 101.6000 | 101.6000 | trailing_stop | trailing_stop | -6.3594 | 7.0000 |
| NSE:PROTEAN | OOS | 2025-02-20 | 1390.0500 | 1390.0500 | 2025-03-18 | 2025-03-18 | 1260.0000 | 1260.0000 | trailing_stop | trailing_stop | -9.3558 | 26.0000 |
| NSE:SASTASUNDR | IS | 2010-10-22 | 92.5000 | 92.5000 | 2010-11-04 | 2010-11-04 | 80.0000 | 80.0000 | trailing_stop | trailing_stop | -13.5135 | 13.0000 |
| NSE:UNIONBANK | IS | 2022-12-14 | 94.4000 | 94.4000 | 2022-12-21 | 2022-12-21 | 84.3500 | 84.3500 | trailing_stop | trailing_stop | -10.6462 | 7.0000 |

### 5 capacity-blocked entries (no simulator counterpart)

| instrument | period | entry_date | exit_date | entry_price | exit_price | exit_reason | entry_close_signal | entry_n_day_high | entry_direction_score |
|---|---|---|---|---|---|---|---|---|---|
| NSE:BIRLACORPN | IS | 2022-09-09 | 2022-09-27 | 1120.1000 | 1005.0000 | trailing_stop | 1110.6500 | 1110.6500 | 0.6527 |
| NSE:BLUEJET | OOS | 2025-12-23 | 2026-01-16 | 554.0000 | 491.0000 | trailing_stop | 553.9500 | 553.9500 | 0.7975 |
| NSE:GAIL | IS | 2022-08-29 | 2023-10-30 | 88.8667 | 118.7500 | trailing_stop | 89.7000 | 89.7000 | 0.5733 |
| NSE:JINDALSAW | IS | 2010-04-06 | 2010-05-10 | 110.5000 | 98.0500 | trailing_stop | 110.0000 | 110.0000 | 0.8828 |
| NSE:M&M | IS | 2017-12-12 | 2018-02-21 | 709.9000 | 716.0000 | trailing_stop | 709.1250 | 709.1250 | 0.6014 |

### Manual OHLCV deep-dive recipe

Run these to fetch ±5 bars around each entry epoch:

```bash
python3 scripts/fetch_ohlcv_window.py --instrument NSE:GRAVITA --epoch 1291161600 --window-days 5
python3 scripts/fetch_ohlcv_window.py --instrument NSE:ACI --epoch 1669248000 --window-days 5
```


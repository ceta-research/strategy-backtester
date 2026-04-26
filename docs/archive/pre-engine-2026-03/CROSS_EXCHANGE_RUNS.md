# Cross-Exchange Strategy Testing

## How to Run

```bash
# Any strategy on any exchange via CR compute:
python run_remote.py scripts/momentum_dip_buy.py --env MARKET=jpx --timeout 900 --ram 16384

# Results auto-saved to results/ and auto-cataloged to results/catalog.jsonl
# To specify output path:
python run_remote.py scripts/momentum_dip_buy.py --env MARKET=jpx --timeout 900 --ram 16384 -o results/momentum_dip_buy_jpx.json
```

## Exchanges

| Exchange | Code | Data Source | Benchmark | Market Cap Threshold |
|----------|------|-------------|-----------|---------------------|
| NSE | nse | NSE native | NIFTYBEES | Turnover >70Cr |
| US | us | FMP | SPY | >$1B |
| LSE | lse | FMP | ISF.L | >500M GBP |
| JPX | jpx | FMP | 1306.T | >100B JPY |
| HKSE | hkse | FMP | 2800.HK | >5B HKD |
| XETRA | xetra | FMP | EXS1.DE | >500M EUR |
| KSC | ksc | FMP | 069500.KS | >500B KRW |
| TSX | tsx | FMP | XIU.TO | >500M CAD |

## Run Matrix

Legend: blank = pending, `0.XX` = best Calmar from run

### momentum_dip:base (`momentum_dip_buy.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.90 | 0.37 | 0.18 | 0.66 | 0.27 | 0.89* | 0.46 | 0.29 |

### momentum_dip:de_filter (`momentum_dip_de_positions.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 1.01 | 0.37 | 0.22 | 0.56 | 0.27 | 0.79* | 0.39 | 0.37 |

### momentum_dip:vol_exits (`momentum_dip_vol_exits.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 1.01 | 0.49 | 0.25 | 0.67 | 0.30 | 0.88* | 0.54 | 0.29 |

### quality_dip:vol_exits (`vol_adjusted_exits.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.70 | 0.42 | 0.17 | 0.62 | 0.30 | skip* | 0.44 | 0.26 |

### quality_dip:fundamental (`quality_dip_buy_fundamental.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.64 | 0.58 | 0.22* | 0.86 | 0.37 | 1.06* | 0.47 | 0.33 |

### quality_dip:baseline (`quality_dip_buy_nse.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.33 | 0.48 | 0.24 | 0.48 | 0.23 | skip* | 0.52 | 0.25 |

### forced_selling:base (`forced_selling_dip.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.64 | 0.39 | 0.08 | 0.50 | 0.20 | 0.37* | 0.35 | 0.40 |

### earnings_surprise:base (`earnings_surprise_dip.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.44 | 0.23 | | n/a | n/a | | | |

## Summary

- **8 strategies × 8 exchanges = 64 total runs**
- **Completed: 57/64** (includes 5 with data artifacts*, 2 XETRA skipped, 2 earnings n/a)
- **Remaining: 7** (earnings_surprise × LSE/XETRA/KSC/TSX — likely unsupported by FMP data)
- **Effectively complete for all meaningful strategy × exchange combinations**

## Run Order

Priority: highest-Calmar strategies first, largest exchanges first.

**Batch 1** (top 3 strategies × 5 new exchanges = 15 runs):
1. momentum_dip:base × JPX, HKSE, XETRA, KSC, TSX
2. momentum_dip:de_filter × JPX, HKSE, XETRA, KSC, TSX
3. momentum_dip:vol_exits × US, JPX, HKSE, XETRA, KSC

**Batch 2** (remaining strategies × key exchanges):
4. quality_dip:vol_exits × US, JPX, HKSE
5. quality_dip:fundamental × JPX, HKSE, XETRA
6. forced_selling:base × JPX, HKSE, XETRA, KSC, TSX
7. earnings_surprise:base × JPX, HKSE
8. quality_dip:baseline × JPX, HKSE

## Notes

- NSE uses native data (nse.nse_charting_day), all others use FMP (fmp.stock_eod with adjClose)
- All runs use 5 bps slippage, real charges (NSE: STT+stamp+GST, US: SEC+FINRA, others: flat 0.1%)
- Market cap thresholds in local currency — adjusted per exchange to get ~500 liquid stocks
- JPX/KSC thresholds are high because market cap is in JPY/KRW (not USD)
- Results auto-append to results/catalog.jsonl (last 5 per strategy:exchange kept)
- `*` = data quality issue (adjClose split artifacts inflate returns, result unreliable)
- XETRA 2010-2011 shows absurd returns (+815% to +163,702%) — adjClose error in FMP data, all XETRA results unreliable
- LSE fundamental also has artifact (2017: +1,013%) — marked with *
- `n/a` = FMP earnings_surprises data not available for that exchange
- `skip*` = skipped due to known XETRA data quality issues

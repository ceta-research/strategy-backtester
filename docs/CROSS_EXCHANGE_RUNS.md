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
| 0.90 | 0.37 | 0.18 | | | | | |

### momentum_dip:de_filter (`momentum_dip_de_positions.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 1.01 | 0.37 | 0.22 | | | | | |

### momentum_dip:vol_exits (`momentum_dip_vol_exits.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 1.01 | | | | | | | |

### quality_dip:vol_exits (`vol_adjusted_exits.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.70 | | | | | | | |

### quality_dip:fundamental (`quality_dip_buy_fundamental.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.64 | 0.58 | | | | | | |

### quality_dip:baseline (`quality_dip_buy_nse.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.33 | 0.48 | | | | | | |

### forced_selling:base (`forced_selling_dip.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.64 | 0.39 | 0.08 | | | | | |

### earnings_surprise:base (`earnings_surprise_dip.py`)

| NSE | US | LSE | JPX | HKSE | XETRA | KSC | TSX |
|:---:|:--:|:---:|:---:|:----:|:-----:|:---:|:---:|
| 0.44 | 0.23 | | | | | | |

## Summary

- **8 strategies × 8 exchanges = 64 total runs**
- **Completed: 16** (NSE: 8, US: 5, LSE: 3)
- **Remaining: 48**

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

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
| ASX | asx | FMP | STW.AX | >500M AUD |
| TAI | tai | FMP | EWT | >10B TWD |

## Run Matrix

Legend: `--` = not applicable, blank = pending, `Cal X.XX` = completed

### momentum_dip:base (`scripts/momentum_dip_buy.py`)

| Exchange | Status | Calmar | CAGR | MDD | Trades |
|----------|--------|--------|------|-----|--------|
| NSE | Cal 0.90 | 0.90 | +26.3% | -29.1% | 236 |
| US | Cal 0.37 | 0.37 | +17.0% | -45.9% | 167 |
| LSE | Cal 0.18 | 0.18 | +5.4% | -30.4% | 223 |
| JPX | | | | | |
| HKSE | | | | | |
| XETRA | | | | | |
| KSC | | | | | |
| TSX | | | | | |

### momentum_dip:de_filter (`scripts/momentum_dip_de_positions.py`)

| Exchange | Status | Calmar | CAGR | MDD | Trades |
|----------|--------|--------|------|-----|--------|
| NSE | Cal 1.01 | 1.01 | +23.7% | -23.3% | 244 |
| US | Cal 0.37 | 0.37 | +14.1% | -37.8% | 345 |
| LSE | Cal 0.22 | 0.22 | +5.2% | -24.0% | 135 |
| JPX | | | | | |
| HKSE | | | | | |
| XETRA | | | | | |
| KSC | | | | | |
| TSX | | | | | |

### forced_selling:base (`scripts/forced_selling_dip.py`)

| Exchange | Status | Calmar | CAGR | MDD | Trades |
|----------|--------|--------|------|-----|--------|
| NSE | Cal 0.64 | 0.64 | +21.4% | -33.6% | 90 |
| US | Cal 0.39 | 0.39 | +5.6% | -14.1% | 66 |
| LSE | Cal 0.08 | 0.08 | +3.0% | -38.1% | 40 |
| JPX | | | | | |
| HKSE | | | | | |
| XETRA | | | | | |
| KSC | | | | | |
| TSX | | | | | |

## Run Order

Run sequentially on CR compute. Priority: highest-Calmar strategies first, largest exchanges first.

1. momentum_dip:base × JPX, HKSE, XETRA, KSC, TSX
2. momentum_dip:de_filter × JPX, HKSE, XETRA, KSC, TSX
3. forced_selling:base × JPX, HKSE, XETRA, KSC, TSX

Total: ~15 runs remaining. Each takes 5-15 min on CR compute.

## Notes

- NSE uses native data (nse.nse_charting_day), all others use FMP (fmp.stock_eod with adjClose)
- All runs use 5 bps slippage, real charges (NSE: STT+stamp+GST, US: SEC+FINRA, others: flat 0.1%)
- Market cap thresholds in local currency — adjusted per exchange to get ~500 liquid stocks
- JPX/KSC/TAI thresholds are high because prices are in JPY/KRW/TWD (not USD)

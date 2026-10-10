"""Pull bhavcopy prices + split dates for the universe members MISSING from charting.
These are the delisted/merged/renamed names whose absence biases the hit rate up.
bhavcopy_historical is survivorship-free (5,072 syms) and the ONLY source for them."""
import sys, json, os, datetime as dt
sys.path.insert(0,'lib')
from cr_client import CetaResearch
sys.path.insert(0,'studies/bluechip_derating/backtest')
D='studies/bluechip_derating/backtest/'
cr=CetaResearch()
uni=json.load(open(D+'universe.json'))
import glob
have=set()
for p in glob.glob(D+'px_*.json'):
    for r in json.load(open(p)): have.add(r['sym'])
miss=sorted(set(r['sym'] for r in uni) - have)
print(f'missing symbols: {len(miss)}'); json.dump(miss, open(D+'miss_syms.json','w'))
inl=','.join("'"+s.replace("'","''")+"'" for s in miss)

def step(n, fn):
    p=D+n
    if os.path.exists(p): print('[skip]',n); return json.load(open(p))
    r=fn(); json.dump(r, open(p,'w')); print(f'[done] {n}: {len(r):,}'); return r

step('miss_px.json', lambda: cr.query(f"""
  SELECT SYMBOL AS sym, date_epoch AS d, CLOSE AS c, HIGH AS h, PREV_CLOSE AS pc
  FROM nse.nse_bhavcopy_historical
  WHERE SERIES='EQ' AND SYMBOL IN ({inl}) AND CLOSE > 0 AND date_epoch >= 1230768000
  ORDER BY SYMBOL, date_epoch
""", timeout=900, limit=400000))

step('miss_splits.json', lambda: cr.query(f"""
  SELECT replace(symbol,'.NS','') AS sym, dateEpoch AS d, numerator AS num, denominator AS den
  FROM fmp.splits_calendar
  WHERE replace(symbol,'.NS','') IN ({inl}) AND numerator>0 AND denominator>0
""", timeout=600, limit=50000))

# fundamentals for the missing names
step('miss_fund.json', lambda: cr.query(f"""
  SELECT replace(symbol,'.NS','') AS sym, fiscalYear AS fy, dateEpoch AS pe_ep,
         dateEpoch + {120*86400} AS avail_ep, eps, netIncome AS ni
  FROM fmp.income_statement WHERE period='FY'
    AND replace(symbol,'.NS','') IN ({inl})
""", timeout=900, limit=100000))
print('SURV PULL COMPLETE')

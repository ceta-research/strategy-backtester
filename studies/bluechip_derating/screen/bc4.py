import sys, json
sys.path.insert(0,'lib')
from cr_client import CetaResearch
cr=CetaResearch()
CUT=1791417600; Y2=CUT-730*86400
syms=[r['sym'] for r in json.load(open('studies/bluechip_derating/screen/bc_live.json'))]
inlist="','".join(syms)
# (a) corporate-action proxy: worst single-session close-to-close move in the 2y window
q1=f"""
WITH b AS (
  SELECT symbol, date_epoch, close,
         LAG(close) OVER (PARTITION BY symbol ORDER BY date_epoch) pc
  FROM nse.nse_charting_day
  WHERE date_epoch<={CUT} AND date_epoch>={Y2} AND symbol IN ('{inlist}')
)
SELECT symbol, round(min(100*(close/pc-1)),2) AS worst_day_pct,
       round(max(100*(close/pc-1)),2) AS best_day_pct
FROM b WHERE pc IS NOT NULL GROUP BY symbol
"""
ca={r['symbol']:r for r in cr.query(q1, verbose=False)}
# (b) earnings: annual revenue + net income, last 3 FY
q2=f"""
SELECT replace(symbol,'.NS','') AS sym, fiscalYear, date,
       revenue, netIncome, eps
FROM fmp.income_statement
WHERE replace(symbol,'.NS','') IN ('{inlist}')
  AND period='FY'
ORDER BY sym, date DESC
"""
fin=cr.query(q2, verbose=False)
json.dump({'ca':ca,'fin':fin}, open('studies/bluechip_derating/screen/bc_fin.json','w'))
print('corp-action rows:', len(ca), '| income rows:', len(fin))
print('fiscalYears present:', sorted({str(f['fiscalYear']) for f in fin})[-6:])
print('\nworst single-session moves (corporate-action suspects):')
for s,r in sorted(ca.items(), key=lambda kv: kv[1]['worst_day_pct'])[:14]:
    print(f"  {s:<13} worst {r['worst_day_pct']:>8.2f}%   best {r['best_day_pct']:>7.2f}%")

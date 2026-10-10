import sys, json, datetime as dt
sys.path.insert(0,'lib')
from cr_client import CetaResearch
cr=CetaResearch()
CUT=1791417600; Y1=CUT-365*86400; Y2=CUT-730*86400
q=f"""
WITH bc AS (
  SELECT replace(symbol,'.NS','') AS sym, any_value(companyName) AS name,
         any_value(industry) AS industry, max(marketCap) AS mcap
  FROM fmp.profile
  WHERE exchange='NSE' AND marketCap IS NOT NULL AND isEtf = false
  GROUP BY 1 HAVING max(marketCap) >= 750000000000      -- >= Rs 75,000 cr
),
px AS (
  SELECT symbol, date_epoch, close, high
  FROM nse.nse_charting_day
  WHERE date_epoch <= {CUT} AND date_epoch >= {Y2}
    AND symbol IN (SELECT sym FROM bc)
),
agg AS (
  SELECT symbol, count(*) AS bars_2y,
         max(CASE WHEN date_epoch >= {Y1} THEN high END) AS hi_1y,
         max(high) AS hi_2y,
         arg_max(date_epoch, high) AS hi_2y_ep,
         arg_max(close, date_epoch) AS last_close,
         max(date_epoch) AS last_ep
  FROM px GROUP BY symbol
)
SELECT b.sym, b.name, b.industry, round(b.mcap/1e7,0) AS mcap_cr, a.bars_2y,
       a.last_close, a.last_ep, a.hi_1y, a.hi_2y, a.hi_2y_ep,
       round(100*(a.last_close/a.hi_1y-1),2) AS off_hi_1y_pct,
       round(100*(a.last_close/a.hi_2y-1),2) AS off_hi_2y_pct,
       round(100*(a.hi_2y/a.last_close-1),2) AS gain_to_hi_2y_pct
FROM bc b JOIN agg a ON a.symbol=b.sym
ORDER BY off_hi_2y_pct ASC
"""
rows=cr.query(q, verbose=False)
json.dump(rows, open('studies/bluechip_derating/screen/bc_px2.json','w'))
full=[r for r in rows if r['bars_2y']>=480]
print(f'universe (mcap>=75k cr): {len(rows)}  | with full 2y history: {len(full)}  | partial: {len(rows)-len(full)}')
print('partial:', ', '.join(sorted(r['sym'] for r in rows if r['bars_2y']<480)) or 'none')
print(f"\n{'sym':<13}{'mcap_cr':>10}{'bars':>6}{'last':>10}{'hi_2y':>10}{'off2y%':>8}{'off1y%':>8}{'upto_hi%':>9}  hi date")
for r in sorted(full,key=lambda x:x['off_hi_2y_pct'])[:24]:
    print(f"{r['sym']:<13}{r['mcap_cr']:>10,.0f}{r['bars_2y']:>6}{r['last_close']:>10.1f}{r['hi_2y']:>10.1f}{r['off_hi_2y_pct']:>8.1f}{r['off_hi_1y_pct']:>8.1f}{r['gain_to_hi_2y_pct']:>9.1f}  {dt.datetime.utcfromtimestamp(r['hi_2y_ep']).date()}")

import sys
sys.path.insert(0,'lib')
from cr_client import CetaResearch
cr=CetaResearch()
# Blue-chip universe: NSE names by market cap (profile marketCap is LOCAL ccy = INR for NSE).
q = """
SELECT p.symbol, p.companyName, p.industry,
       round(p.marketCap/1e7, 0) AS mcap_cr
FROM fmp.profile p
WHERE p.exchangeShortName = 'NSE'
  AND p.marketCap IS NOT NULL
  AND p.isEtf = false
ORDER BY p.marketCap DESC
LIMIT 120
"""
rows=cr.query(q, verbose=False)
print("rows:", len(rows))
for r in rows[:8]: print(r)
import json
json.dump(rows, open('studies/bluechip_derating/screen/bluechips.json','w'))

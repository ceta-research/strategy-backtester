import sys, datetime as dt, statistics as st
sys.path.insert(0,'lib')
from cr_client import CetaResearch
cr=CetaResearch()
WL=sys.argv[1:] or sys.exit('usage: wl1.py SYMBOL [SYMBOL ...]')
inl=','.join(f"'{s}'" for s in WL)
q=f"""
WITH last AS (SELECT max(date_epoch) t FROM nse.nse_charting_day WHERE symbol='NIFTYBEES'),
px AS (SELECT symbol, date_epoch, close, high FROM nse.nse_charting_day
       WHERE symbol IN ({inl},'NIFTYBEES') AND date_epoch >= (SELECT t FROM last) - 800*86400)
SELECT p.symbol,
  arg_max(p.close, p.date_epoch) AS last_close,
  max(CASE WHEN p.date_epoch >= (SELECT t FROM last)-183*86400 THEN p.high END) AS hi_6m,
  arg_max(p.date_epoch, CASE WHEN p.date_epoch >= (SELECT t FROM last)-183*86400 THEN p.high END) AS hi6_ep,
  max(CASE WHEN p.date_epoch >= (SELECT t FROM last)-365*86400 THEN p.high END) AS hi_1y,
  max(p.high) AS hi_2y
FROM px p GROUP BY 1
"""
rows={r['symbol']:r for r in cr.query(q, verbose=False)}
f=lambda e: dt.datetime.fromtimestamp(e, dt.UTC).date()
print(f"{'symbol':<13}{'last':>10}{'6m high':>10}{'to 6m hi':>10}{'1y high':>10}{'to 1y hi':>10}{'to 2y hi':>10}  6m high set")
tot=[]
for s in WL:
    r=rows.get(s)
    if not r: print(f'{s:<13}  NOT FOUND'); continue
    c=r['last_close']; g6=100*(r['hi_6m']/c-1)
    tot.append(g6)
    print(f"{s:<13}{c:>10.2f}{r['hi_6m']:>10.2f}{g6:>9.1f}%{r['hi_1y']:>10.2f}"
          f"{100*(r['hi_1y']/c-1):>9.1f}%{100*(r['hi_2y']/c-1):>9.1f}%  {f(r['hi6_ep'])}")
print(f"\n  gain to the 6-MONTH high: mean {st.mean(tot):+.1f}%   median {st.median(tot):+.1f}%   max {max(tot):+.1f}%   min {min(tot):+.1f}%")
print(f"  names already AT/within 2% of their 6m high: {sum(1 for g in tot if g<=2)}/{len(tot)}")
nb=rows.get('NIFTYBEES')
print(f"  NIFTYBEES: last {nb['last_close']:.2f}, own 6m high {nb['hi_6m']:.2f} -> {100*(nb['hi_6m']/nb['last_close']-1):+.1f}%")

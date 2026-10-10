import sys, json, datetime as dt, collections
sys.path.insert(0,'lib')
from cr_client import CetaResearch
OUT='studies/bluechip_derating/backtest/'
cr=CetaResearch()
# For EVERY NSE split/bonus in fmp.splits_calendar, measure whether nse_charting_day
# shows the RAW ex-date jump (=> unadjusted) or no jump (=> retroactively adjusted).
q = """
WITH ev AS (
  SELECT replace(symbol,'.NS','') AS sym, dateEpoch AS ex_ep, date AS ex_date,
         numerator AS num, denominator AS den
  FROM fmp.splits_calendar
  WHERE symbol LIKE '%.NS' AND numerator > 0 AND denominator > 0
    AND numerator <> denominator
),
prev AS (
  SELECT e.sym, e.ex_ep, e.ex_date, e.num, e.den,
         arg_max(c.close, c.date_epoch) AS c_prev, max(c.date_epoch) AS prev_ep
  FROM ev e JOIN nse.nse_charting_day c
    ON c.symbol = e.sym AND c.date_epoch < e.ex_ep AND c.date_epoch >= e.ex_ep - 15*86400
  GROUP BY 1,2,3,4,5
),
cur AS (
  SELECT e.sym, e.ex_ep,
         arg_min(c.close, c.date_epoch) AS c_cur, min(c.date_epoch) AS cur_ep
  FROM ev e JOIN nse.nse_charting_day c
    ON c.symbol = e.sym AND c.date_epoch >= e.ex_ep AND c.date_epoch <= e.ex_ep + 15*86400
  GROUP BY 1,2
)
SELECT p.sym, p.ex_date, p.ex_ep, p.num, p.den,
       round(p.c_prev,3) AS c_prev, round(c.c_cur,3) AS c_cur,
       round(c.c_cur / p.c_prev, 4)      AS actual_ratio,
       round(p.den / p.num, 4)           AS raw_ratio_if_unadjusted
FROM prev p JOIN cur c ON c.sym = p.sym AND c.ex_ep = p.ex_ep
WHERE p.c_prev > 0 AND c.c_cur > 0
ORDER BY p.ex_ep
"""
rows = cr.query(q, timeout=900, limit=200000, verbose=True)
print('split events matched to charting:', len(rows))
json.dump(rows, open(OUT+'ca_audit.json','w'))

# Classify: UNADJUSTED if actual_ratio is near the raw split ratio; ADJUSTED if near 1.
by_year = collections.defaultdict(lambda: [0,0,0])  # unadj, adj, ambiguous
for r in rows:
    raw, act = r['raw_ratio_if_unadjusted'], r['actual_ratio']
    y = int(r['ex_date'][:4])
    d_raw = abs(act/raw - 1); d_adj = abs(act - 1)
    if d_raw < 0.15 and d_adj > 0.15:   by_year[y][0] += 1
    elif d_adj < 0.15 and d_raw > 0.15: by_year[y][1] += 1
    else:                               by_year[y][2] += 1
print(f"\n{'year':<6}{'UNADJ':>7}{'ADJ':>7}{'ambig':>7}   verdict")
tot=[0,0,0]
for y in sorted(by_year):
    u,a,m = by_year[y]; tot=[tot[i]+[u,a,m][i] for i in range(3)]
    v = 'RAW/unadjusted' if u>a else ('adjusted' if a>u else 'mixed')
    print(f"{y:<6}{u:>7}{a:>7}{m:>7}   {v}")
print(f"{'TOTAL':<6}{tot[0]:>7}{tot[1]:>7}{tot[2]:>7}")

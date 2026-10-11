"""Swas's question: a large cap below its 6-MONTH high — does it get back within 1 year,
and is that worth doing versus just holding the index?

Entry: month-end, large/liquid (point-in-time trailing traded value from bhavcopy).
Exit:  first close >= the 6-month high, else mark at 1 year.
Benchmark: NIFTYBEES over the identical window, every time.
"""
import sys, json, datetime as dt
sys.path.insert(0,'lib')
from cr_client import CetaResearch
cr=CetaResearch()
D='studies/bluechip_derating/backtest/'
q = """
WITH dd AS (
  SELECT max(date_epoch) AS t FROM nse.nse_charting_day
  WHERE symbol='NIFTYBEES' AND date_epoch >= 1293840000
  GROUP BY date_trunc('month', to_timestamp(date_epoch))
),
tv AS (
  SELECT d.t, b.SYMBOL AS sym, sum(b.CLOSE*b.VOLUME) AS tval, count(*) AS nd
  FROM dd d JOIN nse.nse_bhavcopy_historical b
    ON b.date_epoch < d.t - 2*86400 AND b.date_epoch >= d.t - 372*86400
  WHERE b.SERIES='EQ' AND b.VOLUME > 0 AND b.CLOSE > 0
  GROUP BY 1,2
),
uni AS (
  SELECT t, sym FROM (
    SELECT t, sym, row_number() OVER (PARTITION BY t ORDER BY tval DESC) AS rk
    FROM tv WHERE nd >= 180
  ) z WHERE rk <= 150
),
ent AS (
  SELECT u.t, u.sym,
         max(CASE WHEN c.date_epoch = u.t THEN c.close END) AS c0,
         max(c.high) AS hi6,
         count(*) AS nb
  FROM uni u JOIN nse.nse_charting_day c
    ON c.symbol = u.sym AND c.date_epoch BETWEEN u.t - 183*86400 AND u.t
  GROUP BY 1,2
),
e2 AS (SELECT * FROM ent WHERE c0 > 0 AND hi6 > 0 AND nb >= 100),
fwd AS (
  SELECT e.t, e.sym, e.c0, e.hi6,
         min(CASE WHEN c.close >= e.hi6 THEN c.date_epoch END) AS hit_ep,
         arg_max(c.close, c.date_epoch) AS end_c,
         max(c.date_epoch) AS end_ep,
         min(c.close) AS min_c
  FROM e2 e JOIN nse.nse_charting_day c
    ON c.symbol = e.sym AND c.date_epoch > e.t AND c.date_epoch <= e.t + 365*86400
  GROUP BY 1,2,3,4
),
nb AS (
  SELECT d.t,
         max(CASE WHEN n.date_epoch = d.t THEN n.close END) AS n0,
         max(n.high) AS nhi6
  FROM dd d JOIN nse.nse_charting_day n
    ON n.symbol='NIFTYBEES' AND n.date_epoch BETWEEN d.t - 183*86400 AND d.t
  GROUP BY 1
),
nfwd AS (
  SELECT d.t, arg_max(n.close, n.date_epoch) AS n_end
  FROM dd d JOIN nse.nse_charting_day n
    ON n.symbol='NIFTYBEES' AND n.date_epoch > d.t AND n.date_epoch <= d.t + 365*86400
  GROUP BY 1
)
SELECT f.t, f.sym, f.c0, f.hi6,
       round(100*(f.hi6/f.c0 - 1), 3)            AS gap_pct,
       (f.hit_ep IS NOT NULL)                    AS hit,
       f.hit_ep, f.end_ep, f.end_c, f.min_c,
       round(100*(f.min_c/f.c0 - 1), 3)          AS mae_pct,
       round(100*(nb.nhi6/nb.n0 - 1), 3)         AS idx_gap_pct,
       round(100*(nfwd.n_end/nb.n0 - 1), 3)      AS idx_fwd_1y_pct
FROM fwd f JOIN nb ON nb.t = f.t JOIN nfwd ON nfwd.t = f.t
"""
rows=cr.query(q, timeout=900, limit=400000, verbose=True)
print('rows', len(rows))
json.dump(rows, open(D+'sixm.json','w'))

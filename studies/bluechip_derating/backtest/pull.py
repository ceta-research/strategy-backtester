"""Data pull for the blue-chip de-rating backtest. Idempotent: skips any step whose cache exists.

Provider choice is deliberate and follows CLAUDE.md operational_rules.nse_price_data:
  * UNIVERSE  -> nse.nse_bhavcopy_historical traded value (CA-invariant, survivorship-free)
  * PRICE PATH-> nse.nse_charting_day (adjusted, 2011+ only)
"""
import sys, json, os, datetime as dt
sys.path.insert(0,'lib')
from cr_client import CetaResearch

D='studies/bluechip_derating/backtest/'
cr=CetaResearch()
START=1293840000          # 2011-01-01 first decision date (charting adjustment era starts 2011)
PANEL_FROM=1230768000     # 2009-01-01 : 2y lookback for the first decision date
LAG=120*86400             # EPS publication lag off period end (SEBI gives 60d for annuals)
TOPN=150

def step(name, fn):
    p=D+name
    if os.path.exists(p):
        print(f'[skip] {name}', flush=True); return json.load(open(p))
    print(f'[run ] {name}', flush=True)
    r=fn(); json.dump(r, open(p,'w'))
    print(f'[done] {name}: {len(r):,} rows', flush=True); return r

# ---- 1. decision dates -------------------------------------------------
dd = step('dd.json', lambda: cr.query(f"""
  SELECT max(date_epoch) AS t FROM nse.nse_charting_day
  WHERE symbol='NIFTYBEES' AND date_epoch >= {START}
  GROUP BY date_trunc('month', to_timestamp(date_epoch)) ORDER BY 1
""", timeout=600, limit=1000))

# ---- 2. point-in-time universe: trailing-1y traded value ---------------
# TURNOVER is NULL from mid-2024 so use CLOSE*VOLUME, validated identical
# (2023 top-100 overlap 100/100, rank corr 1.0).
uni = step('universe.json', lambda: cr.query(f"""
WITH dd AS (
  SELECT max(date_epoch) AS t FROM nse.nse_charting_day
  WHERE symbol='NIFTYBEES' AND date_epoch >= {START}
  GROUP BY date_trunc('month', to_timestamp(date_epoch))
),
bh AS (
  SELECT SYMBOL AS sym, date_epoch, CLOSE*VOLUME AS tv
  FROM nse.nse_bhavcopy_historical
  WHERE SERIES='EQ' AND VOLUME > 0 AND CLOSE > 0
    AND date_epoch >= {START} - 400*86400
),
tr AS (
  SELECT d.t, b.sym, sum(b.tv) AS tv_1y, count(*) AS days_traded
  FROM dd d JOIN bh b
    ON b.date_epoch < d.t - 2*86400 AND b.date_epoch >= d.t - 372*86400
  GROUP BY 1,2
),
rk AS (
  SELECT t, sym, tv_1y, days_traded,
         row_number() OVER (PARTITION BY t ORDER BY tv_1y DESC) AS rk
  FROM tr WHERE days_traded >= 180
)
SELECT t, sym, rk, round(tv_1y/1e7,1) AS tv_1y_cr FROM rk WHERE rk <= {TOPN}
ORDER BY t, rk
""", timeout=900, limit=200000))

syms = sorted(set(r['sym'] for r in uni))
json.dump(syms, open(D+'syms.json','w'))
print(f'universe symbols: {len(syms)}', flush=True)
inlist = ','.join("'"+s.replace("'","''")+"'" for s in syms)

# ---- 3. daily price panel (charting, adjusted) -------------------------
for y in range(2009, 2027):
    lo = int(dt.datetime(y,1,1,tzinfo=dt.UTC).timestamp())
    hi = int(dt.datetime(y+1,1,1,tzinfo=dt.UTC).timestamp())
    step(f'px_{y}.json', lambda lo=lo,hi=hi: cr.query(f"""
      SELECT symbol AS sym, date_epoch AS d, close AS c, high AS h, low AS l, volume AS v
      FROM nse.nse_charting_day
      WHERE symbol IN ({inlist}) AND date_epoch >= {lo} AND date_epoch < {hi}
        AND close > 0
      ORDER BY symbol, date_epoch
    """, timeout=900, limit=400000))

# ---- 4. benchmark ------------------------------------------------------
step('nifty.json', lambda: cr.query(f"""
  SELECT date_epoch AS d, close AS c, high AS h FROM nse.nse_charting_day
  WHERE symbol='NIFTYBEES' AND date_epoch >= {PANEL_FROM} ORDER BY date_epoch
""", timeout=600, limit=20000))

# ---- 5. fundamentals: FY eps / netIncome, usable at period_end + LAG ---
step('fund.json', lambda: cr.query(f"""
  SELECT replace(symbol,'.NS','') AS sym, fiscalYear AS fy, dateEpoch AS pe_ep,
         dateEpoch + {LAG} AS avail_ep, eps, netIncome AS ni,
         weightedAverageShsOut AS shs
  FROM fmp.income_statement
  WHERE period='FY' AND symbol IN ({','.join("'"+s.replace("'","''")+".NS'" for s in syms)})
  ORDER BY 1,2
""", timeout=900, limit=200000))

# ---- 6. cash dividends (stock leg is price-only; NIFTYBEES reinvests) --
step('divs.json', lambda: cr.query(f"""
  SELECT replace(symbol,'.NS','') AS sym, dateEpoch AS d, adjDividend AS div
  FROM fmp.dividend_calendar
  WHERE symbol IN ({','.join("'"+s.replace("'","''")+".NS'" for s in syms)})
    AND dateEpoch >= {PANEL_FROM} AND adjDividend > 0
  ORDER BY 1,2
""", timeout=900, limit=200000))

# ---- 7. survivorship probe: universe members absent from charting ------
step('surv.json', lambda: cr.query(f"""
WITH u AS (SELECT DISTINCT SYMBOL AS sym FROM nse.nse_bhavcopy_historical WHERE SERIES='EQ'),
ch AS (SELECT DISTINCT symbol AS sym FROM nse.nse_charting_day)
SELECT u.sym, (ch.sym IS NOT NULL) AS in_charting FROM u LEFT JOIN ch ON ch.sym=u.sym
""", timeout=600, limit=20000))

print('PULL COMPLETE', flush=True)

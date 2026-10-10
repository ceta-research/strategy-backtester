"""Fold the survivorship leg into the headline, PER OFF-HIGH BUCKET.

Treatment by classification:
  RENAME / MERGER_SHARE_SWAP / DEMERGER  -> holder made whole via a successor we do not
      have. DROP the episode. CONSERVATIVE: these all showed as WINNERS in the raw leg
      (TATAMOTORS +36.6%, HDFC +21.4%, SRTRANSFIN +38.9%, CADILAHC +58.7%), so dropping
      them LOWERS the corrected result.
  STILL_LISTED_DATA_GAP -> the price fall IS the outcome; keep at the natural last mark,
      no haircut. (This is the right home for RCOM: the ticker survives at ~Rs 2.60, so
      its -95.9% is real, not a delisting artefact. The classifier's "100% recovered"
      referred to the ticker existing, not to the holder.)
  INSOLVENCY_TOTAL_LOSS / DELISTED_PARTIAL_RECOVERY -> keep, terminal value haircut to
      recovery_value_pct of the last traded price.
"""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from surv import *

agent_cls={c['symbol']: c for c in json.load(open(D+'classifications_partial.json'))}
# Fallback for any ticker the fan-out has not returned, from Indian corporate history.
# Marked unverified so the merge can be run as a BOUND both ways.
FALLBACK={
 'TATAMTRDVR': ('RENAME', 100, 'DVR shares converted to ordinary TATAMOTORS shares 2024'),
 'CADILAHC':   ('RENAME', 100, 'Cadila Healthcare renamed Zydus Lifesciences (ZYDUSLIFE) 2022'),
 'CENTURYTEX': ('RENAME', 100, 'Century Textiles renamed Aditya Birla Real Estate 2024'),
 'RELINFRA':   ('STILL_LISTED_DATA_GAP', 100, 'Reliance Infrastructure still listed'),
 'ABAN':       ('STILL_LISTED_DATA_GAP', 100, 'Aban Offshore still listed'),
 'RELCAPITAL': ('INSOLVENCY_TOTAL_LOSS', 0, 'NCLT resolution; equity extinguished'),
}
DROP={'RENAME','MERGER_SHARE_SWAP','DEMERGER'}

def build(unverified_as_failure=False):
    se=surv_episodes(); kept=[]; drops=defaultdict(int)
    for x in se:
        s=x['sym']
        if s in agent_cls:
            cl=agent_cls[s]['classification']; rv=float(agent_cls[s].get('recovery_value_pct') or 0)
        elif s in FALLBACK:
            cl,rv,_=FALLBACK[s]
            if unverified_as_failure and cl in DROP: cl,rv='INSOLVENCY_TOTAL_LOSS',0.0
        else:
            drops['unclassified']+=1; continue
        if cl in DROP: drops[cl]+=1; continue
        x=dict(x)
        if cl in ('INSOLVENCY_TOTAL_LOSS','DELISTED_PARTIAL_RECOVERY') and x['open']:
            x['x_px']=x['x_px']*max(0.0, rv/100.0)
            x['net']=((x['x_px']*(1-COST))/(x['e_px']*(1+COST))-1) if x['x_px']>0 else -1.0
            x['excess']=(x['net']-x['bench']) if x['bench'] is not None else None
        kept.append(x)
    return kept, drops

base=json.load(open(D+'results.json'))['episodes']
for b in base:
    for k in ('net','excess','mae','off'):
        if b.get(k) is not None: b[k]=float(b[k])
    b['open']=(str(b['open'])=='True'); b['days']=int(float(b['days']))

BUCKETS=[('all entries', lambda x: True),
         ('shallow  -25% to -35%', lambda x: -0.35 < x['off'] <= -0.25),
         ('DEEP     <= -35%',      lambda x: x['off'] <= -0.35)]

def table(rows, title):
    print(f'\n{title}')
    print(f"  {'bucket':<24}{'n':>5}{'recov':>7}{'medNet':>9}{'meanNet':>9}{'medExc':>9}{'meanExc':>9}{'beat':>6}{'p10Net':>9}")
    for nm,sel in BUCKETS:
        g=[x for x in rows if sel(x)]
        if len(g)<5: print(f'  {nm:<24}{len(g):>5}  too few'); continue
        done=[x for x in g if not x['open']]
        n=[x['net'] for x in g]; e=[x['excess'] for x in g if x['excess'] is not None]
        print(f"  {nm:<24}{len(g):>5}{100*len(done)/len(g):>6.0f}%"
              f"{100*st.median(n):>+8.1f}%{100*st.mean(n):>+8.1f}%"
              f"{100*st.median(e):>+8.1f}%{100*st.mean(e):>+8.1f}%"
              f"{100*sum(1 for z in e if z>0)/len(e):>5.0f}%{100*np.percentile(n,10):>+8.1f}%")

print('='*104); print('SURVIVORSHIP-CORRECTED RESULTS, PER OFF-HIGH BUCKET'); print('='*104)
table(base, 'BEFORE correction — charting only (253 episodes)')
for lab,flag in [('verified classifications + documented fallbacks', False),
                 ('BOUND: every unverified ticker treated as a TOTAL LOSS', True)]:
    kept,drops=build(flag)
    print(f'\n  [{lab}]  kept {len(kept)} real-failure episodes; dropped {dict(drops)}')
    if kept: print('   kept: ' + ', '.join(sorted(set(x['sym'] for x in kept))))
    table(base+kept, f'AFTER correction — {lab}')
    json.dump([{k:(float(v) if isinstance(v,(np.floating,np.integer)) else v) for k,v in x.items()} for x in kept],
              open(D+f'kept_{"bound" if flag else "verified"}.json','w'), default=str)

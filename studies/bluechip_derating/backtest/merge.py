"""Fold the survivorship leg into the headline, using the per-symbol classifications.

RENAME / MERGER_SHARE_SWAP / DEMERGER / STILL_LISTED_DATA_GAP = a charting COVERAGE gap,
not survivorship. The holder was not harmed, so these episodes must NOT be marked as
losses; they are dropped (their true path is the successor's, which we do not have).

INSOLVENCY_TOTAL_LOSS / DELISTED_PARTIAL_RECOVERY = real survivorship. The episode is
kept and its terminal value is re-marked at recovery_value_pct of the last traded price.
"""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from surv import *

CLS=json.load(open(D+'classifications.json'))
CL={c['symbol']: c for c in CLS}
COVERAGE={'RENAME','MERGER_SHARE_SWAP','DEMERGER','STILL_LISTED_DATA_GAP'}

base=json.load(open(D+'results.json'))['episodes']
for b in base:
    for k in ('net','excess','mae','days'):
        if b.get(k) is not None: b[k]=float(b[k])
    b['open']= (str(b['open'])=='True')
se=surv_episodes()

kept=[]; dropped=defaultdict(int)
for x in se:
    c=CL.get(x['sym'])
    if c is None: dropped['unclassified']+=1; continue
    if c['classification'] in COVERAGE:
        dropped[c['classification']]+=1; continue
    # real survivorship: re-mark the terminal value at the recovered fraction
    if x['open']:
        rv=max(0.0, float(c.get('recovery_value_pct') or 0)/100.0)
        x=dict(x); x['x_px']=x['x_px']*rv
        x['net']=(x['x_px']*(1-COST))/(x['e_px']*(1+COST))-1
        x['excess']=(x['net']-x['bench']) if x['bench'] is not None else None
    kept.append(x)

print(f'survivorship leg: {len(se)} episodes -> kept {len(kept)} real failures')
print(f'  dropped as coverage/rename artefacts: {dict(dropped)}')
if kept:
    print(f'  kept symbols: {sorted(set(x["sym"] for x in kept))}')
    n=[x['net'] for x in kept]; e=[x['excess'] for x in kept if x['excess'] is not None]
    print(f'  kept net   median {100*st.median(n):+.1f}%  mean {100*st.mean(n):+.1f}%  worst {100*min(n):+.1f}%')
    print(f'  kept excess median {100*st.median(e):+.1f}%  mean {100*st.mean(e):+.1f}%')

def agg(rows, label):
    done=[x for x in rows if not x['open']]
    n=[x['net'] for x in rows]; e=[x['excess'] for x in rows if x['excess'] is not None]
    mae=[x['mae'] for x in rows]
    print(f'\n{label}')
    print(f'  episodes {len(rows)}  recovered {len(done)}/{len(rows)} = {100*len(done)/len(rows):.0f}%')
    print(f'  net     median {100*st.median(n):+6.1f}%   mean {100*st.mean(n):+6.1f}%   worst {100*min(n):+6.1f}%')
    print(f'  EXCESS  median {100*st.median(e):+6.1f}%   mean {100*st.mean(e):+6.1f}%   beat idx {100*sum(1 for z in e if z>0)/len(e):.0f}%')
    print(f'  drawdown-first median {100*st.median(mae):+.1f}%  p10 {100*np.percentile(mae,10):+.1f}%')
    return st.mean(e), 100*len(done)/len(rows)

m0,h0 = agg(base, 'BEFORE correction (charting only, 253 episodes)')
m1,h1 = agg(base+kept, 'AFTER  correction (+ real delistings from bhavcopy)')
print(f'\n  >> mean excess {100*m0:+.1f}% -> {100*m1:+.1f}%  ({100*(m1-m0):+.1f}pp)')
print(f'  >> recovery rate {h0:.0f}% -> {h1:.0f}%  ({h1-h0:+.0f}pp)')
json.dump(dict(kept=[{k:(float(v) if isinstance(v,(np.floating,np.integer)) else v) for k,v in x.items()} for x in kept],
               mean_excess_before=m0, mean_excess_after=m1, hit_before=h0, hit_after=h1),
          open(D+'merged.json','w'), default=str)

"""Corrected sample x off-high bucket x time stop. The decision-relevant grid."""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from surv import *
from variants import walk2, BASE, C

agent_cls={c['symbol']: c for c in json.load(open(D+'classifications_partial.json'))}
FALLBACK={'TATAMTRDVR':('RENAME',100),'CADILAHC':('RENAME',100),'CENTURYTEX':('RENAME',100),
          'RELINFRA':('STILL_LISTED_DATA_GAP',100),'ABAN':('STILL_LISTED_DATA_GAP',100),
          'RELCAPITAL':('INSOLVENCY_TOTAL_LOSS',0)}
DROP={'RENAME','MERGER_SHARE_SWAP','DEMERGER'}

def mwalk2(k, max_days=None):
    """Same exit logic as walk2 but on the bhavcopy (missing-name) series."""
    a=MS[k['sym']]; d,c=a['d'],a['c']
    j=np.searchsorted(d,k['t'],'right')
    if j>=len(d): return None
    e_d,e_px=int(d[j]),float(c[j]); fc,fd=c[j:],d[j:]
    lim=e_d+max_days*DAY if max_days else None
    reason='open'; kk=len(fc)-1
    for i in range(len(fc)):
        if fc[i]>=k['hi']: reason='target'; kk=i; break
        if lim is not None and fd[i]>=lim: reason='time'; kk=i; break
    x_d,x_px=int(fd[kk]),float(fc[kk])
    net=(x_px*(1-COST))/(e_px*(1+COST))-1
    bi=np.searchsorted(nb_d,e_d,'left'); bj=np.searchsorted(nb_d,x_d,'right')-1
    bench=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
    return dict(**k, e_d=e_d, x_d=x_d, e_px=e_px, x_px=x_px, open=(reason=='open'),
                reason=reason, days=int((x_d-e_d)//DAY),
                mae=float(fc[:kk+1].min()/e_px-1), net=net, bench=bench,
                excess=(net-bench if bench is not None else None))

# the missing-name candidate set, once
MC=[]
for t in DDATES:
    for s in UNI[t]:
        if s not in MS: continue
        a=MS[s]; d,c,h=a['d'],a['c'],a['h']
        j=np.searchsorted(d,t,'right')-1
        if j<0 or abs(int(d[j])-t)>5*DAY: continue
        w0=t-730*DAY
        sh=MSH[s][(MSH[s]>w0)&(MSH[s]<=t)] if len(MSH[s]) else []
        if len(sh): w0=max(w0,int(max(sh)))
        i0=np.searchsorted(d,w0,'right')
        if j-i0+1<400: continue
        hi=h[i0:j+1].max()
        if hi<=0: continue
        px_t=c[j]
        if px_t/hi-1 > -0.25: continue
        now,then=mlatest_fy(s,t)
        if now is None or now[4] is None or now[4]<=0: continue
        eps=now[3]
        if not eps or eps<=0 or then is None or not then[3] or then[3]<=0: continue
        g=eps/then[3]-1
        if g<=0 or g>3.0: continue
        if px_t/eps>60 or hi/eps>35: continue
        MC.append(dict(t=int(t), sym=s, px=float(px_t), hi=float(hi), off=float(px_t/hi-1),
                       up=float(hi/px_t-1), idx_off=nifty_off_high(t)))

def corrected(max_days=None):
    a=[x for x in (walk2(k, max_days=max_days) for k in C) if x]
    ea=episodes(a)
    b=[x for x in (mwalk2(k, max_days=max_days) for k in MC) if x]
    eb=[]
    for x in episodes(b):
        s=x['sym']
        cl,rv=(agent_cls[s]['classification'], float(agent_cls[s].get('recovery_value_pct') or 0)) \
              if s in agent_cls else FALLBACK.get(s, (None,0))
        if cl is None or cl in DROP: continue
        x=dict(x)
        if cl in ('INSOLVENCY_TOTAL_LOSS','DELISTED_PARTIAL_RECOVERY') and x['open']:
            x['x_px']*= max(0.0, rv/100.0)
            x['net']=((x['x_px']*(1-COST))/(x['e_px']*(1+COST))-1) if x['x_px']>0 else -1.0
            x['excess']=(x['net']-x['bench']) if x['bench'] is not None else None
        eb.append(x)
    return ea+eb

BUCK=[('all   >= -25% off', lambda x: True),
      ('shallow -25..-35%', lambda x: -0.35 < x['off'] <= -0.25),
      ('DEEP      <= -35%', lambda x: x['off'] <= -0.35)]
print('='*120)
print('CORRECTED SAMPLE (charting + real delistings from bhavcopy)  x  off-high bucket  x  exit rule')
print('='*120)
print(f"  {'exit rule':<26}{'bucket':<20}{'n':>5}{'recov':>7}{'medNet':>9}{'meanNet':>9}{'medExc':>9}{'meanExc':>9}{'beat':>6}{'p10':>9}{'medMo':>7}")
for lab, md in [('hold until recovery', None), ('+ 24-month time stop', 730), ('+ 36-month time stop', 1095)]:
    rows=corrected(md)
    for bn, sel in BUCK:
        g=[x for x in rows if sel(x)]
        if len(g)<5: continue
        done=[x for x in g if not x['open']]
        n=[x['net'] for x in g]; e=[x['excess'] for x in g if x['excess'] is not None]
        print(f"  {lab:<26}{bn:<20}{len(g):>5}{100*len(done)/len(g):>6.0f}%"
              f"{100*st.median(n):>+8.1f}%{100*st.mean(n):>+8.1f}%"
              f"{100*st.median(e):>+8.1f}%{100*st.mean(e):>+8.1f}%"
              f"{100*sum(1 for z in e if z>0)/len(e):>5.0f}%{100*np.percentile(n,10):>+8.1f}%"
              f"{st.median([x['days'] for x in g])/30.4:>7.1f}")
    print()

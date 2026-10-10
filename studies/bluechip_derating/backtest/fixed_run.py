"""Full corrected re-run: event study + survivorship + portfolio, on the fixed code."""
import sys, json, statistics as st
from collections import defaultdict, Counter
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import fixed as FX
from fixed import candidates, walk, annx, CA_SH
from study import (S, DDATES, UNI, DV, D, DAY, COST, nb_d, nb_c, f, episodes,
                   LOOKBACK, MIN_BARS, nifty_off_high)

# ---- survivorship leg, split-dates ONLY (no crash loop) ----------------
mpx=json.load(open(D+'miss_px.json')); msp=json.load(open(D+'miss_splits.json'))
mfu=json.load(open(D+'miss_fund.json'))
MS={}; tmp=defaultdict(list)
for r in mpx: tmp[r['sym']].append((r['d'],r['c'],r['h']))
for s,v in tmp.items():
    v.sort(); MS[s]=dict(d=np.array([x[0] for x in v]), c=np.array([x[1] for x in v],float),
                         h=np.array([x[2] for x in v],float))
MSPL=defaultdict(set)
for r in msp: MSPL[r['sym']].add(int(r['d']))
MFU=defaultdict(list)
for r in mfu:
    if r['eps'] is None: continue
    MFU[r['sym']].append((r['pe_ep'], r['avail_ep'], r['fy'], r['eps'], r['ni']))
for s in MFU: MFU[s].sort()
LAST={s:int(a['d'][-1]) for s,a in MS.items()}

def mfy(s,t):
    av=[x for x in MFU.get(s,[]) if x[1]<=t]
    if not av: return None,None
    now=av[-1]; then=[x for x in av if x[2]==now[2]-2]
    return now,(then[-1] if then else None)

def miss_cands(off_thr=-0.25):
    out=[]
    for t in DDATES:
        for s in UNI[t]:
            a=MS.get(s)
            if a is None: continue
            d,c,h=a['d'],a['c'],a['h']
            j=np.searchsorted(d,t,'right')-1
            if j<0 or abs(int(d[j])-t)>5*DAY: continue
            full0=t-LOOKBACK
            iF=np.searchsorted(d, full0,'right')
            if j-iF+1 < MIN_BARS: continue          # FULL window, as in the fix
            w0=full0
            sp=[x for x in MSPL.get(s,()) if full0 < x <= t]
            if sp: w0=max(sp)                        # bhavcopy IS unadjusted: real splits only
            i0=np.searchsorted(d,w0,'right')
            if j<i0: continue
            hi=h[i0:j+1].max()
            if hi<=0: continue
            px_t=c[j]
            if px_t/hi-1 > off_thr: continue
            now,then=mfy(s,t)
            if now is None or now[4] is None or now[4]<=0: continue
            eps=now[3]
            if not eps or eps<=0 or then is None or not then[3] or then[3]<=0: continue
            g=eps/then[3]-1
            if g<=0 or g>3.0: continue
            if px_t/eps>60 or hi/eps>35: continue
            out.append(dict(t=int(t), sym=s, px=float(px_t), hi=float(hi),
                            off=float(px_t/hi-1), up=float(hi/px_t-1), idx_off=nifty_off_high(t)))
    return out

# classifications: agent results + documented history. WIPED => terminal value 0.
agent={c['symbol']:c for c in json.load(open(D+'classifications_partial.json'))}
DROP={'RENAME','MERGER_SHARE_SWAP','DEMERGER'}
RENAMES={'TATAMTRDVR','CADILAHC','CENTURYTEX','LTI','HDFC','IDFC','AMARAJABAT',
         'SRTRANSFIN','TATAMOTORS','L&TFH','MCDOWELL-N','MOTHERSUMI','TATAGLOBAL',
         'MINDTREE','INFRATEL','INFOSYSTCH','SESAGOA','UNIPHOS','BAJAUTOFIN','KOTAKMAH'}
WIPED={'DHFL','HDIL','EDUCOMP','RCOM','SINTEX','RELCAPITAL','JPASSOCIAT','IBREALEST',
       'JETAIRWAYS','ARSSINFRA','ORIENTBANK','RELIGARE'}   # NCLT / equity extinguished

def surv_eps(max_days=None, wipe=True):
    trs=[]
    for k in miss_cands():
        s=k['sym']
        if s in RENAMES: continue
        if s in agent and agent[s]['classification'] in DROP: continue
        x=walk(k, max_days=max_days, prices=MS, divs=False)
        if x is None: continue
        if wipe and x['open'] and s in WIPED:
            x['x_px']=0.0; x['net']=-1.0
            x['excess']=(x['net']-x['bench']) if x['bench'] is not None else None
        trs.append(x)
    return episodes(trs)

BUCK=[('all   >= -25%', lambda x: True),
      ('shallow -25..-35%', lambda x: -0.35 < x['off'] <= -0.25),
      ('DEEP      <= -35%', lambda x: x['off'] <= -0.35)]

print('='*122)
print('CORRECTED EVENT STUDY  (MIN_BARS bug fixed · real CA dates · survivorship leg uncensored)')
print('='*122)
print(f"  {'exit rule':<22}{'bucket':<20}{'n':>5}{'recov':>7}{'medNet':>9}{'medAnnEx':>10}{'meanAnnEx':>11}{'beat':>6}{'p10Net':>9}{'medMo':>7}")
for lab, md in [('hold until recovery', None), ('+ 24-month stop', 730), ('+ 36-month stop', 1095)]:
    ch=episodes([x for x in (walk(k, max_days=md) for k in candidates()) if x])
    sv=surv_eps(md)
    rows=ch+sv
    for bn, sel in BUCK:
        g=[x for x in rows if sel(x)]
        if len(g)<5: continue
        done=[x for x in g if not x['open']]
        n=[x['net'] for x in g]
        v=[a for a in (annx(x) for x in g) if a is not None]
        print(f"  {lab:<22}{bn:<20}{len(g):>5}{100*len(done)/len(g):>6.0f}%"
              f"{100*st.median(n):>+8.1f}%{100*st.median(v):>+9.1f}%{100*st.mean(v):>+10.1f}%"
              f"{100*sum(1 for z in v if z>0)/len(v):>5.0f}%{100*np.percentile(n,10):>+8.1f}%"
              f"{st.median([x['days'] for x in g])/30.4:>7.1f}")
    if md is None:
        print(f"      [survivorship leg: {len(sv)} episodes, "
              f"{len(set(x['sym'] for x in sv))} symbols: {', '.join(sorted(set(x['sym'] for x in sv))[:14])}]")
        print(f"      [charting leg: {len(ch)} episodes]")
    print()
json.dump({'n_ch':len(episodes([x for x in (walk(k) for k in candidates()) if x])),
           'n_sv':len(surv_eps())}, open(D+'fixed_counts.json','w'))

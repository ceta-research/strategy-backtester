"""Survivorship correction: run the SAME rule over the 112 universe members that are
absent from charting, using bhavcopy (unadjusted, survivorship-free).

Splits are masked from fmp.splits_calendar and the same level-shift detector runs, but
these prices are unadjusted so this leg is a BOUND, not a precise measurement."""
import sys, json, datetime as dt, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from report import *      # brings S, DDATES, UNI, NB_RET, nb_d, nb_c, COST, f, DAY, pct ...

mpx=json.load(open(D+'miss_px.json')); msp=json.load(open(D+'miss_splits.json'))
mfu=json.load(open(D+'miss_fund.json'))
MS={}
t=defaultdict(list)
for r in mpx: t[r['sym']].append((r['d'], r['c'], r['h']))
for s,v in t.items():
    v.sort(); MS[s]=dict(d=np.array([x[0] for x in v]), c=np.array([x[1] for x in v],float),
                         h=np.array([x[2] for x in v],float))
SPL=defaultdict(set)
for r in msp: SPL[r['sym']].add(int(r['d']))
MFU=defaultdict(list)
for r in mfu:
    if r['eps'] is None: continue
    MFU[r['sym']].append((r['pe_ep'], r['avail_ep'], r['fy'], r['eps'], r['ni']))
for s in MFU: MFU[s].sort()

# level shifts on the unadjusted series: any split date, plus the same demerger detector
MSH={}
for s,a in MS.items():
    d,c=a['d'],a['c']
    out=set(int(x) for x in d if any(abs(int(x)-sp)<=4*DAY for sp in SPL.get(s,())))
    if len(c)>=3:
        r=np.concatenate([[0.0], c[1:]/c[:-1]-1])
        for i in np.where(r<=-0.08)[0]:
            if NB_RET.get(int(d[i]),0.0) <= -0.04: continue
            j=min(i+5,len(c)-1); back=c[i+1:j+1].max() if j>i else c[i]
            if back >= c[i] + 0.5*(c[i-1]-c[i]): continue
            out.add(int(d[i]))
    MSH[s]=np.array(sorted(out),dtype=np.int64)

LAST={s:int(a['d'][-1]) for s,a in MS.items()}
DATA_END=max(int(nb_d[-1]), max(LAST.values()))
def delisted(s):   # stopped printing well before the data ends => gone
    return LAST[s] < DATA_END - 200*DAY

def mlatest_fy(s,t):
    av=[x for x in MFU.get(s,[]) if x[1]<=t]
    if not av: return None,None
    now=av[-1]; then=[x for x in av if x[2]==now[2]-2]
    return now,(then[-1] if then else None)

def surv_episodes(off_thr=-0.25, pe_now_max=60.0, pe_need_max=35.0):
    cands=[]
    for t in DDATES:
        for s in UNI[t]:
            a=MS.get(s)
            if a is None: continue
            d,c,h=a['d'],a['c'],a['h']
            j=np.searchsorted(d,t,'right')-1
            if j<0 or abs(int(d[j])-t) > 5*DAY: continue   # bhavcopy epoch offset tolerance
            w0=t-730*DAY
            sh=MSH[s][(MSH[s]>w0)&(MSH[s]<=t)] if len(MSH[s]) else []
            if len(sh): w0=max(w0,int(max(sh)))
            i0=np.searchsorted(d,w0,'right')
            if j-i0+1 < 400: continue
            hi=h[i0:j+1].max()
            if hi<=0: continue
            px_t=c[j]
            if px_t/hi-1 > off_thr: continue
            now,then=mlatest_fy(s,t)
            if now is None or now[4] is None or now[4]<=0: continue
            eps=now[3]
            if not eps or eps<=0: continue
            if then is None or not then[3] or then[3]<=0: continue
            g=eps/then[3]-1
            if g<=0 or g>3.0: continue
            if px_t/eps > pe_now_max or hi/eps > pe_need_max: continue
            cands.append(dict(t=int(t), sym=s, px=float(px_t), hi=float(hi),
                              off=float(px_t/hi-1), up=float(hi/px_t-1), idx_off=nifty_off_high(t)))
    # walk forward on bhavcopy
    trs=[]
    for k in cands:
        a=MS[k['sym']]; d,c=a['d'],a['c']
        j=np.searchsorted(d,k['t'],'right')
        if j>=len(d): continue
        e_d,e_px=int(d[j]),float(c[j])
        fc,fd=c[j:],d[j:]
        hit=np.where(fc>=k['hi'])[0]
        if len(hit): kk=int(hit[0]); x_d,x_px,op=int(fd[kk]),float(fc[kk]),False
        else:        kk=len(fc)-1;   x_d,x_px,op=int(fd[kk]),float(fc[kk]),True
        mae=float(fc[:kk+1].min()/e_px-1)
        net=(x_px*(1-COST))/(e_px*(1+COST))-1
        gone = op and delisted(k['sym'])
        bi=np.searchsorted(nb_d,e_d,'left'); bj=np.searchsorted(nb_d,x_d,'right')-1
        bench=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
        trs.append(dict(**k, e_d=e_d, e_px=e_px, x_d=x_d, x_px=x_px, open=op, delisted=gone,
                        days=int((x_d-e_d)//DAY), mae=mae, divd=0.0, gross=net, net=net,
                        bench=bench, excess=(net-bench if bench is not None else None)))
    return episodes(trs)

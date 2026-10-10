"""Does capping the hold rescue the rule? The base rule's flaw is unbounded holding:
capital sits in UCOBANK(-78%) / IDEA(-82%) forever. Test time stops and stop-losses."""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from report import *

BASE=dict(off_thr=-0.25, pe_now_max=60.0, pe_need_max=35.0, require_eps=True,
          ca_mode='truncate', top_rank=150)
C=candidates(**BASE)
print(f'candidates {len(C)}')

def walk2(k, max_days=None, stop_pct=None):
    """Exit on: target hit | time stop | stop-loss (close through it). Whichever first."""
    a=S[k['sym']]; d,c=a['d'],a['c']
    j=np.searchsorted(d,k['t'],'right')
    if j>=len(d): return None
    e_d,e_px=int(d[j]),float(c[j]); fc,fd=c[j:],d[j:]
    stop_px = e_px*(1+stop_pct) if stop_pct is not None else None
    lim = e_d + max_days*DAY if max_days else None
    reason='open'; kk=len(fc)-1
    for i in range(len(fc)):
        if fc[i] >= k['hi']:                       reason='target'; kk=i; break
        if stop_px is not None and fc[i] <= stop_px: reason='stop';   kk=i; break
        if lim is not None and fd[i] >= lim:        reason='time';   kk=i; break
    x_d,x_px=int(fd[kk]),float(fc[kk])
    mae=float(fc[:kk+1].min()/e_px-1)
    dvd=sum(v for dd,v in DV.get(k['sym'],[]) if e_d < dd <= x_d)
    net=(x_px*(1-COST)+dvd)/(e_px*(1+COST))-1
    bi=np.searchsorted(nb_d,e_d,'left'); bj=np.searchsorted(nb_d,x_d,'right')-1
    bench=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
    return dict(**k, e_d=e_d, e_px=e_px, x_d=x_d, x_px=x_px, open=(reason=='open'),
                reason=reason, days=int((x_d-e_d)//DAY), mae=mae, divd=float(dvd),
                gross=net, net=net, bench=bench,
                excess=(net-bench if bench is not None else None))

def pf(cands, slots, max_days=None, stop_pct=None, label=''):
    """Portfolio with the same exit rules."""
    by_t=defaultdict(list)
    for c in cands: by_t[c['t']].append(c)
    for t in by_t: by_t[t].sort(key=lambda x:-x['up'])
    dates=[int(x) for x in nb_d if x>=DDATES[0]]; dset=np.array(dates)
    fill_of={}
    for t in DDATES:
        kk=np.searchsorted(dset,t,'right')
        if kk<len(dset): fill_of.setdefault(int(dset[kk]), t)
    pxc={s:{int(d):float(c) for d,c in zip(S[s]['d'],S[s]['c'])} for s in S}
    cash=100.0; pos=[]; curve=[]; nclosed=0; reasons=defaultdict(int)
    def mk(p,day):
        c=pxc[p['sym']].get(day,p['last']); p['last']=c
        dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d']<dd<=day)
        return p['alloc']*(1+((c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1))
    for day in dates:
        keep=[]
        for p in pos:
            c=pxc[p['sym']].get(day)
            why=None
            if c is not None:
                if c >= p['tgt']: why='target'
                elif p['stop'] is not None and c <= p['stop']: why='stop'
                elif p['lim'] is not None and day >= p['lim']: why='time'
            if why:
                dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d']<dd<=day)
                cash += p['alloc']*(1+((c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1))
                nclosed+=1; reasons[why]+=1
            else: keep.append(p)
        pos=keep
        eq=cash+sum(mk(p,day) for p in pos)
        if day in fill_of:
            held={p['sym'] for p in pos}
            for c in by_t.get(fill_of[day],[]):
                if len(pos)>=slots: break
                if c['sym'] in held: continue
                e=pxc[c['sym']].get(day)
                if e is None: continue
                alloc=eq/slots
                if cash < alloc*0.999: break
                cash-=alloc
                pos.append(dict(sym=c['sym'], e_d=day, e_px=e, tgt=c['hi'], alloc=alloc, last=e,
                                stop=(e*(1+stop_pct) if stop_pct is not None else None),
                                lim=(day+max_days*DAY if max_days else None)))
                held.add(c['sym'])
            eq=cash+sum(mk(p,day) for p in pos)
        curve.append((day,eq))
    eqv=np.array([x[1] for x in curve]); dts=[x[0] for x in curve]
    yrs=(dts[-1]-dts[0])/365/DAY
    cagr=(eqv[-1]/eqv[0])**(1/yrs)-1
    mdd=float((eqv/np.maximum.accumulate(eqv)-1).min())
    i0=np.searchsorted(nb_d,dts[0],'left'); i1=np.searchsorted(nb_d,dts[-1],'right')-1
    bh=nb_c[i0:i1+1]; b_cagr=(bh[-1]/bh[0])**(1/yrs)-1
    b_mdd=float((bh/np.maximum.accumulate(bh)-1).min())
    openv=sum(mk(p,dts[-1]) for p in pos)
    print(f"  {label:<34} slots{slots:>3}  CAGR {100*cagr:>6.2f}%  MDD {100*mdd:>7.2f}%  "
          f"Calmar {cagr/-mdd:>4.2f}  vs idx {100*(cagr-b_cagr):>+6.2f}pp  closed {nclosed:>4}  "
          f"open {len(pos):>2} ({100*openv/eqv[-1]:>3.0f}% eq)  "
          f"exits: {dict(reasons)}")
    return cagr, mdd, b_cagr, b_mdd

print('\n'+'='*118); print('EVENT STUDY under capped holds (distinct episodes)'); print('='*118)
print(f"  {'variant':<40}{'eps':>5}{'medNet':>9}{'meanNet':>9}{'medExc':>9}{'meanExc':>9}{'beat':>7}{'medDays':>9}{'worst':>9}")
for nm,kw in [('hold until recovery (base)',{}),
              ('+ 12-month time stop',{'max_days':365}),
              ('+ 18-month time stop',{'max_days':548}),
              ('+ 24-month time stop',{'max_days':730}),
              ('+ 36-month time stop',{'max_days':1095}),
              ('+ 25% stop-loss',{'stop_pct':-0.25}),
              ('+ 24m stop AND 25% stop-loss',{'max_days':730,'stop_pct':-0.25}),
              ('+ 36m stop AND 30% stop-loss',{'max_days':1095,'stop_pct':-0.30})]:
    tt=[x for x in (walk2(k,**kw) for k in C) if x]; ee=episodes(tt)
    net=[x['net'] for x in ee]; ex=[x['excess'] for x in ee if x['excess'] is not None]
    print(f"  {nm:<40}{len(ee):>5}{pct(st.median(net)):>9}{pct(st.mean(net)):>9}"
          f"{pct(st.median(ex)):>9}{pct(st.mean(ex)):>9}"
          f"{100*sum(1 for e in ex if e>0)/len(ex):>6.0f}%{st.median([x['days'] for x in ee]):>9.0f}{pct(min(net)):>9}")

print('\n'+'='*118); print('PORTFOLIO under capped holds'); print('='*118)
for slots in (10,20):
    for nm,kw in [('hold until recovery (base)',{}),
                  ('+ 24-month time stop',{'max_days':730}),
                  ('+ 36-month time stop',{'max_days':1095}),
                  ('+ 25% stop-loss',{'stop_pct':-0.25}),
                  ('+ 24m stop AND 25% stop-loss',{'max_days':730,'stop_pct':-0.25})]:
        pf(C, slots, label=nm, **kw)
    print()

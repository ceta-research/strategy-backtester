"""Portfolio sim on the CORRECTED candidate set, over a COMBINED panel so the book can
actually buy the names that later died. The audit's decisive objection to the old sim was
that portfolio() ran on the charting panel only, so no arm ever held a survivorship name
even though the deepest-first fill would have bought DHFL and HDIL."""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import fixed_run as FR
from fixed import candidates
from study import S, DDATES, DV, D, DAY, COST, nb_d, nb_c, f

MS=FR.MS; WIPED=FR.WIPED; LAST=FR.LAST
PANEL={}
for s,a in S.items(): PANEL[s]={int(d):float(c) for d,c in zip(a['d'],a['c'])}
for s,a in MS.items():
    if s in FR.RENAMES: continue
    PANEL.setdefault(s, {int(d):float(c) for d,c in zip(a['d'],a['c'])})
DATA_END=int(nb_d[-1])

ALL = candidates() + FR.miss_cands()
ALL = [k for k in ALL if k['sym'] in PANEL and k['sym'] not in FR.RENAMES]
print(f'combined candidate signals: {len(ALL)}  ({len(set(k["sym"] for k in ALL))} symbols)', flush=True)

def pf(slots, max_days=None, label='', deep_only=False, start=100.0):
    cs=[k for k in ALL if (k['off']<=-0.35 if deep_only else True)]
    by=defaultdict(list)
    for c in cs: by[c['t']].append(c)
    for t in by: by[t].sort(key=lambda x:-x['up'])        # deepest-first
    dates=[int(x) for x in nb_d if x>=DDATES[0]]; dset=np.array(dates)
    fill={}
    for t in DDATES:
        k=np.searchsorted(dset,t,'right')
        if k<len(dset): fill.setdefault(int(dset[k]), t)
    cash=start; pos=[]; curve=[]; closed=[]; why=defaultdict(int)
    def px(s,day,fallback):
        v=PANEL[s].get(day)
        if v is not None: return v
        # stopped printing: a delisted NCLT name is worth ~0 to a holder
        if s in WIPED and day > LAST.get(s, DATA_END): return 0.0
        return fallback
    def mark(p,day):
        c=px(p['sym'],day,p['last']); p['last']=c
        dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d']<dd<=day)
        return p['alloc']*(1+((c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1))
    for day in dates:
        keep=[]
        for p in pos:
            c=px(p['sym'],day,None)
            r=None
            if c is not None:
                if c>=p['tgt']: r='target'
                elif p['lim'] is not None and day>=p['lim']: r='time'
                elif c==0.0: r='wiped'
            if r:
                dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d']<dd<=day)
                net=(c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1
                cash+=p['alloc']*(1+net); why[r]+=1
                closed.append(dict(sym=p['sym'], net=net, days=(day-p['e_d'])//DAY, why=r))
            else: keep.append(p)
        pos=keep
        eq=cash+sum(mark(p,day) for p in pos)
        if day in fill:
            held={p['sym'] for p in pos}
            for c in by.get(fill[day],[]):
                if len(pos)>=slots: break
                if c['sym'] in held: continue
                e=PANEL[c['sym']].get(day)
                if e is None or e<=0: continue
                alloc=eq/slots
                if cash<alloc*0.999: break
                cash-=alloc
                pos.append(dict(sym=c['sym'], e_d=day, e_px=e, tgt=c['hi'], alloc=alloc,
                                last=e, lim=(day+max_days*DAY if max_days else None)))
                held.add(c['sym'])
            eq=cash+sum(mark(p,day) for p in pos)
        curve.append((day,eq))
    ev=np.array([x[1] for x in curve]); dts=[x[0] for x in curve]
    yrs=(dts[-1]-dts[0])/365/DAY
    cagr=(ev[-1]/ev[0])**(1/yrs)-1
    mdd=float((ev/np.maximum.accumulate(ev)-1).min())
    i0=np.searchsorted(nb_d,dts[0],'left'); i1=np.searchsorted(nb_d,dts[-1],'right')-1
    bh=nb_c[i0:i1+1]; bc=(bh[-1]/bh[0])**(1/yrs)-1
    bm=float((bh/np.maximum.accumulate(bh)-1).min())
    print(f"  {label:<30} slots{slots:>3}  CAGR {100*cagr:>6.2f}%  MDD {100*mdd:>7.2f}%  "
          f"Calmar {cagr/-mdd:>5.2f}  vs idx {100*(cagr-bc):>+6.2f}pp  closed {len(closed):>4}  "
          f"open {len(pos):>2}  exits {dict(why)}")
    return cagr, mdd, bc, bm

i0=np.searchsorted(nb_d,DDATES[0],'left')
bh=nb_c[i0:]; yrs=(int(nb_d[-1])-DDATES[0])/365/DAY
print(f"\n  NIFTYBEES buy & hold, same window: CAGR {100*((bh[-1]/bh[0])**(1/yrs)-1):.2f}%  "
      f"MDD {100*float((bh/np.maximum.accumulate(bh)-1).min()):.2f}%\n")
print('='*140)
print('CORRECTED PORTFOLIO — combined panel, delisted names buyable and marked to ZERO when the equity was extinguished')
print('='*140)
for slots in (10,20):
    pf(slots, None, 'hold until recovery')
    pf(slots, 1095, '+ 36-month time stop')
    pf(slots, 730, '+ 24-month time stop')
    print()
print('  deep-only variants (>= -35% off the high):')
for slots in (10,20):
    pf(slots, None, 'DEEP, hold until recovery', deep_only=True)
    pf(slots, 1095, 'DEEP, + 36-month stop', deep_only=True)

"""FINAL run. fmp.dividend_calendar.adjDividend is CORRUPT for NSE: 53 rows exceed 25% of
the share price, HINDPETRO's 2021-07-08 row reads Rs 1,314.93 against a Rs 189.23 price
(6.9x), and those rows carry 25.3% of all dividend rupee value. HINDPETRO was the single
largest contributor to the deep-bucket edge, so that edge was a data artefact.

Two arms, neither trusting the field:
  NODIV     - price-only stock leg. NIFTYBEES reinvests, so this UNDERSTATES the rule by
              roughly the dividend yield (~1.5%/yr). Conservative, and clean.
  SANITISED - drop only dividend rows worth > 20% of the share price.
"""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import fixed_run as FR, fixed_pf as PF
from fixed import candidates
from study import S, DDATES, DV, D, DAY, COST, nb_d, nb_c, f, episodes

PANEL=PF.PANEL; WIPED=FR.WIPED; LAST=FR.LAST; DATA_END=PF.DATA_END
ALL=PF.ALL

# sanitised dividend map
DV_CLEAN=defaultdict(list); ndrop=0
for s, rows in DV.items():
    ser=PANEL.get(s)
    for ep,v in rows:
        c=None
        if ser:
            ks=[k for k in ser if k<=ep]
            if ks: c=ser[max(ks)]
        if c and c>0 and v/c > 0.20: ndrop+=1; continue
        DV_CLEAN[s].append((ep,v))
print(f'sanitised dividends: dropped {ndrop} corrupt rows', flush=True)

def pf(slots, max_days=None, dmode='nodiv', deep_only=False, label=''):
    dv = {} if dmode=='nodiv' else DV_CLEAN
    cs=[k for k in ALL if (k['off']<=-0.35 if deep_only else True)]
    by=defaultdict(list)
    for c in cs: by[c['t']].append(c)
    for t in by: by[t].sort(key=lambda x:-x['up'])
    dates=[int(x) for x in nb_d if x>=DDATES[0]]; dset=np.array(dates)
    fill={}
    for t in DDATES:
        k=np.searchsorted(dset,t,'right')
        if k<len(dset): fill.setdefault(int(dset[k]), t)
    cash=100.0; pos=[]; curve=[]; nc=0; why=defaultdict(int); inv=[]
    def price(s,day,fb):
        v=PANEL[s].get(day)
        if v is not None: return v
        if s in WIPED and day > LAST.get(s, DATA_END): return 0.0
        return fb
    def mk(p,day):
        c=price(p['sym'],day,p['last']); p['last']=c
        d=sum(v for dd,v in dv.get(p['sym'],[]) if p['e_d']<dd<=day)
        return p['alloc']*(1+((c*(1-COST)+d)/(p['e_px']*(1+COST))-1))
    for day in dates:
        keep=[]
        for p in pos:
            c=price(p['sym'],day,None); r=None
            if c is not None:
                if c>=p['tgt']: r='target'
                elif p['lim'] is not None and day>=p['lim']: r='time'
                elif c==0.0: r='wiped'
            if r:
                d=sum(v for dd,v in dv.get(p['sym'],[]) if p['e_d']<dd<=day)
                cash+=p['alloc']*(1+((c*(1-COST)+d)/(p['e_px']*(1+COST))-1)); nc+=1; why[r]+=1
            else: keep.append(p)
        pos=keep
        mv=sum(mk(p,day) for p in pos); eq=cash+mv
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
            mv=sum(mk(p,day) for p in pos); eq=cash+mv
        curve.append((day,eq)); inv.append(mv/eq if eq>0 else 0)
    ev=np.array([x[1] for x in curve]); dts=[x[0] for x in curve]
    yrs=(dts[-1]-dts[0])/365/DAY
    cagr=(ev[-1]/ev[0])**(1/yrs)-1
    mdd=float((ev/np.maximum.accumulate(ev)-1).min())
    i0=np.searchsorted(nb_d,dts[0],'left'); i1=np.searchsorted(nb_d,dts[-1],'right')-1
    bh=nb_c[i0:i1+1]; bc=(bh[-1]/bh[0])**(1/yrs)-1
    print(f"  {label:<34}{slots:>3}  CAGR {100*cagr:>6.2f}%  MDD {100*mdd:>7.2f}%  "
          f"Calmar {cagr/-mdd:>5.2f}  vs idx {100*(cagr-bc):>+6.2f}pp  closed {nc:>4}  "
          f"invested {100*np.mean(inv):>3.0f}%  exits {dict(why)}")
    return cagr,mdd,bc

print(f'\nNIFTYBEES buy & hold: CAGR 10.27%  MDD -36.34%  Calmar 0.28')
print('='*142)
print('FINAL — dividend field NOT trusted. "invested" = mean share of equity actually in stock (cash earns 0).')
print('='*142)
for dmode in ('nodiv','sanitised'):
    print(f'\n--- {dmode.upper()} ---')
    for slots in (10,20):
        pf(slots, None, dmode, label=f'hold until recovery')
        pf(slots, 1095, dmode, label=f'+ 36-month stop')
    pf(20, None, dmode, deep_only=True, label='DEEP only, hold until recovery')

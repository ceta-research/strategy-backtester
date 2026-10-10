import json, sys, datetime as dt, statistics as st
from collections import defaultdict, Counter
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
from study import *

def pct(x): return f'{100*x:+.1f}%'
def q(v,p): return float(np.percentile(v,p)) if len(v) else float('nan')

def hit_table(trs, label):
    """Recovery to the frozen 2y high within N months."""
    print(f'\n--- {label}: n={len(trs)} ---')
    if not trs: return
    done=[t for t in trs if not t['open']]
    print(f"  recovered ever (within data): {len(done)}/{len(trs)} = {100*len(done)/len(trs):.0f}%"
          f"   | still open at last bar: {len(trs)-len(done)}")
    hdr='  horizon   '+''.join(f'{m:>7}m' for m in (6,12,18,24,36,60))
    print(hdr)
    row='  hit rate  '
    for m in (6,12,18,24,36,60):
        # only trades with enough forward data to observe that horizon
        elig=[t for t in trs if (t['x_d']-t['e_d'])//DAY >= m*30 or not t['open']]
        elig=[t for t in trs if t['open']==False or (t['x_d']-t['e_d'])//DAY >= m*30]
        if not elig: row+=f'{"-":>8}'; continue
        h=sum(1 for t in elig if not t['open'] and t['days'] <= m*30)
        row+=f'{100*h/len(elig):>7.0f}%'
    print(row)
    d=[t['days'] for t in done]
    if d:
        print(f"  time-to-recovery (days): median {st.median(d):.0f}  p25 {q(d,25):.0f}  p75 {q(d,75):.0f}  p90 {q(d,90):.0f}  max {max(d)}")
        print(f"  time-to-recovery (months): median {st.median(d)/30.4:.1f}  p75 {q(d,75)/30.4:.1f}  p90 {q(d,90)/30.4:.1f}")
    mae=[t['mae'] for t in trs]
    print(f"  drawdown BEFORE the exit: median {pct(st.median(mae))}  p25 {pct(q(mae,25))}  "
          f"p10 {pct(q(mae,10))}  worst {pct(min(mae))}   | >=-20% further: {100*sum(1 for m in mae if m<=-0.20)/len(mae):.0f}%"
          f"  >=-40%: {100*sum(1 for m in mae if m<=-0.40)/len(mae):.0f}%")
    net=[t['net'] for t in trs]; ex=[t['excess'] for t in trs if t['excess'] is not None]
    print(f"  net return  mean {pct(st.mean(net))}  median {pct(st.median(net))}  worst {pct(min(net))}  best {pct(max(net))}")
    if ex:
        w=sum(1 for e in ex if e>0)
        print(f"  EXCESS over NIFTYBEES (same dates)  mean {pct(st.mean(ex))}  median {pct(st.median(ex))}"
              f"   beat the index {w}/{len(ex)} = {100*w/len(ex):.0f}%")
    # annualised, per trade
    ann=[( (1+t['net'])**(365/max(t['days'],20)) -1) for t in trs if t['days']>=20]
    if ann: print(f"  per-trade annualised: median {pct(st.median(ann))}  mean {pct(st.mean(ann))}")
    return done

def regime_split(trs, label):
    print(f'\n--- {label}: regime split (was NIFTY itself >=15% off its own 2y high at entry?) ---')
    broad=[t for t in trs if t['idx_off'] is not None and t['idx_off'] <= -0.15]
    idio =[t for t in trs if t['idx_off'] is not None and t['idx_off'] >  -0.15]
    for nm,g in (('BROAD market drawdown', broad), ('IDIOSYNCRATIC fall (index near its high)', idio)):
        if not g: print(f'  {nm:<42} n=0'); continue
        ex=[t['excess'] for t in g if t['excess'] is not None]
        done=[t for t in g if not t['open']]
        net=[t['net'] for t in g]
        print(f'  {nm:<42} n={len(g):>4}  recovered {100*len(done)/len(g):>3.0f}%  '
              f'net med {pct(st.median(net))}  excess med {pct(st.median(ex)) if ex else "-"}  '
              f'excess mean {pct(st.mean(ex)) if ex else "-"}  beat idx {100*sum(1 for e in ex if e>0)/len(ex):.0f}%' if ex else '')

def cohort(trs, label):
    print(f'\n--- {label}: by entry year ---')
    by=defaultdict(list)
    for t in trs: by[f(t['e_d']).year].append(t)
    print(f"  {'yr':<6}{'n':>4}{'recov%':>8}{'medDays':>9}{'medNet':>9}{'medExc':>9}{'medMAE':>9}")
    for y in sorted(by):
        g=by[y]; done=[t for t in g if not t['open']]
        ex=[t['excess'] for t in g if t['excess'] is not None]
        print(f"  {y:<6}{len(g):>4}{100*len(done)/len(g):>7.0f}%"
              f"{(st.median([t['days'] for t in done]) if done else float('nan')):>9.0f}"
              f"{pct(st.median([t['net'] for t in g])):>9}"
              f"{(pct(st.median(ex)) if ex else '-'):>9}"
              f"{pct(st.median([t['mae'] for t in g])):>9}")

# ---------------- portfolio simulation ----------------
def portfolio(cands, slots=10, start=100.0, label='', rank_by='up'):
    """Equal-weight, fixed slots. Candidates chosen at month-end t fill on the FIRST
    trading bar after t (no same-bar fill). Exit on the first close >= the frozen 2y
    high. Compounds; cash earns nothing; 0.15% each way."""
    by_t=defaultdict(list)
    for c in cands: by_t[c['t']].append(c)
    for t in by_t: by_t[t].sort(key=lambda x:-x[rank_by])
    dates=[int(x) for x in nb_d if x >= DDATES[0]]
    dset=np.array(dates)
    fill_of={}                                     # fill day -> decision date
    for t in DDATES:
        k=np.searchsorted(dset, t, 'right')
        if k < len(dset): fill_of.setdefault(int(dset[k]), t)
    pxc={s:{int(d):float(c) for d,c in zip(S[s]['d'], S[s]['c'])} for s in S}
    cash=start; pos=[]; curve=[]; closed=[]
    def mark(p, day):
        c=pxc[p['sym']].get(day, p['last']); p['last']=c
        dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d'] < dd <= day)
        return p['alloc']*(1+((c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1))
    for day in dates:
        keep=[]
        for p in pos:                              # exits
            c=pxc[p['sym']].get(day)
            if c is not None and c >= p['tgt']:
                dvd=sum(v for dd,v in DV.get(p['sym'],[]) if p['e_d'] < dd <= day)
                net=(c*(1-COST)+dvd)/(p['e_px']*(1+COST))-1
                cash += p['alloc']*(1+net)
                closed.append(dict(sym=p['sym'], e_d=p['e_d'], x_d=day, net=net,
                                   days=(day-p['e_d'])//DAY))
            else: keep.append(p)
        pos=keep
        eq=cash+sum(mark(p,day) for p in pos)
        if day in fill_of:                         # entries, once per decision date
            held={p['sym'] for p in pos}
            for c in by_t.get(fill_of[day], []):
                if len(pos)>=slots: break
                if c['sym'] in held: continue
                e=pxc[c['sym']].get(day)
                if e is None: continue
                alloc=eq/slots
                if cash < alloc*0.999: break
                cash-=alloc
                pos.append(dict(sym=c['sym'], e_d=day, e_px=e, tgt=c['hi'], alloc=alloc, last=e))
                held.add(c['sym'])
            eq=cash+sum(mark(p,day) for p in pos)
        curve.append((day,eq))
    eqv=np.array([x[1] for x in curve]); dts=[x[0] for x in curve]
    yrs=(dts[-1]-dts[0])/365/DAY
    cagr=(eqv[-1]/eqv[0])**(1/yrs)-1
    mdd=float((eqv/np.maximum.accumulate(eqv)-1).min())
    i0=np.searchsorted(nb_d, dts[0],'left'); i1=np.searchsorted(nb_d, dts[-1],'right')-1
    bh=nb_c[i0:i1+1]
    b_cagr=(bh[-1]/bh[0])**(1/yrs)-1
    b_mdd=float((bh/np.maximum.accumulate(bh)-1).min())
    openv=sum(mark(p,dts[-1]) for p in pos)
    print(f'\n--- PORTFOLIO {label} (slots={slots}, {f(dts[0])} -> {f(dts[-1])}, {yrs:.1f}y) ---')
    print(f"  RULE       CAGR {100*cagr:>6.2f}%   MDD {100*mdd:>7.2f}%   Calmar {cagr/-mdd:>5.2f}   "
          f"final {eqv[-1]/eqv[0]:.2f}x   closed {len(closed)}")
    print(f"  NIFTYBEES  CAGR {100*b_cagr:>6.2f}%   MDD {100*b_mdd:>7.2f}%   Calmar {b_cagr/-b_mdd:>5.2f}   "
          f"final {bh[-1]/bh[0]:.2f}x   (buy & hold, same window)")
    print(f"  edge       {100*(cagr-b_cagr):+.2f}pp CAGR   |  still open at the end: {len(pos)} "
          f"= {100*openv/eqv[-1]:.0f}% of final equity")
    if pos: print('             open: ' + ', '.join(f"{p['sym']}({100*(p['last']/p['e_px']-1):+.0f}%)" for p in pos))
    return dict(cagr=cagr, mdd=mdd, b_cagr=b_cagr, b_mdd=b_mdd, curve=curve,
                closed=closed, n_open=len(pos), final=float(eqv[-1]/eqv[0]))

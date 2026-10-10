"""BUY-ONCE BASKET TEST — the honest version of the user's actual question.

On each month-end, take the 10 deepest qualifying names AT THAT MOMENT, buy equal amounts,
hold each until it regains its own 2-year high. One basket, bought once, like the real
decision. This respects correlation: the names fall and recover together, which the
bootstrap in basket.py wrongly assumes away.

Compared against putting the same money in NIFTYBEES on the same day and selling the day
the last basket name resolves.
"""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import fixed_run as FR, fixed_pf as PF
from fixed import candidates
from study import DDATES, DV, D, DAY, COST, nb_d, nb_c, f

PANEL=PF.PANEL; WIPED=FR.WIPED; LAST=FR.LAST; DATA_END=PF.DATA_END
ALL=PF.ALL
by=defaultdict(list)
for k in ALL: by[k['t']].append(k)
for t in by: by[t].sort(key=lambda x: x['off'])      # deepest first

def price(s,day,fb):
    v=PANEL[s].get(day)
    if v is not None: return v
    if s in WIPED and day > LAST.get(s, DATA_END): return 0.0
    return fb

def run_basket(t, n=10, max_days=None):
    """Buy n deepest on the bar after t, hold each to its own target."""
    dts=[int(x) for x in nb_d if x>t]
    if not dts: return None
    day0=dts[0]
    picks=[]
    for k in by.get(t,[]):
        if len(picks)>=n: break
        e=PANEL.get(k['sym'],{}).get(day0)
        if e is None or e<=0: continue
        picks.append((k,e))
    if len(picks)<n: return None
    rets=[]; lens=[]
    for k,e in picks:
        last=e; out=None
        for day in dts:
            c=price(k['sym'],day,last); last=c
            if c>=k['hi'] or (max_days and day>=day0+max_days*DAY) or c==0.0:
                out=(day,c); break
        if out is None: out=(dts[-1], last)
        xd,xp=out
        dvd=0.0   # price-only: the dividend field is corrupt (see RESULTS.md)
        rets.append((xp*(1-COST)+dvd)/(e*(1+COST))-1); lens.append((xd-day0)//DAY)
    hold=max(lens)
    bi=np.searchsorted(nb_d,day0,'left'); bj=np.searchsorted(nb_d,day0+hold*DAY,'right')-1
    ben=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
    return dict(t=t, day0=day0, ret=st.mean(rets), bench=ben, hold_mo=hold/30.4,
                med_mo=st.median(lens)/30.4, worst=min(rets),
                n_wiped=sum(1 for r in rets if r<=-0.95))

for label, md in [('hold until each regains its high', None), ('with a 36-month time stop', 1095)]:
    res=[r for r in (run_basket(t, 10, md) for t in DDATES) if r]
    # only baskets with enough forward data to have resolved
    done=[r for r in res if r['day0'] + r['hold_mo']*30.4*DAY <= DATA_END - 30*DAY]
    rr=sorted(r['ret'] for r in res); ex=sorted(r['ret']-r['bench'] for r in res if r['bench'] is not None)
    q=lambda v,p: float(np.percentile(v,p))
    print(f'\n{"="*104}')
    print(f'BUY-ONCE 10-NAME BASKET, {label} — {len(res)} real start months (2011-2026)')
    print(f'{"="*104}')
    print(f"  {'':<14}{'p10':>10}{'p25':>10}{'MEDIAN':>10}{'p75':>10}{'p90':>10}{'mean':>10}")
    print(f"  {'basket return':<14}{100*q(rr,10):>9.1f}%{100*q(rr,25):>9.1f}%{100*q(rr,50):>9.1f}%"
          f"{100*q(rr,75):>9.1f}%{100*q(rr,90):>9.1f}%{100*st.mean(rr):>9.1f}%")
    print(f"  {'vs NIFTYBEES':<14}{100*q(ex,10):>9.1f}%{100*q(ex,25):>9.1f}%{100*q(ex,50):>9.1f}%"
          f"{100*q(ex,75):>9.1f}%{100*q(ex,90):>9.1f}%{100*st.mean(ex):>9.1f}%")
    print(f"\n  beats NIFTYBEES      {100*sum(1 for v in ex if v>0)/len(ex):>5.1f}% of start months")
    print(f"  loses money          {100*sum(1 for v in rr if v<0)/len(rr):>5.1f}% of start months")
    hm=[r['hold_mo'] for r in res]
    print(f"  months until the LAST name resolves: median {st.median(hm):.0f}  p90 {q(hm,90):.0f}")
    print(f"  worst single name in the basket: median {100*st.median([r['worst'] for r in res]):+.1f}%  "
          f"baskets containing a wipeout: {100*sum(1 for r in res if r['n_wiped'])/len(res):.0f}%")
    CAP=4_200_000
    print(f"\n  on Rs {CAP:,}:  median Rs {CAP*q(rr,50):+,.0f}   p10 Rs {CAP*q(rr,10):+,.0f}   p90 Rs {CAP*q(rr,90):+,.0f}")
    if md is None:
        json.dump([{k:(float(v) if isinstance(v,(np.floating,))else v) for k,v in r.items()} for r in res],
                  open(D+'basket_once.json','w'), default=str)

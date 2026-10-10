"""THE ACTUAL QUESTION: buy the 2026-10-09 top-10 blue chips in equal proportion.
What does the corrected backtest say happens?

Method: for each of the 10 names, draw historical analogues matched on how far off its
2-year high it is (+/-7pp), equal-weight 10 of them, repeat 4000 times. That turns the
511-episode history into a distribution of outcomes for a 10-name basket.
"""
import sys, json, statistics as st, random
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import fixed_run as FR
from fixed import candidates, walk
from study import episodes, D, DAY, f

random.seed(7)
POOL = FR.episodes([x for x in (walk(k) for k in candidates()) if x]) + FR.surv_eps()
print(f'historical pool: {len(POOL)} episodes\n')

TOP10=json.load(open('studies/bluechip_derating/screen/bc_top10_final.json'))
# ITC's target is overstated: its 2y high predates the hotels demerger (-8.09%).
for r in TOP10:
    if r['sym']=='ITC':
        r['hi']=471.10; r['up']=100*(471.10/r['live']-1); r['off']=100*(r['live']/471.10-1)

print(f"{'name':<12}{'buy':>9}{'target':>9}{'off_high':>10}{'upside':>9}   matched analogues")
MATCH={}
for r in TOP10:
    off=r['off']/100
    m=[e for e in POOL if abs(e['off']-off)<=0.07]
    MATCH[r['sym']]=m
    print(f"{r['sym']:<12}{r['live']:>9.1f}{r['hi']:>9.1f}{r['off']:>9.1f}%{r['up']:>8.1f}%   n={len(m)}")

CAP=4_200_000; PER=CAP/10
draws=[]
for _ in range(4000):
    nets=[]; bens=[]; days=[]
    for r in TOP10:
        e=random.choice(MATCH[r['sym']])
        nets.append(e['net']); days.append(e['days'])
        if e['bench'] is not None: bens.append(e['bench'])
    draws.append((st.mean(nets), st.mean(bens) if bens else 0.0, st.median(days)))

bn=sorted(d[0] for d in draws); bx=sorted(d[0]-d[1] for d in draws)
bd=sorted(d[2] for d in draws)
q=lambda v,p: float(np.percentile(v,p))

print(f'\n{"="*96}')
print(f'₹{CAP:,} EQUAL-WEIGHTED ACROSS THE 10 NAMES (₹{PER:,.0f} each), held until each regains its high')
print(f'{"="*96}')
print(f"  {'outcome':<22}{'basket return':>15}{'rupee P&L':>16}{'ends at':>16}")
for lab,p in [('p10  (bad)',10),('p25',25),('MEDIAN',50),('p75',75),('p90  (good)',90)]:
    v=q(bn,p)
    print(f"  {lab:<22}{100*v:>14.1f}%{CAP*v:>+16,.0f}{CAP*(1+v):>16,.0f}")
print(f"  {'mean':<22}{100*st.mean(bn):>14.1f}%{CAP*st.mean(bn):>+16,.0f}{CAP*(1+st.mean(bn)):>16,.0f}")
print(f"\n  probability the basket LOSES money          {100*sum(1 for v in bn if v<0)/len(bn):>5.1f}%")
print(f"  probability it beats NIFTYBEES (same dates) {100*sum(1 for v in bx if v>0)/len(bx):>5.1f}%")
print(f"  median excess over NIFTYBEES                {100*q(bx,50):>+5.1f}%   (p10 {100*q(bx,10):+.1f}%, p90 {100*q(bx,90):+.1f}%)")
print(f"  median time to get there                    {q(bd,50)/30.4:>5.1f} months   (p90 {q(bd,90)/30.4:.1f} months)")

naive=st.mean([r['up'] for r in TOP10])/100
pr=100*sum(1 for v in bn if v>=naive)/len(bn)
print(f"\n  THE SCREEN SAID +{100*naive:.1f}% (₹{CAP*naive:+,.0f}). History says that outcome or better")
print(f"  happens {pr:.0f}% of the time — it is the p{100-pr:.0f} case, not the expected case.")

# what the same money does in the index over the same horizon
mo=q(bd,50)/30.4
for r_ in (0.10,0.15):
    v=(1+r_)**(mo/12)-1
    print(f"  NIFTYBEES at {100*r_:.0f}%/yr over the same {mo:.0f} months: {100*v:+.1f}%  ₹{CAP*v:+,.0f}")
json.dump(dict(median=q(bn,50), mean=st.mean(bn), p10=q(bn,10), p90=q(bn,90),
               p_loss=sum(1 for v in bn if v<0)/len(bn),
               p_beat=sum(1 for v in bx if v>0)/len(bx),
               med_months=q(bd,50)/30.4, naive=naive), open(D+'basket.json','w'))

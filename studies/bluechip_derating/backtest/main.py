import sys, json, statistics as st
sys.path.insert(0,'studies/bluechip_derating/backtest')
from report import *

print('='*100); print('BLUE-CHIP DE-RATING RULE — BACKTEST'); print('='*100)

BASE=dict(off_thr=-0.25, pe_now_max=60.0, pe_need_max=35.0, require_eps=True,
          ca_mode='truncate', top_rank=150)

c=candidates(**BASE)
print(f'\nqualifying (symbol, month) signals: {len(c):,}')
tr=[x for x in (walk(k) for k in c) if x]
print(f'tradeable (have a next bar): {len(tr):,}')
ep=episodes(tr)
print(f'DISTINCT drawdown episodes (first entry only): {len(ep):,}   '
      f'symbols: {len(set(t["sym"] for t in ep))}')

hit_table(tr, 'ALL monthly signals (overlapping)')
hit_table(ep, 'DISTINCT EPISODES  <-- the honest sample size')
regime_split(ep, 'EPISODES')
cohort(ep, 'EPISODES')

print('\n' + '='*100); print('SENSITIVITY'); print('='*100)
rows=[]
def arm(name, **kw):
    k=dict(BASE); k.update(kw)
    cc=candidates(**k); tt=[x for x in (walk(z) for z in cc) if x]; ee=episodes(tt)
    if not ee: print(f'  {name:<34} n=0'); return
    done=[t for t in ee if not t['open']]
    ex=[t['excess'] for t in ee if t['excess'] is not None]
    net=[t['net'] for t in ee]
    rows.append((name,len(ee),100*len(done)/len(ee),st.median(net),st.median(ex) if ex else 0,
                 st.median([t['mae'] for t in ee]),
                 st.median([t['days'] for t in done]) if done else float('nan')))
    print(f"  {name:<34} eps={len(ee):>4}  recov {100*len(done)/len(ee):>3.0f}%  "
          f"medNet {pct(st.median(net)):>7}  medExcess {pct(st.median(ex)) if ex else '   -':>7}  "
          f"medMAE {pct(st.median([t['mae'] for t in ee])):>7}  medDays {st.median([t['days'] for t in done]) if done else 0:>5.0f}")
for thr in (-0.20,-0.25,-0.30,-0.35,-0.40):
    arm(f'off-high threshold {100*thr:.0f}%', off_thr=thr)
arm('no EPS/multiple legs at all', require_eps=False)
arm('EPS leg, no multiple cap', pe_need_max=1e9, pe_now_max=1e9)
arm('required multiple <= 25x', pe_need_max=25.0)
arm('CA mode: exclude shifted names', ca_mode='exclude')
arm('CA mode: OFF (no CA handling)', ca_mode='off')
arm('universe top 50 by turnover', top_rank=50)
arm('universe top 100 by turnover', top_rank=100)

print('\n' + '='*100); print('PORTFOLIO'); print('='*100)
P={}
for slots in (5,10,20):
    P[slots]=portfolio(c, slots=slots, label=f'rule, -25% off high, EPS intact')
json.dump(dict(n_signals=len(c), n_trades=len(tr), n_episodes=len(ep),
               episodes=[{k:v for k,v in t.items()} for t in ep],
               portfolio={str(k):{kk:vv for kk,vv in v.items() if kk not in ('curve','closed')} for k,v in P.items()}),
          open(D+'results.json','w'), default=str)
print('\nwrote results.json')

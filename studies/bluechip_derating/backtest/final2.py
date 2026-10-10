"""Corrected grid on ANNUALISED excess.

Arithmetic excess (net - bench) is unbounded over multi-year holds: RELINFRA held 14 years
scores -433% because the index compounded 4.6x meanwhile. That metric made the deep bucket
look like +41.4% mean when 161% of the total came from three HINDPETRO episodes.
Annualised excess = trade CAGR - index CAGR over the SAME window. Bounded, comparable,
and a single 20x winner can no longer dominate a mean.
"""
import sys, json, statistics as st
from collections import Counter
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import final as F
from surv import *

def annx(x):
    d=max(int(x['days']),30); y=d/365.0
    if x['bench'] is None or x['net'] <= -1 or x['bench'] <= -1: return None
    return (1+x['net'])**(1/y) - (1+x['bench'])**(1/y)

BUCK=[('all   >= -25% off', lambda x: True),
      ('shallow -25..-35%', lambda x: -0.35 < x['off'] <= -0.25),
      ('DEEP      <= -35%', lambda x: x['off'] <= -0.35)]

print('='*126)
print('ANNUALISED excess over NIFTYBEES (trade CAGR - index CAGR, same window), survivorship-corrected')
print('='*126)
print(f"  {'exit rule':<24}{'bucket':<20}{'n':>5}{'medAnnEx':>10}{'meanAnnEx':>11}{'trimmed':>9}{'exTop1':>9}{'exTop3':>9}{'beat':>6}{'p10':>9}")
GRID={}
for lab, md in [('hold until recovery', None), ('+ 24-month time stop', 730), ('+ 36-month time stop', 1095)]:
    rows=F.corrected(md)
    for bn, sel in BUCK:
        g=[x for x in rows if sel(x)]
        v=sorted([a for a in (annx(x) for x in g) if a is not None], reverse=True)
        if len(v)<5: continue
        trim=st.mean(sorted(v)[len(v)//10: len(v)-len(v)//10])
        GRID[(lab,bn)]=(len(v), st.median(v), st.mean(v), trim)
        print(f"  {lab:<24}{bn:<20}{len(v):>5}"
              f"{100*st.median(v):>+9.1f}%{100*st.mean(v):>+10.1f}%{100*trim:>+8.1f}%"
              f"{100*st.mean(v[1:]):>+8.1f}%{100*st.mean(v[3:]):>+8.1f}%"
              f"{100*sum(1 for z in v if z>0)/len(v):>5.0f}%{100*np.percentile(v,10):>+8.1f}%")
    print()

rows=F.corrected(None)
deep=[x for x in rows if x['off']<=-0.35]
pairs=sorted([(annx(x), x['sym'], x['e_d'], x['net'], x['days']) for x in deep if annx(x) is not None], reverse=True)
print('  DEEP bucket on annualised excess — top 8 and worst 5:')
for a,s,d,n,dy in pairs[:8]: print(f"    {s:<12} {f(d)}  {dy/365:>4.1f}y  net {100*n:>+9.1f}%  annEx {100*a:>+7.1f}%/yr")
print('    ...')
for a,s,d,n,dy in pairs[-5:]: print(f"    {s:<12} {f(d)}  {dy/365:>4.1f}y  net {100*n:>+9.1f}%  annEx {100*a:>+7.1f}%/yr")
v=[p[0] for p in pairs]
print(f"\n  share of POSITIVE annualised excess from the top 3: {100*sum(z for z in v[:3])/sum(z for z in v if z>0):.0f}%")
c=Counter(p[1] for p in pairs[:10])
print(f"  symbols in the top 10: {dict(c)}")
# HINDPETRO removed
nh=[x for x in deep if x['sym']!='HINDPETRO']
vn=[a for a in (annx(x) for x in nh) if a is not None]
print(f"\n  DEEP excluding HINDPETRO entirely: n={len(vn)}  median {100*st.median(vn):+.1f}%/yr  "
      f"mean {100*st.mean(vn):+.1f}%/yr  beat {100*sum(1 for z in vn if z>0)/len(vn):.0f}%")

import json, collections
S='studies/bluechip_derating/screen/'
px={r['sym']:r for r in json.load(open(S+'bc_live.json'))}
d=json.load(open(S+'bc_fin.json')); ca=d['ca']; fin=d['fin']

by=collections.defaultdict(dict)
for f in fin:
    fy=str(f.get('fiscalYear'))
    if fy in ('2024','2025','2026') and f.get('revenue') is not None:
        if fy not in by[f['sym']]:          # rows are date DESC; keep the first (latest filing) per FY
            by[f['sym']][fy]={'rev':f.get('revenue'),'ni':f.get('netIncome'),'eps':f.get('eps')}

CA_LIMIT=-20.0; MIN_OFF=-20.0
rows=[]
for s,p in px.items():
    c=ca.get(s)
    if not c: continue
    worst=c['worst_day_pct']
    fy=by.get(s,{})
    if '2026' not in fy or '2024' not in fy: 
        reason='no FY2024/FY2026 earnings'
        rows.append(dict(sym=s,excl=reason,worst=worst,off=p['off_hi_2y_pct'])); continue
    ni_now, ni_then = fy['2026']['ni'], fy['2024']['ni']
    rv_now, rv_then = fy['2026']['rev'], fy['2024']['rev']
    ni_g = 100*(ni_now/ni_then-1) if ni_then and ni_then>0 else None
    rv_g = 100*(rv_now/rv_then-1) if rv_then and rv_then>0 else None
    live=p['live']; hi=p['hi_2y']
    rows.append(dict(sym=s, name=(p['name'] or '')[:30], ind=(p['industry'] or '')[:22],
        mcap=p['mcap_cr'], live=live, hi2y=hi,
        off=round(100*(live/hi-1),2), up=round(100*(hi/live-1),2),
        worst=worst, ni_g=ni_g, rv_g=rv_g, excl=None))

ok=[r for r in rows if not r.get('excl')]
ca_excl=[r for r in ok if r['worst']<=CA_LIMIT]
pool=[r for r in ok if r['worst']>CA_LIMIT and r['off']<=MIN_OFF]
print(f"universe {len(px)} | usable earnings {len(ok)} | corp-action excluded {len(ca_excl)} ({', '.join(r['sym'] for r in ca_excl)})")
print(f"fallen >=20% off 2y high, CA-clean: {len(pool)}")
print()
print(f"{'sym':<12}{'live':>9}{'hi_2y':>9}{'off%':>7}{'upside%':>8}{'NI g%':>9}{'Rev g%':>8}  {'industry':<22} earnings")
grow=[]; shrink=[]
for r in sorted(pool,key=lambda x:-x['up']):
    tag = 'INTACT' if (r['ni_g'] is not None and r['ni_g']>=0) else ('FELL' if r['ni_g'] is not None else 'n/a')
    (grow if tag=='INTACT' else shrink).append(r)
    nig = f"{r['ni_g']:+.1f}" if r['ni_g'] is not None else '   n/a'
    rvg = f"{r['rv_g']:+.1f}" if r['rv_g'] is not None else '  n/a'
    print(f"{r['sym']:<12}{r['live']:>9.1f}{r['hi2y']:>9.1f}{r['off']:>7.1f}{r['up']:>8.1f}{nig:>9}{rvg:>8}  {r['ind']:<22} {tag}")
json.dump({'grow':grow,'shrink':shrink}, open(S+'bc_final.json','w'))
print(f"\nFY2026 vs FY2024 net income INTACT: {len(grow)}   FELL: {len(shrink)}")

import json, collections, datetime as dt
S='studies/bluechip_derating/screen/'
px={r['sym']:r for r in json.load(open(S+'bc_live.json'))}
d=json.load(open(S+'bc_fin.json')); ca=d['ca']; fin=d['fin']
by=collections.defaultdict(dict)
for f in fin:
    fy=str(f.get('fiscalYear'))
    if fy in ('2024','2025','2026') and fy not in by[f['sym']]:
        by[f['sym']][fy]={'rev':f.get('revenue'),'ni':f.get('netIncome'),'eps':f.get('eps')}

cand=[]
for s,p in px.items():
    c=ca.get(s); fy=by.get(s,{})
    if not c or '2026' not in fy or '2024' not in fy: continue
    if c['worst_day_pct']<=-20: continue                       # corporate action
    e_now, e_then = fy['2026']['eps'], fy['2024']['eps']
    if not e_now or not e_then or e_now<=0 or e_then<=0: continue
    eps_g=100*(e_now/e_then-1)
    if abs(eps_g)>300: continue                                 # data-suspect base
    if e_now < e_then: continue                                 # per-share earnings must be intact
    live, hi = p['live'], p['hi_2y']
    off=100*(live/hi-1)
    if off>-25: continue                                        # materially fallen
    pe_now = live/e_now
    pe_at_hi = hi/e_then                                        # multiple it actually traded at
    pe_needed = hi/e_now                                        # multiple needed to regain that PRICE today
    cand.append(dict(sym=s, name=(p['name'] or '')[:26], ind=(p['industry'] or '')[:24],
        mcap=p['mcap_cr'], live=live, hi=hi, off=off, up=100*(hi/live-1),
        eps_g=eps_g, pe_now=pe_now, pe_at_hi=pe_at_hi, pe_needed=pe_needed,
        rerate=pe_needed/pe_at_hi, hi_date=str(dt.datetime.utcfromtimestamp(p['hi_2y_ep']).date())))

cand.sort(key=lambda x:-x['up'])
print(f"candidates passing ALL filters: {len(cand)}\n")
print(f"{'sym':<12}{'live':>9}{'hi_2y':>9}{'off%':>7}{'up%':>7}{'EPSg%':>7}{'PEnow':>7}{'PE@hi':>7}{'PEneed':>7}{'rerate':>7}  industry")
for r in cand:
    print(f"{r['sym']:<12}{r['live']:>9.1f}{r['hi']:>9.1f}{r['off']:>7.1f}{r['up']:>7.1f}{r['eps_g']:>7.1f}{r['pe_now']:>7.1f}{r['pe_at_hi']:>7.1f}{r['pe_needed']:>7.1f}{r['rerate']:>7.2f}  {r['ind']}")

# sector-capped top 10 (max 2 per industry) to stop it becoming one sector bet
seen=collections.Counter(); top=[]
for r in cand:
    if seen[r['ind']]>=2: continue
    seen[r['ind']]+=1; top.append(r)
    if len(top)==10: break
json.dump(top, open(S+'bc_top10.json','w'))
print(f"\n=== SECTOR-CAPPED TOP 10 (max 2 per industry) ===")
print(f"{'#':<3}{'sym':<12}{'live':>9}{'target(2y hi)':>14}{'upside%':>9}{'EPSg%':>7}{'rerate':>7}  {'industry':<24} hi set")
for i,r in enumerate(top,1):
    print(f"{i:<3}{r['sym']:<12}{r['live']:>9.1f}{r['hi']:>14.1f}{r['up']:>9.1f}{r['eps_g']:>7.1f}{r['rerate']:>7.2f}  {r['ind']:<24} {r['hi_date']}")
import statistics as st
ups=[r['up'] for r in top]
print(f"\nEQUAL-WEIGHT basket of 10: mean upside to 2y high {st.mean(ups):+.1f}%  median {st.median(ups):+.1f}%  min {min(ups):+.1f}%  max {max(ups):+.1f}%")
for cap in (4200000, 4549166):
    per=cap/10
    gain=sum(per*(r['up']/100) for r in top)
    print(f"  Rs {cap:>9,} deployed (Rs {per:,.0f} each) -> gain if ALL regain their 2y high: Rs {gain:+,.0f}  (final Rs {cap+gain:,.0f})")

import json, collections, datetime as dt, statistics as st
S='studies/bluechip_derating/screen/'
px={r['sym']:r for r in json.load(open(S+'bc_live.json'))}
d=json.load(open(S+'bc_fin.json')); ca=d['ca']; fin=d['fin']
by=collections.defaultdict(dict)
for f in fin:
    fy=str(f.get('fiscalYear'))
    if fy in ('2024','2026') and fy not in by[f['sym']]:
        by[f['sym']][fy]={'eps':f.get('eps'),'ni':f.get('netIncome')}
# PSU-lender / correlated-cluster tagging: FMP industry misses that IRFC/RECLTD/PFC are one bet
CLUSTER={'IRFC':'PSU lender','RECLTD':'PSU lender','PFC':'PSU lender',
         'BANKBARODA':'PSU bank','CANBK':'PSU bank','SBIN':'PSU bank','IDBI':'PSU bank',
         'NTPC':'PSU power','POWERGRID':'PSU power','IOC':'PSU oil','BPCL':'PSU oil','ONGC':'PSU oil'}
cand=[]
for s,p in px.items():
    c=ca.get(s); fy=by.get(s,{})
    if not c or '2026' not in fy or '2024' not in fy: continue
    if c['worst_day_pct']<=-20: continue
    e_now,e_then=fy['2026']['eps'],fy['2024']['eps']
    if not e_now or not e_then or e_now<=0 or e_then<=0: continue
    eps_g=100*(e_now/e_then-1)
    if abs(eps_g)>300 or e_now<e_then: continue
    live,hi=p['live'],p['hi_2y']
    off=100*(live/hi-1)
    if off>-25: continue
    pe_now=live/e_now; pe_at_hi=hi/e_then; pe_needed=hi/e_now
    if pe_now>60 or pe_needed>35: continue          # <-- the new gate: required multiple must be sane
    cand.append(dict(sym=s,ind=(p['industry'] or '')[:22],cluster=CLUSTER.get(s,(p['industry'] or '')[:22]),
        mcap=p['mcap_cr'],live=live,hi=hi,off=off,up=100*(hi/live-1),eps_g=eps_g,
        pe_now=pe_now,pe_at_hi=pe_at_hi,pe_needed=pe_needed,
        hi_date=str(dt.datetime.utcfromtimestamp(p['hi_2y_ep']).date())))
cand.sort(key=lambda x:-x['up'])
print(f"pass ALL filters incl. sane required multiple (PE_needed<=35, PE_now<=60): {len(cand)}\n")
print(f"{'sym':<12}{'live':>9}{'hi_2y':>9}{'off%':>7}{'up%':>7}{'EPSg%':>7}{'PEnow':>7}{'PEneed':>7}  {'cluster':<22} hi set")
for r in cand:
    print(f"{r['sym']:<12}{r['live']:>9.1f}{r['hi']:>9.1f}{r['off']:>7.1f}{r['up']:>7.1f}{r['eps_g']:>7.1f}{r['pe_now']:>7.1f}{r['pe_needed']:>7.1f}  {r['cluster']:<22} {r['hi_date']}")
seen=collections.Counter(); top=[]
for r in cand:
    if seen[r['cluster']]>=2: continue
    seen[r['cluster']]+=1; top.append(r)
    if len(top)==10: break
json.dump(top, open(S+'bc_top10_final.json','w'))
print(f"\n=== FINAL TOP 10 (max 2 per correlated cluster) ===")
print(f"{'#':<3}{'sym':<12}{'buy at':>9}{'target':>9}{'upside%':>9}{'PEnow':>7}{'PEneed':>7}  cluster")
for i,r in enumerate(top,1):
    print(f"{i:<3}{r['sym']:<12}{r['live']:>9.1f}{r['hi']:>9.1f}{r['up']:>9.1f}{r['pe_now']:>7.1f}{r['pe_needed']:>7.1f}  {r['cluster']}")
ups=[r['up'] for r in top]
print(f"\nmean upside {st.mean(ups):+.1f}%  median {st.median(ups):+.1f}%  min {min(ups):+.1f}%  max {max(ups):+.1f}%")
for cap in (4200000,4549166):
    per=cap/10; gain=sum(per*(r['up']/100) for r in top)
    print(f"  Rs {cap:>9,} (Rs {per:,.0f} each) -> if ALL regain their 2y high: Rs {gain:+,.0f}  -> Rs {cap+gain:,.0f}")
    for hit in (0.7,0.5):
        print(f"      if only {hit:.0%} of the upside is realised: Rs {gain*hit:+,.0f}")

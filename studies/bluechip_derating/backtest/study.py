"""Blue-chip de-rating event study + portfolio sim.

RULE UNDER TEST (as screened on 2026-10-09, scratchpad bc7.py):
  buy a large, liquid, profitable NSE name trading >= X% below its trailing
  2-year high, with EPS intact and a sane required re-rating multiple;
  hold until it regains that 2-year high.

Everything here is point-in-time. Universe from bhavcopy traded value (CA-invariant),
prices from charting (adjusted, 2011+), EPS lagged 120d off period end because
fmp.income_statement carries NO real filing date for India.
"""
import json, os, sys, math, datetime as dt, statistics as st
from collections import defaultdict
import numpy as np

D='studies/bluechip_derating/backtest/'
DAY=86400
f=lambda e: dt.datetime.fromtimestamp(e, dt.UTC).date()

# ---------------- load ----------------
uni=json.load(open(D+'universe.json'))
fund=json.load(open(D+'fund.json'))
divs=json.load(open(D+'divs.json'))
nifty=json.load(open(D+'nifty.json'))
px=[]
for y in range(2009,2027):
    p=D+f'px_{y}.json'
    if os.path.exists(p): px+=json.load(open(p))
print(f'panel {len(px):,} bars | universe rows {len(uni):,} | fund {len(fund):,} | divs {len(divs):,}')

# per-symbol arrays
S={}
tmp=defaultdict(list)
for r in px: tmp[r['sym']].append((r['d'], r['c'], r['h']))
for s,v in tmp.items():
    v.sort()
    S[s]=dict(d=np.array([x[0] for x in v]), c=np.array([x[1] for x in v], float),
              h=np.array([x[2] for x in v], float))
del tmp, px

NB=sorted((r['d'], r['c'], r['h']) for r in nifty)
nb_d=np.array([x[0] for x in NB]); nb_c=np.array([x[1] for x in NB], float); nb_h=np.array([x[2] for x in NB], float)
nb_ret=np.concatenate([[0.0], nb_c[1:]/nb_c[:-1]-1])
NB_RET={int(d): r for d,r in zip(nb_d, nb_ret)}

# fundamentals: per sym, list of (avail_ep, pe_ep, fy, eps, ni) sorted by pe_ep
FU=defaultdict(list)
for r in fund:
    if r['eps'] is None: continue
    FU[r['sym']].append((r['pe_ep'], r['avail_ep'], r['fy'], r['eps'], r['ni']))
for s in FU: FU[s].sort()

DV=defaultdict(list)
for r in divs: DV[r['sym']].append((r['d'], r['div']))
for s in DV: DV[s].sort()

# ---------------- CA level shifts ----------------
# charting is adjusted for splits/bonuses from 2011 but NOT for demergers
# (ITC 2025-01-06 printed -8.1%). A demerger leaves a permanent level shift, so
# the old high is unreachable. Detect: idiosyncratic single-day drop <= SHIFT_PCT
# on a day the index was not itself falling hard, with no prompt recovery.
SHIFT_PCT=-0.08; IDX_OK=-0.04; RECOVER_WIN=5; RECOVER_FRAC=0.5
SHIFTS={}
for s,a in S.items():
    d,c=a['d'],a['c']
    if len(c)<3: SHIFTS[s]=np.array([],dtype=np.int64); continue
    r=np.concatenate([[0.0], c[1:]/c[:-1]-1])
    out=[]
    for i in np.where(r<=SHIFT_PCT)[0]:
        if NB_RET.get(int(d[i]), 0.0) <= IDX_OK: continue        # market-wide, not a CA
        j=min(i+RECOVER_WIN, len(c)-1)
        # recovery test: did it win back >= half the drop within RECOVER_WIN bars?
        back=c[i+1:j+1].max() if j>i else c[i]
        if back >= c[i] + RECOVER_FRAC*(c[i-1]-c[i]): continue   # bounced -> real move
        out.append(int(d[i]))
    SHIFTS[s]=np.array(out, dtype=np.int64)
n_shift=sum(len(v) for v in SHIFTS.values())
print(f'CA level-shift candidates: {n_shift} across {sum(1 for v in SHIFTS.values() if len(v))} symbols')

# ---------------- signal ----------------
LOOKBACK=730*DAY; MIN_BARS=400
UNI=defaultdict(list)
for r in uni: UNI[r['t']].append(r['sym'])
DDATES=sorted(UNI)
print(f'decision dates {len(DDATES)}  {f(DDATES[0])} -> {f(DDATES[-1])}')

def latest_fy(s, t):
    """(fy, eps, ni) of the most recent FY statement PUBLISHED by t, and the one 2 FYs before it."""
    av=[x for x in FU.get(s,[]) if x[1] <= t]
    if not av: return None, None
    now=av[-1]
    then=[x for x in av if x[2] == now[2]-2]
    return now, (then[-1] if then else None)

def nifty_off_high(t):
    j=np.searchsorted(nb_d, t, 'right')-1
    if j<0: return None
    i0=np.searchsorted(nb_d, t-LOOKBACK, 'left')
    hh=nb_h[i0:j+1].max() if j>=i0 else None
    return (nb_c[j]/hh-1) if hh else None

def candidates(off_thr=-0.25, pe_now_max=60.0, pe_need_max=35.0,
               require_eps=True, ca_mode='truncate', top_rank=150):
    """ca_mode: 'truncate' (window starts after the last level shift) | 'exclude' | 'off'"""
    out=[]
    for t in DDATES:
        for s in UNI[t][:top_rank]:
            a=S.get(s)
            if a is None: continue
            d,c,h=a['d'],a['c'],a['h']
            j=np.searchsorted(d, t, 'right')-1
            if j<0 or d[j] != t: continue                 # must trade on the decision date
            w0=t-LOOKBACK
            if ca_mode!='off' and len(SHIFTS[s]):
                sh=SHIFTS[s][(SHIFTS[s]>w0) & (SHIFTS[s]<=t)]
                if len(sh):
                    if ca_mode=='exclude': continue
                    w0=max(w0, int(sh.max()))             # truncate: high must post-date the shift
            i0=np.searchsorted(d, w0, 'right')
            if j-i0+1 < MIN_BARS: continue
            hi=h[i0:j+1].max()
            if hi<=0: continue
            px_t=c[j]; off=px_t/hi-1
            if off > off_thr: continue
            now,then=latest_fy(s,t)
            if now is None: continue
            eps_now=now[3]; ni_now=now[4]
            if ni_now is None or ni_now <= 0: continue    # profitable = the "blue chip" leg
            pe_now = px_t/eps_now if eps_now and eps_now>0 else None
            pe_need= hi/eps_now   if eps_now and eps_now>0 else None
            if require_eps:
                if not eps_now or eps_now<=0: continue
                if then is None or not then[3] or then[3]<=0: continue
                g=eps_now/then[3]-1
                if g<=0 or g>3.0: continue
                if pe_now is None or pe_now>pe_now_max: continue
                if pe_need is None or pe_need>pe_need_max: continue
            out.append(dict(t=int(t), sym=s, px=float(px_t), hi=float(hi), off=float(off),
                            up=float(hi/px_t-1), eps_now=eps_now,
                            pe_now=(float(pe_now) if pe_now else None),
                            pe_need=(float(pe_need) if pe_need else None),
                            idx_off=nifty_off_high(t)))
    return out

# ---------------- forward path ----------------
COST=0.0015   # each way: 0.05% slippage + ~0.10% charges
def walk(cand):
    """Enter at the NEXT bar's close. Exit on the first close >= the frozen target."""
    a=S[cand['sym']]; d,c=a['d'],a['c']
    j=np.searchsorted(d, cand['t'], 'right')     # next bar
    if j>=len(d): return None
    e_d, e_px = int(d[j]), float(c[j])
    tgt=cand['hi']
    fwd_c=c[j:]; fwd_d=d[j:]
    hit=np.where(fwd_c>=tgt)[0]
    if len(hit):
        k=int(hit[0]); x_d, x_px, open_=int(fwd_d[k]), float(fwd_c[k]), False
    else:
        k=len(fwd_c)-1; x_d, x_px, open_=int(fwd_d[k]), float(fwd_c[k]), True
    path=fwd_c[:k+1]
    mae=float(path.min()/e_px-1)                 # worst close-to-close drawdown before exit
    mae_d=int(fwd_d[:k+1][int(path.argmin())])
    dvd=sum(v for dd,v in DV.get(cand['sym'],[]) if e_d < dd <= x_d)
    gross=(x_px + dvd)/e_px - 1
    net=(x_px*(1-COST) + dvd)/(e_px*(1+COST)) - 1
    # benchmark over the identical holding period
    bi=np.searchsorted(nb_d, e_d, 'left'); bj=np.searchsorted(nb_d, x_d, 'right')-1
    bench=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
    days=(x_d-e_d)//DAY
    return dict(**cand, e_d=e_d, e_px=e_px, x_d=x_d, x_px=x_px, open=open_,
                days=int(days), mae=mae, mae_d=mae_d, divd=float(dvd),
                gross=float(gross), net=float(net), bench=bench,
                excess=(float(net-bench) if bench is not None else None))

def episodes(trades):
    """First entry per drawdown episode: consecutive monthly qualifications collapse to one."""
    by=defaultdict(list)
    for tr in trades: by[tr['sym']].append(tr)
    out=[]
    for s,v in by.items():
        v.sort(key=lambda x:x['t'])
        prev_idx=None
        for tr in v:
            i=DDATES.index(tr['t'])
            if prev_idx is None or i-prev_idx > 1: out.append(tr)
            prev_idx=i
    return sorted(out, key=lambda x:x['t'])

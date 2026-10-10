"""CORRECTED study. Fixes three defects the 2026-10-10 audit found in study.py/surv.py.

1. `ca_mode='truncate'` was not truncation. MIN_BARS was re-applied to the SHORTENED
   window, so any flag inside the most recent ~19 of 24 months silently DROPPED the
   candidate instead of shortening its lookback. Only 7 of 253 episodes were truly
   truncated and 53% of signals were deleted. FIX: the history requirement is tested on
   the FULL 2-year window; truncation now affects only the high.
2. The level-shift detector was 96.4% false positives (70 of 2,722 flags near a real
   split) — an earnings-crash detector. FIX: corporate actions come from
   fmp.splits_calendar events that the charting audit classified RAW or AMBIGUOUS, plus
   the ITC hotels demerger explicitly.
3. On the bhavcopy leg the same mechanism deleted the real failures (DHFL, HDIL, EDUCOMP,
   ARSSINFRA, OPTOCIRCUI) before they could be classified — circular, since an
   NCLT-bound company necessarily has >=8% down days. FIX: bhavcopy is unadjusted, so keep
   only genuine split dates there and drop the crash loop entirely.
"""
import sys, json, statistics as st
from collections import defaultdict
import numpy as np
sys.path.insert(0,'studies/bluechip_derating/backtest')
import study as ST
from study import (S, DDATES, UNI, DV, FU, D, DAY, COST, nb_d, nb_c, nb_h, f,
                   LOOKBACK, MIN_BARS, latest_fy, nifty_off_high, episodes)

# ---- (2) corporate actions: real events only ---------------------------
ca=json.load(open(D+'ca_audit.json'))
CA=defaultdict(set)
nraw=0
for r in ca:
    act, raw = r['actual_ratio'], r['raw_ratio_if_unadjusted']
    d_raw=abs(act/raw-1); d_adj=abs(act-1)
    unadj = d_raw < 0.15 and d_adj > 0.15
    ambig = not (d_adj < 0.15 and d_raw > 0.15) and not unadj
    if unadj or ambig:
        CA[r['sym']].add(int(r['ex_ep'])); nraw+=1
CA['ITC'].add(1736121600)        # hotels demerger ex 2025-01-06, -8.09%, never adjusted
CA_SH={s: np.array(sorted(v), dtype=np.int64) for s,v in CA.items()}
print(f'CA events used: {nraw+1} across {len(CA_SH)} symbols  '
      f'(vs the old detector: 2,722 across 319)', flush=True)

def candidates(off_thr=-0.25, pe_now_max=60.0, pe_need_max=35.0, require_eps=True,
               ca='real', top_rank=150):
    """ca: 'real' = truncate the high at genuine CA dates | 'off' = no handling."""
    out=[]
    for t in DDATES:
        for s in UNI[t][:top_rank]:
            a=S.get(s)
            if a is None: continue
            d,c,h=a['d'],a['c'],a['h']
            j=np.searchsorted(d,t,'right')-1
            if j<0 or d[j]!=t: continue
            full0=t-LOOKBACK
            iF=np.searchsorted(d, full0, 'right')
            # (1) the history requirement is tested on the FULL window, always
            if j-iF+1 < MIN_BARS: continue
            w0=full0
            if ca=='real' and s in CA_SH:
                sh=CA_SH[s][(CA_SH[s]>full0)&(CA_SH[s]<=t)]
                if len(sh): w0=int(sh.max())          # truncate the HIGH only
            i0=np.searchsorted(d, w0, 'right')
            if j < i0: continue
            hi=h[i0:j+1].max()
            if hi<=0: continue
            px_t=c[j]; off=px_t/hi-1
            if off > off_thr: continue
            now,then=latest_fy(s,t)
            if now is None or now[4] is None or now[4]<=0: continue
            eps=now[3]
            pe_now=px_t/eps if eps and eps>0 else None
            pe_need=hi/eps if eps and eps>0 else None
            if require_eps:
                if not eps or eps<=0: continue
                if then is None or not then[3] or then[3]<=0: continue
                g=eps/then[3]-1
                if g<=0 or g>3.0: continue
                if pe_now is None or pe_now>pe_now_max: continue
                if pe_need is None or pe_need>pe_need_max: continue
            out.append(dict(t=int(t), sym=s, px=float(px_t), hi=float(hi), off=float(off),
                            up=float(hi/px_t-1), idx_off=nifty_off_high(t)))
    return out

def walk(k, max_days=None, prices=None, divs=True):
    a=(prices or S)[k['sym']]; d,c=a['d'],a['c']
    j=np.searchsorted(d,k['t'],'right')
    if j>=len(d): return None
    e_d,e_px=int(d[j]),float(c[j]); fc,fd=c[j:],d[j:]
    lim=e_d+max_days*DAY if max_days else None
    reason='open'; kk=len(fc)-1
    for i in range(len(fc)):
        if fc[i]>=k['hi']: reason='target'; kk=i; break
        if lim is not None and fd[i]>=lim: reason='time'; kk=i; break
    x_d,x_px=int(fd[kk]),float(fc[kk])
    dvd=sum(v for dd,v in DV.get(k['sym'],[]) if e_d<dd<=x_d) if divs else 0.0
    net=(x_px*(1-COST)+dvd)/(e_px*(1+COST))-1
    bi=np.searchsorted(nb_d,e_d,'left'); bj=np.searchsorted(nb_d,x_d,'right')-1
    bench=float(nb_c[bj]/nb_c[bi]-1) if 0<=bi<=bj<len(nb_c) else None
    return dict(**k, e_d=e_d, e_px=e_px, x_d=x_d, x_px=x_px, open=(reason=='open'),
                reason=reason, days=int((x_d-e_d)//DAY), mae=float(fc[:kk+1].min()/e_px-1),
                divd=float(dvd), net=net, bench=bench,
                excess=(net-bench if bench is not None else None))

def annx(x):
    y=max(int(x['days']),30)/365.0
    if x['bench'] is None or x['net']<=-1 or x['bench']<=-1: return None
    return (1+x['net'])**(1/y)-(1+x['bench'])**(1/y)

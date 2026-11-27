import json, glob, numpy as np, math, sys, collections
from scipy.stats import norm
OUT="/home/user/pm"
# ---- BRTI 1s
pts={}
for f in glob.glob(OUT+"/brti-*.json"):
    for t,p in json.load(open(f)): pts[t]=p
ts=np.array(sorted(pts)); T0=ts[0]; N=ts[-1]-T0+1
px=np.full(N,np.nan); px[ts-T0]=[pts[t] for t in ts]
# forward fill gaps
idx=np.where(~np.isnan(px),np.arange(N),0); np.maximum.accumulate(idx,out=idx); px=px[idx]
lp=np.log(px); r=np.diff(lp,prepend=lp[0]); cs=np.concatenate([[0],np.cumsum(r*r)])
def I(t): return int(t-T0)
# ---- markets
M=[]
for l in open(OUT+"/markets.jsonl"):
    m=json.loads(l)
    if m["settlement"] not in (0,1) or not m["history"]: continue
    s=I(m["start"])
    if s-1000<0 or s+300>=N: continue
    m["open"]=px[s-59:s+1].mean(); m["close"]=px[s+241:s+301].mean()
    m["prices"]={h["timestamp"]-m["start"]:(h.get("longPrice"),h.get("shortPrice")) for h in m["history"]}
    M.append(m)
M.sort(key=lambda m:m["start"])
agree=np.mean([(m["close"]>=m["open"])==(m["settlement"]==1) for m in M])
print(f"markets {len(M)}  days {(M[-1]['start']-M[0]['start'])/86400:.1f}  UP rate {np.mean([m['settlement'] for m in M]):.3f}")
print(f"BRTI 60s-average rule reproduces official result: {agree:.2%}")
def fair(m,tau,k=1.0,W=60):
    """P(UP) knowing BRTI up to start+tau, settlement = mean of last W 1s prices."""
    s=I(m["start"]); e=s+300; kk=s+tau
    sig2=(cs[kk]-cs[kk-900])/900*k*k; op=lp[s-W+1:s+1].mean(); ws=e-W
    if kk>=ws: Kn=kk-ws; known=lp[ws+1:kk+1].sum(); U=W-Kn; d=0
    else: Kn=0; known=0; U=W; d=ws-kk
    mu=(known+U*lp[kk])/W; var=(U/W)**2*sig2*(d+U/3)
    if var<=1e-18: return 1.0 if mu>=op else 0.0
    return float(norm.cdf((mu-op)/math.sqrt(var)))
json.dump(dict(n=len(M)),open('/home/user/sims/meta.json','w'))

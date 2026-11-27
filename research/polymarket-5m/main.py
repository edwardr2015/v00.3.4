import json, glob, numpy as np, math, collections, itertools, sys
from scipy.stats import norm
exec(open('/home/user/sims/analyze.py').read().split("def fair(")[0].replace('print(f"BRTI','#'))
LAG=5          # seconds of extra caution between data used and trade time
SH,HALF=15,15  # empirical settlement window: labels [T-14, T+15]
def win_mean(c): return lp[c-HALF+1:c+SH+1].mean()
for m in M:
    s=I(m["start"]); m["ok"]=(math.exp(win_mean(s+300))>=math.exp(win_mean(s)))==(m["settlement"]==1)
print(f"markets {len(M)}  rule reproduces {np.mean([m['ok'] for m in M]):.2%}")
def fair(m,tau,k=1.0):
    s=I(m["start"]); e=s+300; kk=s+tau
    sig2=(cs[kk]-cs[kk-900])/900*k*k; op=win_mean(s); ws=e-HALF+1; we=e+SH; W=we-ws+1
    Kn=max(0,min(kk,we)-ws+1); known=lp[ws:ws+Kn].sum() if Kn else 0.0; U=W-Kn; d=max(0,ws-kk)
    mu=(known+U*lp[kk])/W; var=(U/W)**2*sig2*(d+U/3)
    if var<=1e-18: return 1.0 if mu>=op else 0.0
    return float(norm.cdf((mu-op)/math.sqrt(var)))
FEE=.11; SLIP=.01
def cost(p): return p+SLIP+FEE*p*(1-p)
# Build observation table: for each market and minute point k (price at ~k+59s)
rows=[]
for m in M:
    for k in (0,60,120,180):
        pr=m["prices"].get(k)
        if not pr or pr[0] is None or pr[1] is None: continue
        t_dec=k+59
        f=fair(m,t_dec-LAG)
        rows.append((m["start"],k,pr[0],pr[1],f,m["settlement"]))
R=np.array(rows); y=R[:,5]
print(f"observations {len(R)}")
days=(R[:,0]-R[:,0].min())/86400; split=np.quantile(R[:,0],.6); TR=R[:,0]<split; TE=~TR
print(f"train {TR.sum()} obs / test {TE.sum()} obs (later {100*(1-.6):.0f}% of days)")
# 1. Who forecasts better, by minute
mid=(R[:,2]+(1-R[:,3]))/2
print("\n1) Forecast error by minute (Brier, lower better)")
for k in (0,60,120,180):
    s=R[:,1]==k
    print(f"   price at ~{int(k)+59:3d}s: market {np.mean((mid[s]-y[s])**2):.4f}  correct-rule model {np.mean((R[s,4]-y[s])**2):.4f}")
# 2. Market calibration (favourite-longshot bias?)
print("\n2) Market calibration: UP price vs how often UP won")
for lo in np.arange(0,1,.1):
    s=(R[:,2]>=lo)&(R[:,2]<lo+.1)
    if s.sum()>30: print(f"   {lo:.1f}-{lo+.1:.1f}: n={s.sum():5d} won {y[s].mean():.3f}  avg price {R[s,2].mean():.3f}  gap {y[s].mean()-R[s,2].mean():+.3f}")
# 3. Strategy search on train, evaluate on test
def trades(mask, edge, kmin, kmax, pmin, pmax, mode="fair"):
    out=[]
    for i in np.where(mask)[0]:
        st,k,up,dn,f,o=R[i]
        if not (kmin<=k<=kmax): continue
        best=None
        for side,p,q in (("UP",up,f),("DOWN",dn,1-f)):
            if not (pmin<=p<=pmax): continue
            c=cost(p)
            if mode=="fair": ev=q-c
            elif mode=="cheap": ev=1.0 if side==("UP" if f>=.5 else "DOWN") else -1
            if ev>=edge and (best is None or ev>best[0]): best=(ev,side,c)
        if best:
            ev,side,c=best; win=(o==1)==(side=="UP"); out.append(((1-c) if win else -c)/c)  # return per $ staked
    return np.array(out)
grid=list(itertools.product([0,.02,.04,.06,.08,.10,.15],[0,60,120,180],[0,60,120,180],[.05,.2,.3,.4],[.6,.7,.8,.95]))
res=[]
for edge,kmin,kmax,pmin,pmax in grid:
    if kmax<kmin or pmax<=pmin: continue
    t=trades(TR,edge,kmin,kmax,pmin,pmax)
    if len(t)>=40: res.append((t.mean(),len(t),edge,kmin,kmax,pmin,pmax))
res.sort(reverse=True)
print(f"\n3) Searched {len(res)} rule variants on train days. Top 5, then the same rule on unseen test days (return per $1 staked):")
for r_ in res[:5]:
    te=trades(TE,*r_[2:])
    print(f"   edge>={r_[2]:.2f} minutes {int(r_[3])}-{int(r_[4])} price {r_[5]:.2f}-{r_[6]:.2f}: train {r_[0]:+.3f} on {r_[1]} | TEST {te.mean() if len(te) else float('nan'):+.3f} on {len(te)}  (test total on $5 bets ${5*te.sum():+.0f})")
# null: what does the best train rule look like if outcomes are shuffled?
rng=np.random.default_rng(0); null=[]
for _ in range(20):
    perm=R.copy(); 
    # shuffle outcomes across markets (keep per-market consistency)
    us=np.unique(R[:,0]); mp=dict(zip(us,rng.permutation([R[R[:,0]==u][0,5] for u in us])))
    R_save=R[:,5].copy(); R[:,5]=[mp[u] for u in R[:,0]]
    best=max((trades(TR,*g).mean() for g in [r_[2:] for r_ in res[:50]] if len(trades(TR,*g))>=40),default=0)
    null.append(best); R[:,5]=R_save
print(f"   luck check: best train result with shuffled outcomes ~{np.median(null):+.3f} (if real top is not well above this, it's luck)")
np.save('/home/user/sims/R.npy',R)

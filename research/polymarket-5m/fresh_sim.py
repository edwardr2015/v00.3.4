import numpy as np, math
exec(open('/home/user/sims/main.py').read().split("FEE=.11")[0])   # loads BRTI, markets, fair(), win_mean
# Market price point at label k is the market around real time k+30 (found earlier). The index history
# labels run ~29 s late, so information available at real time T sits at label T+29.
DELAY=29
def at_real(m, real_t, k=1.0):
    return fair(m, int(real_t + DELAY), k)
rows=[]
for m in M:
    for k in (0,60,120,180):
        pr=m["prices"].get(k)
        if not pr or None in pr: continue
        real=k+30
        rows.append((m["start"],k,(pr[0]+1-pr[1])/2,m["settlement"],m,real))
split=np.quantile([r[0] for r in rows],.6)
def brier(vals): return float(np.mean(vals))
for label,real_offset in (("STALE (old feed, 29 s late)",-DELAY),("FRESH (live)",0)):
    for kscale in (1.0,1.5,2.0,2.5):
        tr=[(at_real(r[4],r[5]+real_offset,kscale)-r[3])**2 for r in rows if r[0]<split]
        print(f"  {label:28s} vol x{kscale}: train error {brier(tr):.4f}")
# choose best k on train for each, report test vs market by minute
def best_k(off):
    return min((1.0,1.25,1.5,2.0,2.5,3.0),key=lambda kk:brier([(at_real(r[4],r[5]+off,kk)-r[3])**2 for r in rows if r[0]<split]))
ks={off:best_k(off) for off in (-DELAY,0)}
print("\nUnseen later days: forecast error (lower is better)")
print(f"{'minute':>8} {'market price':>13} {'model, stale feed':>18} {'model, fresh feed':>18}")
for k in (0,60,120,180):
    te=[r for r in rows if r[0]>=split and r[1]==k]
    mk=brier([(r[2]-r[3])**2 for r in te]); st=brier([(at_real(r[4],r[5]-DELAY,ks[-DELAY])-r[3])**2 for r in te]); fr=brier([(at_real(r[4],r[5],ks[0])-r[3])**2 for r in te])
    print(f"{'~'+str(k+30)+'s':>8} {mk:13.4f} {st:18.4f} {fr:18.4f}")
# trading: buy when fresh model beats market price + costs
FEE=.11;SLIP=.01
def cost(p): return p+SLIP+FEE*p*(1-p)
for label,off in (("stale feed",-DELAY),("fresh feed",0)):
    for edge in (.03,.06,.10):
        res=[]
        for r in rows:
            if r[0]<split: continue
            f=at_real(r[4],r[5]+off,ks[off]); up=r[2]+.006; dn=1-r[2]+.006
            for side,p,q in (("UP",up,f),("DOWN",dn,1-f)):
                c=cost(p)
                if .05<p<.95 and q-c>=edge:
                    win=(r[3]==1)==(side=="UP"); res.append(((1-c) if win else -c)/c); break
        res=np.array(res)
        print(f"  trade on {label}, edge>={edge:.2f}: {len(res)} trades, avg {res.mean() if len(res) else float('nan'):+.3f} per $1, win {np.mean(res>0) if len(res) else 0:.0%}")

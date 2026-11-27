import sys
src=open('/home/user/sims/mkt.py').read()
exec(src)
print("\n=== Same rules with NO fees and NO slippage (best case for limit orders) ===")
FEE=0; SLIP=0
def cost(p): return p
res=[(evalr(rules[nm],te).mean(),len(evalr(rules[nm],te)),nm) for _,_,nm in out]
pos=[r for r in res if r[0]>0]
print(f"profitable on test days with zero costs: {len(pos)}/{len(res)}; median rule {np.median([r[0] for r in res]):+.4f}")
# market calibration on all data, minute 1 and 2 points, with confidence intervals
print("\n=== Calibration, all markets: UP price -> UP win rate (95% range) ===")
for k in (0,120):
    print(f" price at ~{k+59}s")
    for lo in np.arange(.1,.9,.1):
        y=[m["settlement"] for m in M if lo<=m["P"][k][0]<lo+.1]; p=[m["P"][k][0] for m in M if lo<=m["P"][k][0]<lo+.1]
        if len(y)>50:
            y=np.array(y); se=1.96*np.sqrt(y.mean()*(1-y.mean())/len(y))
            print(f"   {lo:.1f}-{lo+.1:.1f}: n={len(y):4d} price {np.mean(p):.3f} won {y.mean():.3f} ±{se:.3f}")

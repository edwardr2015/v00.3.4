exec(open('/home/user/sims/main.py').read().split("FEE=.11")[0])
y=[];mk=[];F={x:[] for x in (-30,0,15,30,45,60,90)}
for m in M:
    pr=m["prices"].get(60)
    if not pr or pr[0] is None: continue
    y.append(m["settlement"]); mk.append((pr[0]+1-pr[1])/2)
    for x in F: F[x].append(fair(m,119+x))
y=np.array(y);mk=np.array(mk)
print("market @~119s Brier",round(np.mean((mk-y)**2),4))
for x,v in F.items(): v=np.array(v); print(f" model using index labels up to {119+x}s: Brier {np.mean((v-y)**2):.4f}  corr with market {np.corrcoef(v,mk)[0,1]:.3f}")

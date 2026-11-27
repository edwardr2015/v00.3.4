import json, numpy as np, sys
FEE=.11; SLIP=.01
def cost(p): return p+SLIP+FEE*p*(1-p)
def load(f,L):
    M=[]
    for l in open(f):
        m=json.loads(l)
        if m["settlement"] not in (0,1) or not m["history"]: continue
        P={h["timestamp"]-m["start"]:(h.get("longPrice"),h.get("shortPrice")) for h in m["history"]}
        pts=sorted(k for k,v in P.items() if 0<=k<L and v[0] is not None and v[1] is not None)
        if len(pts)<4: continue
        m["P"]=P; m["pts"]=pts; M.append(m)
    M.sort(key=lambda m:m["start"]); return M
def run(name,f,L):
    M=load(f,L); n=len(M); cut=int(n*.6)
    print(f"\n=== {name}: {n} markets ===")
    fr=(.1,.25,.5,.75)
    def at(m,q):
        k=min(m["pts"],key=lambda x:abs(x-q*L)); return m["P"][k]
    # calibration across all points at ~25% and ~50% of window
    for q in (.25,.5):
        print(f" calibration at {int(q*100)}% of window:",end="")
        for lo in (.2,.3,.4,.5,.6,.7):
            s=[(at(m,q)[0],m["settlement"]) for m in M if lo<=at(m,q)[0]<lo+.1]
            if len(s)>25: a=np.array(s); print(f" {lo:.1f}-{lo+.1:.1f}: paid {a[:,0].mean():.2f} won {a[:,1].mean():.2f} (n={len(a)}) |",end="")
        print()
    rules={}
    for q in fr:
        for lo,hi in [(.2,.3),(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,.97)]:
            def r(m,q=q,lo=lo,hi=hi):
                up,dn=at(m,q)
                for side,p in (("UP",up),("DOWN",dn)):
                    if lo<=p<hi and (p>=.5)==(lo>=.5): return side,p
            rules[f"band {int(q*100)}% {lo}-{hi}"]=r
        for d in (.1,.2):
            for follow in (True,False):
                def r(m,q=q,d=d,follow=follow):
                    if q==.1: return None
                    a=at(m,q-.15 if q>.25 else .1)[0]; up,dn=at(m,q); ch=up-a
                    if abs(ch)<d: return None
                    return ("UP",up) if (ch>0)==follow else ("DOWN",dn)
                rules[f"{'momentum' if follow else 'fade'} {int(q*100)}% {d}"]=r
    def ev(fn,ms):
        out=[]
        for m in ms:
            x=fn(m)
            if x:
                side,p=x; c=cost(p); win=(m["settlement"]==1)==(side=="UP"); out.append(((1-c) if win else -c)/c)
        return np.array(out)
    res=[]
    for nm,fn in rules.items():
        a=ev(fn,M[:cut])
        if len(a)>=25: res.append((a.mean(),len(a),nm))
    res.sort(reverse=True)
    pos=0
    for _,_,nm in res:
        b=ev(rules[nm],M[cut:])
        if len(b) and b.mean()>0: pos+=1
    print(f" {len(res)} rules: profitable on later {n-cut} markets: {pos}/{len(res)}")
    for mean,cnt,nm in res[:5]:
        b=ev(rules[nm],M[cut:]); print(f"   {nm:22s} earlier {mean:+.3f} ({cnt}) -> later {b.mean() if len(b) else float('nan'):+.3f} ({len(b)})")
    allr=[ev(rules[nm],M).mean() for _,_,nm in res]; print(f" median rule over all markets: {np.median(allr):+.3f} per $1")
for name,f,L in [("5-minute","/home/user/pm/markets.jsonl",300),("15-minute","/home/user/pm/markets-15m.jsonl",900),("1-hour","/home/user/pm/markets-1h.jsonl",3600)]:
    try: run(name,f,L)
    except FileNotFoundError: print(name,"not downloaded yet")

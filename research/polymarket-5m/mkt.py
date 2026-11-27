import json, numpy as np, itertools, datetime as dt, collections
FEE=.11; SLIP=.01
def cost(p): return p+SLIP+FEE*p*(1-p)
M=[]
for l in open('/home/user/pm/markets.jsonl'):
    m=json.loads(l)
    if m["settlement"] not in (0,1) or not m["history"]: continue
    P={h["timestamp"]-m["start"]:(h.get("longPrice"),h.get("shortPrice")) for h in m["history"]}
    if any(P.get(k) is None or None in P.get(k) for k in (0,60,120,180,240)): continue
    m["P"]=P; M.append(m)
M.sort(key=lambda m:m["start"])
for i,m in enumerate(M): m["prev"]=M[i-1]["settlement"] if i and M[i-1]["start"]==m["start"]-300 else None
n=len(M); cut=int(n*.6)
print(f"{n} markets, {(M[-1]['start']-M[0]['start'])/86400:.1f} days; train first {cut}, test last {n-cut}")
def ret(m,side,p):
    c=cost(p); win=(m["settlement"]==1)==(side=="UP"); return ((1-c) if win else -c)/c
# rule = function(m) -> (side, price) or None
def rule_band(k,lo,hi,fav):
    def f(m):
        up,dn=m["P"][k]
        for side,p in (("UP",up),("DOWN",dn)):
            if lo<=p<hi and ((p>=.5)==fav): return side,p
    return f
def rule_mom(k,d,follow):
    def f(m):
        up0=m["P"][k-60][0]; up,dn=m["P"][k]; ch=up-up0
        if abs(ch)<d: return None
        goes_up=(ch>0)==follow
        return ("UP",up) if goes_up else ("DOWN",dn)
    return f
def rule_prev(k,follow):
    def f(m):
        if m["prev"] is None: return None
        up,dn=m["P"][k]; wantup=(m["prev"]==1)==follow
        return ("UP",up) if wantup else ("DOWN",dn)
    return f
def rule_hour(k,h0,h1,fav,lo,hi):
    base=rule_band(k,lo,hi,fav)
    def f(m):
        h=dt.datetime.fromtimestamp(m["start"],dt.timezone.utc).hour
        return base(m) if h0<=h<h1 else None
    return f
rules={}
K=(0,60,120,180,240)
for k in K:
    for lo,hi in [(.03,.1),(.1,.2),(.2,.3),(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.7,.8),(.8,.9),(.9,.97),(.97,.995)]:
        rules[f"band k={k} {lo}-{hi}"]=rule_band(k,lo,hi,lo>=.5)
for k in K[1:]:
    for d in (.05,.1,.2,.3):
        for fo in (True,False): rules[f"{'momentum' if fo else 'fade'} k={k} move>={d}"]=rule_mom(k,d,fo)
for k in K:
    for fo in (True,False): rules[f"{'repeat' if fo else 'reverse'} prev k={k}"]=rule_prev(k,fo)
for k in (0,120,240):
    for h0 in range(0,24,4):
        for lo,hi in [(.4,.6),(.6,.8),(.8,.97)]:
            rules[f"hour {h0}-{h0+4} k={k} {lo}-{hi}"]=rule_hour(k,h0,h0+4,lo>=.5,lo,hi)
def evalr(f,ms):
    r=[]
    for m in ms:
        x=f(m)
        if x: r.append(ret(m,*x))
    return np.array(r)
tr,te=M[:cut],M[cut:]
out=[]
for name,f in rules.items():
    a=evalr(f,tr)
    if len(a)>=60: out.append((a.mean(),len(a),name))
out.sort(reverse=True)
print(f"\nTested {len(out)} market-only rules. Best 12 on train days -> result on unseen test days (return per $1, after fees):")
for mean,cnt,name in out[:12]:
    b=evalr(rules[name],te); se=b.std()/np.sqrt(len(b)) if len(b)>1 else 0
    print(f"  {name:32s} train {mean:+.3f} ({cnt:4d}) | TEST {b.mean():+.3f} ±{1.96*se:.3f} ({len(b)})")
pos=sum(1 for _,_,nm in out if evalr(rules[nm],te).mean()>0)
print(f"\nRules profitable on test days: {pos}/{len(out)}")
allm=[(evalr(rules[nm],M).mean(),nm) for _,_,nm in out]; allm.sort(reverse=True)
print("Whole-period best 5 (for reference, in-sample):",[(round(a,3),b) for a,b in allm[:5]])

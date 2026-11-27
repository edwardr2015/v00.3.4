import openpyxl, json, numpy as np, datetime as dt, math
from scipy.stats import norm
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_predict
d=np.load('/home/user/btcdata/grid.npz'); T=d['t']; lp=np.log(d['p']); r=np.diff(lp,prepend=lp[0]); cs=np.concatenate([[0],np.cumsum(r*r)]); t0=T[0]
def utc(s): return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
def start_of(slug):
    d_,hm=slug.split('-5m-')[1][:10],slug[-5:-1]; return utc(f"{d_} {hm[:2]}:{hm[2:]}:00")
outcome={}
for f in ['ebd8cbcc-BTC-Prophet-Quick-Report-17.xlsx','45beeed5-BTC-Prophet-Quick-Report-18.xlsx']:
    wb=openpyxl.load_workbook('/root/.claude/uploads/1d77e9db-8273-5fad-8a42-b8427e6883f7/'+f,read_only=True)
    for sh,cs_,co in [('Unified Trades',0,13),('Recent Pooled Research',1,5),('Recent Model Trades',1,5)]:
        for row in wb[sh].iter_rows(min_row=2,values_only=True):
            if len(row)>co and row[cs_] and row[co] in ('UP','DOWN'): outcome[row[cs_]]=row[co]
def agreement(off):
    a=[];m=[]
    for slug,o in outcome.items():
        s=int(start_of(slug)-t0)+off
        if 0<=s and s+300<len(lp): mv=lp[s+300]-lp[s]; a.append((mv>0)==(o=='UP')); m.append(abs(mv)*1e4)
    return np.array(a),np.array(m)
a,m=agreement(0)
print(f"1) Binance direction vs official result: {a.mean():.1%} agree over {len(a)} rounds")
for lo,hi in [(0,2),(2,5),(5,1000)]:
    s=(m>=lo)&(m<hi); print(f"   Binance moved {lo}-{hi} bps: {a[s].mean():.0%} agree (n={s.sum()})")
print('   by time offset:',{off:round(agreement(off)[0].mean(),3) for off in [-30,-10,-5,-2,0,2,5,10,30]})
X=[];Y=[]
wb=openpyxl.load_workbook('/root/.claude/uploads/1d77e9db-8273-5fad-8a42-b8427e6883f7/45beeed5-BTC-Prophet-Quick-Report-18.xlsx',read_only=True)
for row in wb['Unified Observations'].iter_rows(min_row=2,values_only=True):
    slug=row[0]; e=json.loads(row[5] or '{}')
    if slug not in outcome or not e.get('midpoint') or not e.get('book_timestamp'): continue
    st=start_of(slug); k=int(e['book_timestamp']-t0); s=int(st-t0)
    if k>=len(lp) or s<900: continue
    left=max(300-(e['book_timestamp']-st),1); sig=math.sqrt((cs[k]-cs[k-900])/900)
    fair=min(.999,max(.001,norm.cdf((lp[k]-lp[s])/(sig*math.sqrt(left)))))
    mm=min(.99,max(.01,e['midpoint'])); X.append([math.log(mm/(1-mm)), math.log(fair/(1-fair))]); Y.append(outcome[slug]=='UP')
X=np.array(X);Y=np.array(Y)
pm=cross_val_predict(LogisticRegression(),X[:,[0]],Y,cv=10,method='predict_proba')[:,1]
pb=cross_val_predict(LogisticRegression(),X,Y,cv=10,method='predict_proba')[:,1]
raw=1/(1+np.exp(-X[:,0]))
print(f"2) Latency test, {len(Y)} rounds: error market alone {np.mean((raw-Y)**2):.4f} | market refit {np.mean((pm-Y)**2):.4f} | market + exchange price {np.mean((pb-Y)**2):.4f}")
print("   weights [market, exchange]:",LogisticRegression().fit(X,Y).coef_.round(2))
print('fine offsets:')
best=[]
for off in range(-120,31,5):
    a,_=agreement(off); best.append((a.mean(),off)); print(f"  {off:+4d}s {a.mean():.3f}")
# averaged-price hypothesis: official = average of last W seconds?
def agree_avg(W,off=0):
    a=[]
    for slug,o in outcome.items():
        s=int(start_of(slug)-t0)+off
        if s-W>=0 and s+300<len(lp):
            op=lp[s-W+1:s+1].mean(); cl=lp[s+300-W+1:s+301].mean(); a.append((cl>op)==(o=='UP'))
    return np.mean(a)
print('average-of-last-W-seconds:',{W:round(agree_avg(W),3) for W in [1,10,30,60]})

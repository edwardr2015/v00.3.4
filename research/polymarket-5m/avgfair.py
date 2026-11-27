import numpy as np, math
exec(open('lat.py').read().split("a,m=agreement(0)")[0])
W=60
def fair_avg(t, st):
    """P(avg price over last W s of round > avg over W s before start), knowing exchange prices up to t."""
    k=int(t-t0); s=int(st-t0); e=s+300
    if k-900<0 or e>=len(lp): return None
    sig2=(cs[k]-cs[k-900])/900
    op=lp[s-W+1:s+1].mean()
    ws=e-W  # window start
    if k>=ws:
        Kn=k-ws+1; known=lp[ws+1:k+1].mean() if Kn>0 else 0; U=W-Kn; d=0
    else:
        Kn=0; known=0; U=W; d=ws-k
    mu=(Kn*known+U*lp[k])/W; var=(U/W)**2*sig2*(d+U/3)
    if var<=0: return float(mu>op)
    return norm.cdf((mu-op)/math.sqrt(var))
X=[];Y=[];F=[];M=[];EL=[]
wb=openpyxl.load_workbook('/root/.claude/uploads/1d77e9db-8273-5fad-8a42-b8427e6883f7/45beeed5-BTC-Prophet-Quick-Report-18.xlsx',read_only=True)
for row in wb['Unified Observations'].iter_rows(min_row=2,values_only=True):
    slug=row[0]; e=json.loads(row[5] or '{}')
    if slug not in outcome or not e.get('midpoint') or not e.get('book_timestamp'): continue
    st=start_of(slug); f=fair_avg(e['book_timestamp'],st)
    if f is None: continue
    f=min(.999,max(.001,f)); mm=min(.99,max(.01,e['midpoint']))
    X.append([math.log(mm/(1-mm)),math.log(f/(1-f))]); Y.append(outcome[slug]=='UP'); F.append(f); M.append(mm); EL.append(e['book_timestamp']-st)
X=np.array(X);Y=np.array(Y);F=np.array(F);M=np.array(M)
pb=cross_val_predict(LogisticRegression(),X,Y,cv=10,method='predict_proba')[:,1]
print(f"{len(Y)} rounds (one observation each, mostly late in round, median {np.median(EL):.0f}s)")
print(f" forecast error: market {np.mean((M-Y)**2):.4f} | exchange-average model {np.mean((F-Y)**2):.4f} | combined {np.mean((pb-Y)**2):.4f}")
print(" weights [market, exchange-avg]:",LogisticRegression().fit(X,Y).coef_.round(2))
dis=np.abs(F-M)>.15
print(f" rounds where model and market disagree by >15c: {dis.sum()}, model right {np.mean((F[dis]>.5)==Y[dis]):.0%}, market right {np.mean((M[dis]>.5)==Y[dis]):.0%}")

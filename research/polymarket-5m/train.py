import numpy as np, pickle
from scipy.stats import norm
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
X=np.load('/home/user/sims/dataset.npy')
cols=['start','c','gap','z','volratio','m10','m30','m60','hi','lo','vr','hour','prev','up']
F=[1,3,2,4,5,6,7,8,9,10,11,12]   # features
SIGNED=[3,2,5,6,7,12]             # flip sign for mirror
split=np.quantile(np.unique(X[:,0]),2/3)
tr=X[X[:,0]<split]; te=X[X[:,0]>=split]
def mirror(A):
    B=A.copy(); B[:,SIGNED]*=-1; B[:,8],B[:,9]=-A[:,9],-A[:,8]; B[:,13]=1-A[:,13]; return B
def brier(p,y): return np.mean((p-y)**2)
res={}
base_te=norm.cdf(te[:,3])
# calibrated baseline: logistic on z only
lz=LogisticRegression().fit(tr[:,[3]],tr[:,13]); 
for name,train in [('plain',tr),('mirror',np.vstack([tr,mirror(tr)]))]:
    m=HistGradientBoostingClassifier(max_iter=300,learning_rate=.05,max_leaf_nodes=31,l2_regularization=1.0,random_state=0).fit(train[:,F],train[:,13])
    res[name]=m
    pickle.dump(m,open(f'/home/user/sims/gbm_{name}.pkl','wb'))
pg={k:m.predict_proba(te[:,F])[:,1] for k,m in res.items()}
pl=lz.predict_proba(te[:,[3]])[:,1]
y=te[:,13]
print('test rounds',len(np.unique(te[:,0])),'train rounds',len(np.unique(tr[:,0])))
print(f"{'sec':>4} {'gap-only':>9} {'gap-calib':>9} {'smart':>7} {'smart+flip':>10}  improvement")
for c in np.unique(te[:,1])[::2]:
    s=te[:,1]==c
    b=brier(base_te[s],y[s]); b2=brier(pl[s],y[s]); g1=brier(pg['plain'][s],y[s]); g2=brier(pg['mirror'][s],y[s])
    print(f"{int(c):4d} {b:9.4f} {b2:9.4f} {g1:7.4f} {g2:10.4f}  {100*(b-g2)/b:5.1f}%")
print('ALL',brier(base_te,y),brier(pl,y),brier(pg['plain'],y),brier(pg['mirror'],y))

import numpy as np, sys
d=np.load('/home/user/btcdata/grid.npz'); T=d['t']; P=d['p']; V=d['v']
lp=np.log(P); r=np.diff(lp,prepend=lp[0])
t0=T[0]; first=int(np.ceil((t0+900)/300)*300)  # need 15 min history
starts=np.arange(first, T[-1]-300, 300)
CK=np.arange(15,286,15)
cs=np.cumsum(r*r); cs=np.concatenate([[0],cs]); cv=np.concatenate([[0],np.cumsum(V)])
def i(ts): return (ts-t0).astype(int)
rows=[]
S=i(starts); O=lp[S]; C=lp[S+300]; up=(C>O)
prevret=np.concatenate([[0],(C-O)[:-1]])
for c in CK:
    k=S+c
    gap=(lp[k]-O)*1e4
    var300=(cs[k]-cs[k-300])/300; var900=(cs[k]-cs[k-900])/900
    sig=np.sqrt(np.maximum(var900,1e-12))
    z=(lp[k]-O)/(sig*np.sqrt(300-c))
    m10=(lp[k]-lp[k-10])*1e4; m30=(lp[k]-lp[k-30])*1e4; m60=(lp[k]-lp[k-60])*1e4
    hi=np.array([lp[a:b+1].max() for a,b in zip(S,k)])-O; lo=np.array([lp[a:b+1].min() for a,b in zip(S,k)])-O
    vr=(cv[k]-cv[k-60])/np.maximum((cv[k-60]-cv[k-660])/10,1e-9)
    hour=((starts%86400)//3600)
    rows.append(np.column_stack([starts,np.full(len(S),c),gap,z,np.sqrt(var300/np.maximum(var900,1e-12)),m10,m30,m60,hi*1e4,lo*1e4,vr,hour,prevret*1e4,up]))
X=np.vstack(rows)
np.save('/home/user/sims/dataset.npy',X); print(X.shape, 'rounds',len(starts),'up rate',up.mean())

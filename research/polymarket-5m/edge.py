import numpy as np, json, openpyxl, datetime as dt, collections
from scipy.stats import norm
d=np.load('/home/user/btcdata/grid.npz'); T=d['t']; lp=np.log(d['p']); r=np.diff(lp,prepend=lp[0]); cs=np.concatenate([[0],np.cumsum(r*r)])
t0=T[0]
def fair_up(ts,start):
    k=int(ts-t0); s=int(start-t0); left=max(300-(ts-start),1)
    if k>=len(lp) or s<900: return None
    sig=np.sqrt((cs[k]-cs[k-900])/900)
    return norm.cdf((lp[k]-lp[s])/(sig*np.sqrt(left)))
def start_of(slug):
    d_,hm=slug.split('-5m-')[1][:10],slug[-5:-1]
    return dt.datetime.fromisoformat(f"{d_} {hm[:2]}:{hm[2:]}:00").replace(tzinfo=dt.timezone.utc).timestamp()
def ts(s): return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
out=[]
seen=set()
for f in ['ebd8cbcc-BTC-Prophet-Quick-Report-17.xlsx','45beeed5-BTC-Prophet-Quick-Report-18.xlsx']:
    wb=openpyxl.load_workbook('/root/.claude/uploads/1d77e9db-8273-5fad-8a42-b8427e6883f7/'+f,read_only=True)
    for r_ in wb['Entry Pricing Audit'].iter_rows(min_row=2,values_only=True):
        model,slug,side,et,ask,px,allin,fee,q,outc,pnl=r_[:11]
        if not slug or outc not in ('UP','DOWN'): continue
        key=(model,slug,side,et)
        if key in seen: continue
        seen.add(key)
        st=start_of(slug); fu=fair_up(ts(et),st)
        if fu is None: continue
        fair=fu if side=='UP' else 1-fu
        win=outc==side
        out.append(dict(model=model,slug=slug,el=ts(et)-st,fair=fair,cost=allin,price=px,win=win,pnl=(1-allin)*q if win else -allin*q,q=q))
print(len(out),'entries with fair value;',len({o['slug'] for o in out}),'markets')
# calibration check: does fair value predict wins in these real markets?
fv=np.array([o['fair'] for o in out]); w=np.array([o['win'] for o in out]); c=np.array([o['cost'] for o in out])
print('Brier fair',np.mean((fv-w)**2).round(4),' Brier market price',np.mean((np.array([o['price'] for o in out])-w)**2).round(4))
print('\nEdge = fair value - all-in cost per contract (cents)')
for lo,hi in [(-1,-.2),(-.2,-.1),(-.1,0),(0,.05),(.05,.1),(.1,.2),(.2,1)]:
    s=(fv-c>=lo)&(fv-c<hi)
    if s.sum()==0: continue
    sub=[o for o,k in zip(out,s) if k]
    print(f"  {lo*100:+4.0f} to {hi*100:+4.0f}: n={s.sum():4d} markets={len({o['slug'] for o in sub}):3d} win={w[s].mean():.0%} avg cost={c[s].mean():.2f} avg fair={fv[s].mean():.2f} net per $5≈{np.mean([o['pnl']/ (o['cost']*o['q']) for o in sub])*5:+.2f}")
json.dump(out,open('/home/user/sims/entries.json','w'))

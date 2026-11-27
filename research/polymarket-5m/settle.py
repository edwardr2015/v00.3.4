import numpy as np, datetime as dt
exec(open('lat.py').read().split("a,m=agreement(0)")[0])
P=np.exp(lp)
def test(Wo,Wc,oo=0,co=0,kind='mean'):
    a=[]
    for slug,o in outcome.items():
        s=int(start_of(slug)-t0); e=s+300
        if s-Wo-abs(oo)<0 or e+abs(co)>=len(lp): continue
        f=np.mean if kind=='mean' else np.median
        op=f(P[s+oo-Wo+1:s+oo+1]) if Wo else P[s]; cl=f(P[e+co-Wc+1:e+co+1]) if Wc else P[e]
        a.append((cl>op)==(o=='UP'))
    return np.mean(a),len(a)
print('open avg W / close avg W -> agreement')
for Wo in [1,30,60,90]:
    print(' open',Wo,{Wc:round(test(Wo,Wc)[0],3) for Wc in [1,30,45,60,75,90,120]})
print('open = single price at start, close avg:',{Wc:round(test(0,Wc)[0],3) for Wc in [30,60,90]})
print('median 60/60:',round(test(60,60,kind='median')[0],3))
print('n rounds',test(60,60)[1])

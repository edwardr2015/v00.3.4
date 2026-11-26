"""Calibration and dependence summaries for settled, matched research forecasts."""
import math
from itertools import combinations


def scores(pairs):
    n = len(pairs)
    return dict(count=n,correct=sum(p!=.5 and (p>.5)==y for p,y in pairs),
                brier=sum((p-y)**2 for p,y in pairs)/n if n else None,
                log_loss=-sum(math.log(max(1e-9,p if y else 1-p)) for p,y in pairs)/n if n else None)


def reliability(model, pairs):
    bins = {}
    for p,y in pairs:
        bins.setdefault(min(9,int(p*10)),[]).append((p,y))
    return [dict(model=model,band=band,count=len(values),
                 mean_up=sum(p for p,_ in values)/len(values),
                 observed_up=sum(y for _,y in values)/len(values))
            for band,values in sorted(bins.items())]


def panel_dependence(panels):
    paired = {}
    for components,outcome in panels:
        for a,b in combinations(sorted(components),2):
            first,second = components[a]["up"],components[b]["up"]
            if first==.5 or second==.5:
                continue
            row = paired.setdefault((a,b),[0,0,0,0])
            e1,e2 = (first>.5)!=outcome,(second>.5)!=outcome
            row[0] += 1
            row[1] += e1
            row[2] += e2
            row[3] += e1 and e2
    results = []
    for (a,b),(n,first,second,both) in sorted(paired.items()):
        f,s = first/n,second/n
        denominator = math.sqrt(f*(1-f)*s*(1-s))
        results.append(dict(first=a,second=b,markets=n,both_wrong=both,
                            error_correlation=(both/n-f*s)/denominator if denominator else None))
    return results

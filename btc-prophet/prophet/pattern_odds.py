"""Lightweight chronological sequence and market-pattern research."""
from collections import Counter
from itertools import groupby
import json
import math
from statistics import fmean, pstdev

VERSION = "pattern-odds-v1"
MIN_OUTCOMES = 20


def sequence_analysis(outcomes):
    """Fit Beta-smoothed suffix probabilities without adding a synthetic outcome."""
    n = len(outcomes)
    prior = (outcomes.count("UP")+2)/(n+4)
    suffixes = []
    for k in range(1,7):
        key = tuple(outcomes[-k:])
        followers = [outcomes[i] for i in range(k,n) if tuple(outcomes[i-k:i])==key]
        ups = followers.count("UP")
        mass = len(followers)+4
        p = (ups+4*prior)/mass
        suffixes.append(dict(order=k,sequence=list(key),occurrences=len(followers),up=ups,
                             up_probability=p,sd=math.sqrt(p*(1-p)/(mass+1))))
    transitions = Counter(zip(outcomes,outcomes[1:]))
    matrix = {a:{b:(transitions[a,b]+1)/(sum(transitions[a,x] for x in ("UP","DOWN"))+2)
                 for b in ("UP","DOWN")} for a in ("UP","DOWN")}
    runs = Counter((value,len(list(group))) for value,group in groupby(outcomes))
    a = outcomes.count("UP")
    b = n-a
    observed = sum(runs.values())
    mu = 1+2*a*b/n if n else None
    variance = 2*a*b*(2*a*b-n)/(n*n*(n-1)) if a and b and n>1 else 0
    return dict(outcomes=n,prior_up=prior,suffixes=suffixes,transition=matrix,
                runs={f"{side} {length}":count for (side,length),count in sorted(runs.items())},
                runs_z=(observed-mu)/math.sqrt(variance) if variance>0 else None,
                continuations=sum(transitions[x,x] for x in ("UP","DOWN")),
                reversals=transitions["UP","DOWN"]+transitions["DOWN","UP"],
                alternating_triplets=sum(a==c and a!=b for a,b,c in zip(outcomes,outcomes[1:],outcomes[2:])))


def ema(xs, period):
    alpha = 2/(period+1)
    value = xs[0]
    for x in xs[1:]:
        value += alpha*(x-value)
    return value


def technical(closes):
    if len(closes)<26:
        return {}
    returns = [math.log(b/a) for a,b in zip(closes,closes[1:])]
    short_vol,long_vol = pstdev(returns[-6:]),pstdev(returns[-24:])
    avg = fmean(closes[-20:])
    band = 2*pstdev(closes[-20:])
    support,resistance = min(closes[-21:-1]),max(closes[-21:-1])
    gains = sum(max(r,0) for r in returns[-14:])
    losses = sum(max(-r,0) for r in returns[-14:])
    return dict(momentum=closes[-1]/closes[-5]-1,trend_persistence=fmean(r>0 for r in returns[-12:]),
                mean_reversion_distance=closes[-1]/avg-1,support=support,resistance=resistance,
                breakout="UP" if closes[-1]>resistance else "DOWN" if closes[-1]<support else None,
                false_breakout=(closes[-2]>max(closes[-22:-2]) and closes[-1]<=max(closes[-22:-2])
                                or closes[-2]<min(closes[-22:-2]) and closes[-1]>=min(closes[-22:-2])),
                rsi=100*gains/(gains+losses) if gains+losses else 50,
                macd=ema(closes,12)-ema(closes,26),bollinger_low=avg-band,bollinger_high=avg+band,
                moving_average_5=fmean(closes[-5:]),moving_average_20=avg,short_volatility=short_vol,
                long_volatility=long_vol,regime="expansion" if short_vol>1.25*long_vol
                else "compression" if long_vol and short_vol<.75*long_vol else "steady")


def forecast(store,slug,checkpoint,now):
    with store.connect() as db:
        captured = db.execute("""SELECT d.details,b.payload FROM v2_decisions d
            JOIN timing_checkpoint_books b USING(slug,checkpoint)
            WHERE d.slug=? AND d.checkpoint=? AND d.time<=?""",(slug,checkpoint,now)).fetchone()
        market = db.execute("SELECT payload FROM state WHERE key='market'").fetchone()
    if not captured:
        return None,dict(reason="MISSING_CAPTURE")
    details,book = (json.loads(value) for value in captured)
    market = json.loads(market[0]) if market else {}
    current_open = (market.get("assetPriceTerms") or {}).get("priceToBeat")
    if isinstance(current_open,dict):
        current_open = current_open.get("value")
    opening = float(current_open) if market.get("slug")==slug and current_open else None
    return forecast_from_snapshot(store,slug,now,details,book,opening)


def forecast_from_snapshot(store,slug,now,details,book,opening):
    from .execution import paper_buy
    from .research import baseline_probability

    with store.connect() as db:
        outcomes = [row[0] for row in db.execute("""SELECT outcome FROM verified_markets
            WHERE slug<>? AND end<? AND verified_at<=? ORDER BY end DESC LIMIT 600""",(slug,now,now))][::-1]
    evidence = sequence_analysis(outcomes)
    if len(outcomes)<MIN_OUTCOMES:
        return None,dict(sequence=evidence,reason="WAITING_FOR_VERIFIED_HISTORY")
    if not book.get("bids") or not book.get("asks"):
        return None,dict(sequence=evidence,reason="MISSING_BOOK_DEPTH")
    ref = details.get("reference_price")
    closes = [c["close"] for c in store.candles(15,40,now=now) if c["complete"]]
    indicators = technical(closes)
    reliable = [item for item in evidence["suffixes"] if item["occurrences"]>=5]
    chosen = reliable[-1] if reliable else None
    partial = chosen["up_probability"] if chosen else evidence["prior_up"]
    bid,ask = book["bids"][0]["price"],book["asks"][0]["price"]
    odds = (bid+ask)/2
    seconds = max(0,details["window_end"]-now)
    distance = math.log(ref/opening) if opening and ref else None
    scaled = distance/max(1e-5,details["volatility"]*math.sqrt(max(1,seconds/15))) if distance is not None else None
    technical_p = .5+.35*math.tanh(scaled) if scaled is not None else .5
    p = min(.98,max(.02,.6*partial+.25*technical_p+.15*odds))
    imbalance = (details["bid_depth"]-details["ask_depth"])/max(1e-12,details["bid_depth"]+details["ask_depth"])
    returns = {}
    quotes = {}
    for side,prob in (("UP",p),("DOWN",1-p)):
        fill,why = paper_buy(book,side,1,theta=details["theta"],tick=details["tick"],quantum=details["quantum"])
        returns[side] = prob*fill["quantity"]-fill["total"] if fill else None
        quotes[side] = dict(fill=fill,reason=why)
    return p,dict(sequence=evidence,selected=chosen,indicators=indicators,market_up=odds,
                  up=p,down=1-p,opening=opening,reference=ref,distance=distance,
                  volatility_adjusted_distance=scaled,seconds_remaining=seconds,bid=bid,ask=ask,
                  spread=ask-bid,imbalance=imbalance,bid_depth=details["bid_depth"],ask_depth=details["ask_depth"],
                  quote_age=now-book["timestamp"],ev=returns,execution=quotes,
                  baselines=dict(frequency=evidence["prior_up"],market=odds,
                      momentum=baseline_probability([details["momentum_60"],details["trend_300"],
                                                     math.log(max(details["volatility"],1e-7))])) ,
                  change_signal=change_signal(outcomes))


def probability_metrics(probabilities, labels):
    if len(probabilities)!=len(labels):
        raise ValueError("Expected paired predictions and labels")
    if not labels:
        return dict(n=0,brier=None,log_loss=None)
    ps = [min(1-1e-12,max(1e-12,p)) for p in probabilities]
    return dict(n=len(labels),brier=fmean((p-y)**2 for p,y in zip(ps,labels)),
                log_loss=-fmean(y*math.log(p)+(1-y)*math.log1p(-p) for p,y in zip(ps,labels)))


def walk_forward(outcomes,warmup=20,window=600):
    """Pure outcome-sequence baseline. Each forecast uses the earlier prefix only."""
    for i in range(warmup,len(outcomes)):
        fitted = sequence_analysis(outcomes[max(0,i-window):i])
        supported = [s for s in fitted["suffixes"] if s["occurrences"]>=5]
        yield (supported[-1]["up_probability"] if supported else fitted["prior_up"],
               fitted["prior_up"],int(outcomes[i]=="UP"))


def uncertainty(successes, *, block=12,batches=500,seed=42):
    """Bounded block bootstrap and fair-coin benchmark."""
    import random

    if not successes or not 1<=batches<=2000 or not 1<=block:
        raise ValueError("Need observations, positive blocks and at most 2,000 samples")
    if len(successes)>10000:
        raise ValueError("Maximum of 10,000 observations per research batch")
    rng=random.Random(seed)
    n=len(successes)
    bootstrap,coin=[],[]
    for _ in range(batches):
        values=[]
        while len(values)<n:
            start=rng.randrange(n)
            values.extend(successes[(start+i)%n] for i in range(min(block,n-len(values))))
        bootstrap.append(fmean(values))
        coin.append(sum(rng.getrandbits(1) for _ in range(n))/n)
    bootstrap.sort()
    return dict(lower=bootstrap[int(.025*batches)],upper=bootstrap[int(.975*batches)],
                coin_tail=(1+sum(rate>=fmean(successes) for rate in coin))/(batches+1),
                block=block,repetitions=batches)


def holm(p_values):
    """Familywise-adjusted p values for a prespecified group of comparisons."""
    ordered=sorted(enumerate(p_values),key=lambda item:item[1])
    adjusted=[None]*len(ordered)
    prior=0
    for rank,(index,p) in enumerate(ordered):
        if not 0<=p<=1:
            raise ValueError("Expected probability in [0, 1]")
        prior=max(prior,min(1,p*(len(ordered)-rank)))
        adjusted[index]=prior
    return adjusted


def change_signal(outcomes, alpha=.05):
    """Compare neighboring 20-round segments with a conservative sequential bound."""
    if len(outcomes)<40:
        return None
    a,b=(fmean(x=="UP" for x in outcomes[-40:-20]),fmean(x=="UP" for x in outcomes[-20:]))
    look=len(outcomes)-39
    spending=alpha/(look*(look+1))
    bound=min(1,2*math.exp(-20*(a-b)**2))
    return dict(previous_up=a,recent_up=b,gap=b-a,look=look,p_bound=bound,
                alpha_at_look=spending,change_detected=bound<spending)

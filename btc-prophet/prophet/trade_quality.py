"""Lightweight directional challengers and a separate shadow execution gate."""

import math
from statistics import pstdev

from .execution import paper_buy

CHALLENGERS = ("ewma_gap","student_t_gap","kalman_trend","book_imbalance","nearest_neighbor")


def normal(z):
    return min(.99,max(.01,.5*(1+math.erf(z/math.sqrt(2)))))


def signals(closes, reference, opening, seconds, book, neighbors):
    """Standalone estimates. No model artifact, fitting dependency, or ledger mutation."""
    outputs = {}
    bids,asks = book.get("bids",[])[:5],book.get("asks",[])[:5]
    bid,ask = sum(x["quantity"] for x in bids),sum(x["quantity"] for x in asks)
    if bids and asks and bid+ask:
        imbalance = (bid-ask)/(bid+ask)
        midpoint = (bids[0]["price"]+asks[0]["price"])/2
        outputs["book_imbalance"] = max(.01,min(.99,.8*midpoint+.2*(.5+.5*imbalance)))
    if len(neighbors)>=20:
        outputs["nearest_neighbor"] = (sum(neighbors)+2)/(len(neighbors)+4)
    if len(closes)<20:
        return outputs
    prices = [math.log(value) for value in closes]
    returns = [b-a for a,b in zip(prices,prices[1:])]
    variance = max(1e-12,pstdev(returns[:10])**2)
    filtered = drift = 0.
    for change in returns:
        variance = .94*variance+.06*change*change
        # Scalar steady-state Kalman filter, with drift damped over the forecast horizon.
        filtered += .18*(change-filtered)
        drift += .25*(filtered-drift)
    horizon = max(1,seconds/5)
    gap = math.log(reference/opening)
    sigma = max(1e-5,math.sqrt(variance*horizon))
    outputs["ewma_gap"] = normal(gap/sigma)
    z = gap/(sigma/math.sqrt(3))
    t_cdf = .5+(math.atan(z/math.sqrt(3))+math.sqrt(3)*z/(z*z+3))/math.pi
    outputs["student_t_gap"] = max(.01,min(.99,t_cdf))
    outputs["kalman_trend"] = normal((gap+drift*min(horizon,6))/sigma)
    return outputs


def feature_distance(a,b):
    return sum((x-y)**2 for x,y in zip(a,b,strict=True))


def quality(book, terms, side, probability, confidence, features):
    """Score executable value independently of a directional forecast."""
    window = features["seconds_left"]
    if window<=0:
        return "SKIP","WINDOW_CLOSED",None,None
    if confidence is None:
        return "WAIT","INSUFFICIENT_VERIFIED_CALIBRATION",None,None
    base,reason = paper_buy(book,side,5,**terms)
    if not base:
        return "WAIT",reason,None,None
    harder,_ = paper_buy(book,side,5,**dict(terms,theta=min(1,terms["theta"]+.02),slippage_ticks=2))
    if not harder:
        return "WAIT","HIGHER_COST_DEPTH_INSUFFICIENT",base,None
    if not .30<=base["average_price"]<=.99:
        return "WAIT","PRICE_OUTSIDE_BAND",base,harder
    premium = .03 + min(.03,features["spread"]/2) + min(.02,features["disagreement"]*.04)
    # Size and costs are independently checked at each depth level. The probability limit
    # is assembled from previously verified comparable markets by the caller.
    low = min(probability,confidence)
    if any(low*fill["quantity"] < (1+premium)*fill["total"] for fill in (base,harder)):
        return "WAIT","CONSERVATIVE_EDGE_TOO_SMALL",base,harder
    return "ENTER "+side,"CONSERVATIVE_EDGE",base,harder

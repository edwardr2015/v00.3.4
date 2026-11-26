"""Forward shadow for the frozen 90-second opening-gap rule."""
import math

from .adaptive_entry import freshness
from .execution import compare_stakes
from .providers import number, timestamp, validate_market
from .research import baseline_probability, features

CHECKPOINT = 90
GRACE = 5
GAP_BPS = 12
MIN_SCORE = .65
# Laplace-smoothed rate from the earlier 325/358 archived calls.
HISTORICAL_PROBABILITY = 326 / 360
BUDGET = 3


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS selective_forecasts (
        slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
        observed REAL, status TEXT NOT NULL, reason TEXT, side TEXT, score REAL, gap_bps REAL,
        quote_source REAL, quote_confirmed REAL, fill_reason TEXT,
        quantity REAL, cost REAL, fee REAL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS selective_pending ON selective_forecasts(start)
                  WHERE status='WAITING'""")


def close_missed(db, now):
    db.execute("""UPDATE selective_forecasts SET status='SKIP',reason='MISSED_CHECKPOINT',observed=?
                   WHERE status='WAITING' AND start+?<?""", (now,CHECKPOINT+GRACE,now))


def completed_inputs(db, checkpoint):
    buckets = {}
    for t, price in db.execute("""SELECT time,close FROM candles
        WHERE source='BRTI' AND time>=? AND time<? ORDER BY time""", (checkpoint-315,checkpoint)):
        if price is None or not math.isfinite(price) or price<=0:
            continue
        bucket = buckets.setdefault(t//15*15, dict(ticks=0,close=None))
        bucket["ticks"] += 1
        bucket["close"] = price
    return buckets


def observe(db, data, now, *, model_fills=True):
    market = data.get("market") or {}
    if data.get("collection_paused") or not validate_market(market,now):
        return
    slug = market.get("slug")
    if not slug:
        return
    terms = market["assetPriceTerms"]
    start,end = (timestamp(terms[key]) for key in ("windowStart","windowEnd"))
    target = start + CHECKPOINT
    if now>target+GRACE:
        return
    db.execute("""INSERT OR IGNORE INTO selective_forecasts (slug,start,end,status)
                  VALUES (?,?,?,'WAITING')""", (slug,start,end))
    if now<target:
        return
    pending = db.execute("SELECT status FROM selective_forecasts WHERE slug=?", (slug,)).fetchone()
    if pending["status"]!="WAITING":
        return
    buckets = completed_inputs(db,int(target))
    x = features(buckets,int(target))
    if x is None:
        return
    try:
        opening = number(terms["priceToBeat"],True)
    except (KeyError,TypeError,ValueError):
        return
    gap = (buckets[int(target)-15]["close"]/opening-1)*10000
    up = baseline_probability(x)
    score = max(up,1-up)
    side = "UP" if up>.5 else "DOWN"
    reason = None
    if score<MIN_SCORE:
        reason = "LOW_MOMENTUM_SCORE"
    elif abs(gap)<GAP_BPS:
        reason = "OPENING_MOVE_TOO_SMALL"
    elif (side=="UP")!=(gap>0):
        reason = "REFERENCE_DISAGREES"
    book = data.get("book") or {}
    confirmation = (data.get("health_market") or {}).get("checked")
    fill_reason,fill = None,None
    if not reason:
        fill_reason = freshness(data,now) if model_fills else "LEARN_FORECAST_ONLY"
        if not fill_reason and (book.get("slug")!=slug or book.get("state")!="MARKET_STATE_OPEN"
                                or not book.get("bids") or not book.get("asks")):
            fill_reason = "BOOK_UNAVAILABLE"
        if not fill_reason:
            try:
                execution_terms = dict(theta=max(.095,number(market.get("feeCoefficient",.095),True)),
                                       tick=number(market["orderPriceMinTickSize"],True),
                                       quantum=number(market["minimumTradeQty"],True))
                trial, = compare_stakes(book,side,HISTORICAL_PROBABILITY,execution_terms,.03,(BUDGET,))
                fill_reason = trial["reason"]
                if fill_reason=="SHADOW_FILL":
                    fill = trial["modeled_fill"]
            except (TypeError,ValueError,KeyError):
                fill_reason = "INVALID_EXECUTION_TERMS"
    db.execute("""UPDATE selective_forecasts SET observed=?,status=?,reason=?,side=?,score=?,gap_bps=?,
        quote_source=?,quote_confirmed=?,fill_reason=?,quantity=?,cost=?,fee=? WHERE slug=? AND status='WAITING'""",
        (now,"SKIP" if reason else "CALL",reason or "QUALIFIED",None if reason else side,score,gap,
         book.get("timestamp"),confirmation,fill_reason,(fill or {}).get("quantity"),
         (fill or {}).get("total"),(fill or {}).get("fee"),slug))


def rows():
    return """SELECT p.*,v.outcome,
        CASE WHEN p.side IS NOT NULL AND v.outcome IS NOT NULL THEN p.side=v.outcome END AS correct,
        CASE WHEN p.quantity IS NOT NULL AND v.outcome IS NOT NULL THEN (p.side=v.outcome)*p.quantity-p.cost END AS pnl
        FROM selective_forecasts p LEFT JOIN verified_markets v ON v.slug=p.slug AND v.end=p.end"""


def summary(db):
    result = dict(db.execute(f"""SELECT count(*) AS observed, count(correct) AS settled_calls,
        coalesce(sum(correct),0) AS right, count(CASE WHEN status='SKIP' THEN 1 END) AS skips,
        count(pnl) AS settled_fills, sum(pnl) AS net_pnl FROM ({rows()})""").fetchone())
    last = db.execute(rows()+" ORDER BY p.start DESC LIMIT 1").fetchone()
    result["latest"] = dict(last) if last else None
    return result

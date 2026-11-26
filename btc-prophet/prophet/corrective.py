"""Isolated forward measurements for 0.3.22. No paper-ledger writes."""

import asyncio
from hashlib import sha256
import json
import logging
import math
import time

from .adaptive_entry import freshness, get_state, snapshot
from .consensus import vote
from .execution import paper_buy
from .lab import MODEL_ERRORS
from .model_paper import FORMULA_VERSIONS, ModelPaper, probabilities_from_market
from .models import BASE_PARTICIPANTS, Models, feature_sequence
from .pattern_odds import VERSION as PATTERN_VERSION, forecast_from_snapshot
from .providers import number, timestamp, validate_market
from .v2risk import CHECKPOINTS
from . import forecast_metrics, late_exit_research, trade_quality

STAKE = 5
GRACE = 8
TARGET = 200
BANDS = ((.30,.39),(.40,.49),(.50,.59),(.60,.70),(.70,.79),(.80,.89),(.90,.94),(.95,.99))
AGREEMENT = {
    "gap_agreement_40_70": (.50,.03),
    "gap_agreement_conf65": (.65,.03),
    "gap_agreement_conf75": (.75,.03),
    "gap_agreement_edge10": (.50,.10),
    "gap_agreement_calibrated": (.50,.03),
}


def interval(wins, count):
    if not count:
        return None
    z = 1.95996398454
    p = wins/count
    center = p+z*z/(2*count)
    margin = z*math.sqrt((p*(1-p)+z*z/(4*count))/count)
    scale = 1+z*z/count
    return [max(0,(center-margin)/scale),min(1,(center+margin)/scale)]


def stats(values):
    pnl = [round(v,10) for v in values]
    running = peak = drawdown = 0
    for amount in pnl:
        running += amount
        peak = max(peak,running)
        drawdown = max(drawdown,peak-running)
    wins = sum(v>0 for v in pnl)
    return dict(trades=len(pnl),wins=wins,losses=sum(v<0 for v in pnl),pnl=running,
                win_interval=interval(wins,len(pnl)),drawdown=drawdown,
                per_trade=running/len(pnl) if pnl else None)


def initialize(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS corrective_rounds (
        slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, recorded REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS corrective_observations (
        id INTEGER PRIMARY KEY, slug TEXT NOT NULL, time REAL NOT NULL, identity TEXT NOT NULL,
        evidence TEXT NOT NULL, UNIQUE(slug,identity));
    CREATE TABLE IF NOT EXISTS corrective_gap_forecasts (
        slug TEXT PRIMARY KEY, time REAL NOT NULL, up REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS corrective_challengers (
        slug TEXT NOT NULL, checkpoint INTEGER NOT NULL, model TEXT NOT NULL,
        time REAL NOT NULL, up REAL NOT NULL, context TEXT NOT NULL,
        PRIMARY KEY(slug,checkpoint,model));
    CREATE INDEX IF NOT EXISTS corrective_challenger_time ON corrective_challengers(model,time);
    CREATE TABLE IF NOT EXISTS corrective_panels (
        slug TEXT NOT NULL, checkpoint INTEGER NOT NULL, time REAL NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(slug,checkpoint));
    CREATE INDEX IF NOT EXISTS corrective_panel_time ON corrective_panels(checkpoint,time);
    CREATE TABLE IF NOT EXISTS corrective_trials (
        slug TEXT NOT NULL, policy TEXT NOT NULL, cost_case TEXT NOT NULL, time REAL NOT NULL,
        side TEXT NOT NULL, quantity REAL NOT NULL, cost REAL NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(slug,policy,cost_case));
    """)
    late_exit_research.initialize(db)


def choice(book, terms, side, probability, band, edge, stress=False):
    terms = dict(terms)
    if stress:
        terms.update(theta=min(1, terms["theta"]+.02),slippage_ticks=2)
    fill,reason = paper_buy(book,side,STAKE,**terms)
    if not fill:
        return None,reason
    if not band[0]-1e-10 <= fill["average_price"] <= band[1]+1e-10:
        return None,"PRICE_OUTSIDE_BAND"
    if probability*fill["quantity"]-fill["total"] < edge*fill["total"]:
        return None,"EDGE_TOO_SMALL"
    return fill,"ELIGIBLE"


def prior_panels(db, checkpoint, now):
    return [(json.loads(r["payload"]),r["outcome"]) for r in db.execute("""
        SELECT p.payload,v.outcome FROM corrective_panels p
        JOIN corrective_rounds r USING(slug)
        JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        WHERE checkpoint=? AND p.time<r.end AND v.verified_at<? AND v.end<=?
        ORDER BY v.end DESC LIMIT 200""",(checkpoint,now,now))]

def calibrate_gap(db, slug, p, midpoint, now):
    confidence = max(p,1-p)
    bucket = min(9,int(confidence*10))
    earlier = db.execute("""SELECT f.up,v.outcome FROM corrective_gap_forecasts f
        JOIN corrective_rounds r USING(slug)
        JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        WHERE f.slug<>? AND f.time<r.end AND v.verified_at<? AND v.end<=?
        AND min(9,cast(max(f.up,1-f.up)*10 AS integer))=?
        ORDER BY v.end DESC LIMIT 150""",(slug,now,now,bucket)).fetchall()
    prior = (p+midpoint)/2
    right = sum((r["up"]>.5)==(r["outcome"]=="UP") for r in earlier)
    same_side = p>.5
    adjusted = (right+30*(prior if same_side else 1-prior))/(30+len(earlier))
    return (adjusted if same_side else 1-adjusted),len(earlier)



def agreement_side(panel, rule):
    up,down = panel["up_votes"],panel["down_votes"]
    if panel["missing"]:
        return None
    minimum = int(rule[-1])
    return "UP" if up>=minimum else "DOWN" if down>=minimum else None


def scores(panel, prior):
    weighted = {}
    for family,item in panel["components"].items():
        matched = [(p["components"][family]["up"],result=="UP") for p,result in prior
                   if family in p["components"] and p["components"][family]["version"]==item["version"]]
        if len(matched)>=20:
            weighted[family] = 1/max(.05,sum((x-y)**2 for x,y in matched)/len(matched))
    if len(weighted)!=8 or panel["missing"]:
        return None
    return sum(panel["components"][family]["up"]*weight for family,weight in weighted.items())/sum(weighted.values())


def summary(db, now):
    tests = list(db.execute("""SELECT r.slug,v.outcome FROM corrective_rounds r
        JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        WHERE v.verified_at<=? ORDER BY v.end,r.slug""",(now,)))
    outcomes = {r["slug"]:r["outcome"] for r in tests}
    grouped = {}
    for r in db.execute("""SELECT t.* FROM corrective_trials t JOIN corrective_rounds r USING(slug)
        ORDER BY r.end,t.policy,t.cost_case"""):
        outcome = outcomes.get(r["slug"])
        if outcome:
            grouped.setdefault((r["policy"],r["cost_case"]),[]).append(
                (r["side"]==outcome)*r["quantity"]-r["cost"])
    results = [dict(policy=k[0],cost_case=k[1],**stats(v)) for k,v in sorted(grouped.items())]
    comparable = [r for r in db.execute("""SELECT r.slug,t.status,t.pnl
        FROM corrective_rounds r LEFT JOIN unified_trades t USING(slug)
        WHERE t.time>=r.start OR t.time IS NULL ORDER BY r.end,r.slug""") if r["slug"] in outcomes]
    primary = stats([r["pnl"] for r in comparable if r["pnl"] is not None])
    primary["markets"] = len(tests)
    primary["skips"] = sum(r["status"] is None for r in comparable)
    policies = [{"policy":p,"cost_case":cost,**stats(grouped.get((p,cost),[]))}
                for p in AGREEMENT for cost in ("standard","higher")]
    for row in results:
        if row["policy"] not in AGREEMENT:
            policies.append(row)
    for row in policies:
        row["skips"] = len(tests)-row["trades"]
    panel = db.execute("SELECT * FROM corrective_panels ORDER BY time DESC LIMIT 1").fetchone()
    scorecards, panels90 = {}, []
    for row in db.execute("""SELECT p.checkpoint,p.payload,v.outcome FROM corrective_panels p
        JOIN corrective_rounds r USING(slug)
        JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        WHERE p.time<r.end AND v.verified_at<=?""",(now,)):
        payload = json.loads(row["payload"])
        target = row["outcome"]=="UP"
        if row["checkpoint"]==90:
            panels90.append((payload["components"],target))
        for name,item in payload["components"].items():
            scorecards.setdefault((row["checkpoint"],name),[]).append((item["up"],target))
    scores = [dict(checkpoint=checkpoint,model=model,**forecast_metrics.scores(pairs))
              for (checkpoint,model),pairs in sorted(scorecards.items())]
    latest = dict(panel) if panel else None
    if latest:
        latest["payload"] = json.loads(latest["payload"])
    challengers = {}
    for r in db.execute("""SELECT f.* FROM corrective_challengers f JOIN corrective_rounds r USING(slug)
        WHERE f.checkpoint=90 AND f.time<r.end"""):
        if r["slug"] in outcomes:
            challengers.setdefault(r["model"],[]).append((r["up"],outcomes[r["slug"]]=="UP"))
    shadow_scores = [dict(model=model,**forecast_metrics.scores(rows))
                     for model,rows in sorted(challengers.items())]
    calibration = [row for model,pairs in sorted(challengers.items())
                   for row in forecast_metrics.reliability(model,pairs)]
    for (checkpoint,model),pairs in sorted(scorecards.items()):
        if checkpoint==90:
            calibration.extend(forecast_metrics.reliability(model,pairs))
    pairs = forecast_metrics.panel_dependence(panels90)
    return dict(verified_markets=len(tests),target=TARGET,primary=primary,policies=policies,latest_panel=latest,
                models=scores,challengers=shadow_scores,calibration=calibration,dependence=pairs)



class Research:
    def __init__(self, store, paper):
        self.store = store
        self.paper = paper
        self.models = Models(store)
        self.task = None
        self.last_error = None
        with store.connect() as db:
            initialize(db)

    async def loop(self):
        while True:
            try:
                # A research failure never changes the primary account or its mode.
                self.tick()
                self.last_error = None
            except Exception as error:
                name = f"{type(error).__name__}: {error}"
                if self.last_error!=name:
                    logging.getLogger(__name__).exception("Corrective shadow evaluation failed")
                    self.last_error = name
            await asyncio.sleep(2)

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)

    def panel(self, versions, slug, checkpoint, now, features, book, terms, opening, data, end, block):
        components,missing = {},{}
        x = None
        if all(k in features for k in ("momentum_60","trend_300","volatility")):
            x = [features["momentum_60"],features["trend_300"],math.log(max(features["volatility"],1e-7))]
        catalog = None
        for family in BASE_PARTICIPANTS:
            version = versions.get(family)
            if not version:
                if catalog is None:
                    catalog = {m["id"]:m["status"] for m in self.models.catalog()}
                missing[family] = catalog.get(family,"NO_CAUSAL_MODEL_VERSION")
                continue
            if x is None:
                missing[family] = features.get("reason") or block or "FEATURES_UNAVAILABLE"
                continue
            try:
                if version==PATTERN_VERSION:
                    if block:
                        missing[family] = block
                        continue
                    context = dict(features,**terms,window_end=end,reference_price=data["reference"]["price"],
                        bid_depth=sum(x["quantity"] for x in book["bids"]),ask_depth=sum(x["quantity"] for x in book["asks"]))
                    p,evidence = forecast_from_snapshot(self.store,slug,now,context,book,opening)
                    if p is None:
                        missing[family] = evidence.get("reason","PATTERN_INPUTS_UNAVAILABLE")
                        continue
                else:
                    seq = feature_sequence(self.store,x,checkpoint,slug,now)
                    p = self.models.predict(version,x,seq)
                if p is None or not math.isfinite(p) or not 0<=p<=1:
                    missing[family] = "MODEL_RETURNED_NO_VALID_PROBABILITY"
                    continue
                components[family] = dict(up=p,version=version)
            except (*MODEL_ERRORS,KeyError) as exc:
                missing[family] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return dict(vote(components,BASE_PARTICIPANTS),missing_reasons=missing)

    def tick(self, now=None):
        live = now is None
        now = time.time() if live else now
        with self.store.connect() as db:
            if get_state(db,"mode") not in ("LEARN","SIMULATE") or get_state(db,"emergency"):
                return
            data = snapshot(db)
            market = data.get("market") or {}
            if not validate_market(market,now):
                return
            slug = market["slug"]
            start,end = (timestamp(market["assetPriceTerms"][k]) for k in ("windowStart","windowEnd"))
            captured = {r[0] for r in db.execute("SELECT checkpoint FROM corrective_panels WHERE slug=?",(slug,))}
            due = [c for c in CHECKPOINTS if c not in captured and now>=start+c]
            versions = ModelPaper.versions(db,now) if due else {}
        block = freshness(data,now)
        book = data.get("book") or {}
        if not block and (book.get("slug")!=slug or book.get("state")!="MARKET_STATE_OPEN"
                          or not book.get("bids") or not book.get("asks")):
            block = "BOOK_UNAVAILABLE"
        terms = {}
        opening = None
        try:
            opening = number(market["assetPriceTerms"]["priceToBeat"],True)
            terms = dict(theta=max(.095,number(market.get("feeCoefficient",.095),True)),
                         tick=number(market["orderPriceMinTickSize"],True),quantum=number(market["minimumTradeQty"],True))
            if not (terms["theta"]<=1 and terms["tick"]<1 and terms["quantum"]<=100):
                raise ValueError("Unsupported terms")
        except (KeyError,ValueError,TypeError):
            block = block or "INVALID_EXECUTION_TERMS"
        ref = data.get("reference") or {}
        raw, features = self.paper.probability(now,book=book if not block else {})
        panels = {}
        for checkpoint in due:
            if now>start+checkpoint+GRACE:
                panels[checkpoint] = dict(vote({},BASE_PARTICIPANTS),missing_reasons={
                    k:block or "MISSED_CHECKPOINT" for k in BASE_PARTICIPANTS})
            elif not block:
                panels[checkpoint] = self.panel(versions,slug,checkpoint,now,features,book,terms,opening,data,end,block)
        observed = time.time() if live else now
        if not validate_market(market,observed):
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (get_state(db,"mode") not in ("SIMULATE","LEARN") or get_state(db,"emergency")
                    or snapshot(db)!=data):
                return
            db.execute("INSERT OR IGNORE INTO corrective_rounds VALUES (?,?,?,?)",(slug,start,end,observed))
            block = freshness(data,observed) or block
            info = dict(source=book.get("timestamp"),received=(data.get("health_market") or {}).get("checked"),
                        reference_source=ref.get("timestamp"),decision=observed,seconds_left=end-observed,
                        reference_price=ref.get("price"),opening=opening,volatility=features.get("volatility"),
                        source_lag=(data["health_market"]["checked"]-book["timestamp"]) if not block else None,
                        bid_depth=sum(r["quantity"] for r in book.get("bids",[])),
                        ask_depth=sum(r["quantity"] for r in book.get("asks",[])),
                        reason=block,terms=terms)
            for checkpoint,panel in panels.items():
                if panel["components"] and (block or observed>start+checkpoint+GRACE):
                    continue
                prior = prior_panels(db,checkpoint,observed)
                panel["weighted_up"] = scores(panel,prior)
                panel["timing"] = info
                inserted = db.execute("INSERT OR IGNORE INTO corrective_panels VALUES (?,?,?,?)",
                            (slug,checkpoint,observed,json.dumps(panel))).rowcount
                if inserted and not block and observed<=start+checkpoint+GRACE:
                    self.panel_trials(db,slug,checkpoint,panel,prior,book,terms,info)
            if not block and raw is not None and features.get("volatility") is not None:
                for checkpoint in panels:
                    if observed<=start+checkpoint+GRACE:
                        self.gap_fixed_trial(db,slug,checkpoint,raw,book,terms,info)
                        if checkpoint==90:
                            self.challenger_trials(db,slug,book,terms,info,panels[checkpoint])
            if not block and raw is not None and features.get("volatility") is not None:
                self.adaptive_trials(db,slug,raw,features,book,terms,info)
            else:
                info["reason"] = block or features.get("reason") or "FORECAST_INPUTS_UNAVAILABLE"
            late_exit_research.observe(db,slug,start,end,observed,book,terms,block,info)
            identity = sha256(json.dumps([info["reason"],book,ref,raw,features,info.get("rejections")],
                                  sort_keys=True).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO corrective_observations(slug,time,identity,evidence) VALUES (?,?,?,?)",
                       (slug,observed,identity,json.dumps(info)))

    def record(self, db, slug, policy, side, p, book, terms, info, band, edge):
        reasons = {}
        for case in ("standard","higher"):
            fill,why = choice(book,terms,side,p,band,edge,stress=case=="higher")
            reasons[case] = why
            if fill:
                db.execute("INSERT OR IGNORE INTO corrective_trials VALUES (?,?,?,?,?,?,?,?)",
                           (slug,policy,case,info["decision"],side,fill["quantity"],fill["total"],
                            json.dumps(dict(info,fill=fill,probability=p))))
        return reasons

    def challenger_trials(self, db, slug, book, terms, info, panel):
        now = info["decision"]
        midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
        gap = math.log(info["reference_price"]/info["opening"])
        spread = book["asks"][0]["price"]-book["bids"][0]["price"]
        vol = info["volatility"]
        vector = [1000*gap,1000*vol,midpoint,spread,info["seconds_left"]/300]
        earlier = list(db.execute("""SELECT f.up,f.context,v.outcome FROM corrective_challengers f
            JOIN corrective_rounds r USING(slug)
            JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
            WHERE f.model='reference_gap' AND f.checkpoint=90 AND f.slug<>?
              AND f.time<r.end AND v.end<=? AND v.verified_at<?
            ORDER BY v.end DESC LIMIT 250""",(slug,now,now)))
        neighbors = sorted(earlier,key=lambda r: trade_quality.feature_distance(vector,json.loads(r["context"])["vector"]))[:30]
        anchor = int(now)//5*5
        closes = [r[0] for r in db.execute("""SELECT c.close FROM candles c
            JOIN (SELECT max(time) AS last FROM candles WHERE source='BRTI'
                  AND time>=? AND time<? GROUP BY time/5 HAVING count(*)>=4) x ON x.last=c.time
            WHERE c.source='BRTI' ORDER BY c.time""",(anchor-300,anchor))]
        probabilities = trade_quality.signals(closes,info["reference_price"],info["opening"],
            info["seconds_left"],book,[r["outcome"]=="UP" for r in neighbors])
        raw = probabilities_from_market(midpoint,info["reference_price"],info["opening"],vol,
            info["seconds_left"],midpoint)["volatility_reference_gap"]
        p,n = calibrate_gap(db,slug,raw,midpoint,now)
        probabilities["reference_gap"] = p
        components = list(panel["components"].values())
        disagreement = (max(x["up"] for x in components)-min(x["up"] for x in components)) if components else .5
        side = "UP" if p>.5 else "DOWN"
        # Use comparable directions and price regions, with one verified score per market.
        similar = [r for r in earlier if (r["up"]>.5)==(p>.5) and abs(r["up"]-p)<=.20
                   and abs(json.loads(r["context"])["midpoint"]-midpoint)<=.15]
        ci = interval(sum((r["up"]>.5)==(r["outcome"]=="UP") for r in similar),len(similar))
        context = dict(vector=vector,midpoint=midpoint,gap=gap,volatility=vol,
            seconds_left=info["seconds_left"],spread=spread,disagreement=disagreement,
            volatility_regime="higher" if vol>=.001 else "lower",calibration_markets=n)
        action,reason,fill,stressed = trade_quality.quality(book,terms,side,max(p,1-p),
            ci[0] if len(similar)>=20 else None,context)
        if action.startswith("ENTER"):
            for case,model_fill in (("standard",fill),("higher",stressed)):
                db.execute("INSERT OR IGNORE INTO corrective_trials VALUES (?,?,?,?,?,?,?,?)",
                    (slug,"trade_quality_90s",case,now,side,model_fill["quantity"],model_fill["total"],
                     json.dumps(dict(context,fill=model_fill,probability=p,interval=ci))))
        context.update(quality_action=action,quality_reason=reason,quality_interval=ci,
                       quality_sample=len(similar),budget_depth={"bid":info["bid_depth"],"ask":info["ask_depth"]})
        for name,prob in probabilities.items():
            db.execute("INSERT OR IGNORE INTO corrective_challengers VALUES (?,?,?,?,?,?)",
                       (slug,90,name,now,prob,json.dumps(context)))
            if name!="reference_gap":
                predicted_side = "UP" if prob>.5 else "DOWN"
                self.record(db,slug,name+"_90s",predicted_side,max(prob,1-prob),book,terms,info,(.30,.99),.03)

    def gap_fixed_trial(self, db, slug, checkpoint, raw, book, terms, info):
        midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
        p = probabilities_from_market(midpoint,info["reference_price"],info["opening"],
              info["volatility"],info["seconds_left"],raw)["volatility_reference_gap"]
        if p!=.5 and midpoint!=.5 and (p>.5)==(midpoint>.5):
            self.record(db,slug,f"{checkpoint}s_gap_agreement_40_70",
                        "UP" if p>.5 else "DOWN",max(p,1-p),book,terms,info,(.40,.70),.03)

    def adaptive_trials(self, db, slug, raw, features, book, terms, info):
        midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
        p = probabilities_from_market(midpoint,info["reference_price"],info["opening"],
              features["volatility"],info["seconds_left"],raw)["volatility_reference_gap"]
        gap_side = "UP" if p>.5 else "DOWN"
        info.update(midpoint=midpoint, gap_probability=p,
                    spread=book["asks"][0]["price"]-book["bids"][0]["price"],
                    top_bid=book["bids"][0],top_ask=book["asks"][0],rejections={})
        adjusted,n = calibrate_gap(db,slug,p,midpoint,info["decision"])
        db.execute("INSERT OR IGNORE INTO corrective_gap_forecasts VALUES (?,?,?)",(slug,info["decision"],p))
        info.update(gap_calibrated_up=adjusted,gap_calibration_markets=n)
        # Preserve the original rule as one comparison and score calibration separately.
        for name,(confidence,edge) in AGREEMENT.items():
            if p==.5 or midpoint==.5 or (p>.5)!=(midpoint>.5):
                info["rejections"][name] = "MARKET_DISAGREEMENT"
            elif max(p,1-p)<confidence:
                info["rejections"][name] = "BELOW_CONFIDENCE"
            else:
                q = adjusted if name.endswith("calibrated") else p
                info["rejections"][name] = self.record(db,slug,name,gap_side,q if gap_side=="UP" else 1-q,
                                                     book,terms,info,(.40,.70),edge)
        for family,prob in (("gap",p),("momentum",raw)):
            side = "UP" if prob>.5 else "DOWN"
            for band in BANDS:
                label = f"{family}_{band[0]:.2f}_{band[1]:.2f}"
                info["rejections"][label] = self.record(db,slug,label,side,max(prob,1-prob),
                                                      book,terms,info,band,0)
            label = f"{family}_eligible_control"
            info["rejections"][label] = self.record(db,slug,label,side,max(prob,1-prob),
                                                   book,terms,info,(.02,.99),-1)

    def panel_trials(self, db, slug, checkpoint, panel, prior, book, terms, info):
        reasons = {}
        for rule in ("vote6","vote7","vote8","weighted65","weighted75","weighted90","highconfidence90"):
            threshold = int(rule[-2:])/100 if not rule.startswith("vote") else None
            weighted = panel["weighted_up"]
            if threshold:
                side = ("UP" if weighted>=threshold else "DOWN" if weighted<=1-threshold else None) if weighted is not None else None
                past = []
                for item,result in prior:
                    q = item.get("weighted_up")
                    if q is not None and max(q,1-q)>=threshold:
                        past.append((q>.5)==(result=="UP"))
            else:
                side = agreement_side(panel,rule)
                past = [s==result for item,result in prior if (s:=agreement_side(item,rule))]
            ci = interval(sum(past),len(past))
            entry = dict(side=side,observations=len(past),interval=ci)
            if not side:
                entry["reason"] = "MISSING_OR_BELOW_AGREEMENT"
            elif len(past)<20:
                entry["reason"] = "INSUFFICIENT_VERIFIED_CALIBRATION"
            elif rule=="highconfidence90" and ci[0]<.90:
                entry["reason"] = "LOWER_CONFIDENCE_BOUND_BELOW_90"
            else:
                entry["fills"] = self.record(db,slug,f"{checkpoint}s_{rule}",side,ci[0],book,terms,
                                             info,(.30,.99),.03)
            reasons[rule] = entry
        db.execute("UPDATE corrective_panels SET payload=? WHERE slug=? AND checkpoint=?",
                   (json.dumps(dict(panel,rules=reasons)),slug,checkpoint))

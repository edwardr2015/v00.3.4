"""Forward-only paper comparisons and pre-decision market change observations."""
import json

from .execution import paper_buy

CAP = .85
STAKE = 3
CHECKPOINTS = (45,60,90)


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS agreement_cap_trials(
        slug TEXT, checkpoint INTEGER, policy TEXT, time REAL NOT NULL, side TEXT, up REAL,
        gap_bps REAL, quantity REAL, cost REAL, fee REAL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(slug,checkpoint,policy))""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_market_samples(
        slug TEXT, observation TEXT, time REAL NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(slug,observation))""")
    db.execute("CREATE INDEX IF NOT EXISTS model_market_samples_time ON model_market_samples(slug,time DESC)")


def agreement_entry(db, slug, checkpoint, observed, side, up, gap, reason, book, details):
    if checkpoint not in CHECKPOINTS:
        return
    fill = None
    if not reason and (not book or not book.get("bids") or not book.get("asks")):
        reason = "NO_TWO_SIDED_DEPTH"
    if not reason:
        terms = {k:details[k] for k in ("theta","tick","quantum")}
        fill, reason = paper_buy(book,side,STAKE,**terms)
    all_in = fill["total"]/fill["quantity"] if fill else None
    audit = json.dumps(dict(all_in=all_in,book_source=details.get("book_timestamp"),
                           book_confirmed=details.get("book_checked"),reference_time=details.get("reference_timestamp"),
                           volatility=details.get("volatility"),spread=details.get("spread"),fill=fill))
    for policy in ("UNCAPPED","CAP_85"):
        why = reason or ("ABOVE_85_CENT_CAP" if policy=="CAP_85" and all_in>CAP+1e-10 else None)
        accepted = fill if not why else None
        db.execute("""INSERT OR IGNORE INTO agreement_cap_trials VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (slug,checkpoint,policy,observed,side,up,gap,accepted["quantity"] if accepted else None,
                    accepted["total"] if accepted else None,accepted["fee"] if accepted else None,
                    why or "SHADOW_HOLD_ENTRY",audit))


def market_context(book, ref, opening, now, features, vote):
    bids, asks = book["bids"],book["asks"]
    return dict(spread=asks[0]["price"]-bids[0]["price"],mid=(bids[0]["price"]+asks[0]["price"])/2,
                bid_depth=sum(level["quantity"] for level in bids),ask_depth=sum(level["quantity"] for level in asks),
                volatility=features.get("volatility"),momentum=features.get("momentum_60"),
                gap_bps=(ref["price"]/opening-1)*10000,book_age=now-book["timestamp"],
                reference_age=now-ref["timestamp"],up_votes=vote.get("up_votes"),
                down_votes=vote.get("down_votes"),missing_votes=len(vote.get("missing",[])) if vote else None)


def measure_change(current, prior, tick):
    observed = dict(current, spread_change=None, depth_ratio=None, volatility_ratio=None,
                    midpoint_change=None, gap_change_bps=None, vote_margin_change=None, alerts=[])
    if not prior:
        return observed
    observed["spread_change"] = current["spread"]-prior["spread"]
    observed["midpoint_change"] = current["mid"]-prior["mid"]
    observed["gap_change_bps"] = current["gap_bps"]-prior["gap_bps"]
    if current.get("missing_votes")==prior.get("missing_votes")==0:
        observed["vote_margin_change"] = ((current["up_votes"]-current["down_votes"])
                                         -(prior["up_votes"]-prior["down_votes"]))
    prior_depth=min(prior["bid_depth"],prior["ask_depth"])
    if prior_depth>0:
        observed["depth_ratio"] = min(current["bid_depth"],current["ask_depth"])/prior_depth
    if current.get("volatility") is not None and prior.get("volatility"):
        observed["volatility_ratio"] = current["volatility"]/prior["volatility"]
    if current["spread"]>=.03-1e-10 and observed["spread_change"]>=tick-1e-10:
        observed["alerts"].append("SPREAD_WIDENING")
    if observed["depth_ratio"] is not None and observed["depth_ratio"]<=.5:
        observed["alerts"].append("DEPTH_DROP")
    if observed["volatility_ratio"] is not None and observed["volatility_ratio"]>=1.5:
        observed["alerts"].append("VOLATILITY_JUMP")
    return observed


def observe(db, slug, observation, now, *, book, ref, opening, features, votes, tick):
    existing=db.execute("SELECT evidence FROM model_market_samples WHERE slug=? AND observation=?",
                        (slug,observation)).fetchone()
    if existing:
        return json.loads(existing[0])
    # Same-round observations only, strictly earlier than the decision.
    previous=db.execute("SELECT time,evidence FROM model_market_samples WHERE slug=? AND time<? ORDER BY time DESC LIMIT 1",
                        (slug,now)).fetchone()
    prior=json.loads(previous["evidence"]) if previous and now-previous["time"]<=30 else None
    current=market_context(book,ref,opening,now,features,votes)
    evidence=measure_change(current,prior,tick)
    evidence["comparison_seconds"]=now-previous["time"] if prior else None
    db.execute("INSERT OR IGNORE INTO model_market_samples VALUES (?,?,?,?)",
               (slug,observation,now,json.dumps(evidence)))
    return evidence


def cap_results(db, now):
    return db.execute("""SELECT t.*,v.outcome,
        CASE WHEN v.outcome IS NOT NULL AND t.quantity IS NOT NULL
            THEN t.quantity*(t.side=v.outcome)-t.cost END AS pnl
        FROM agreement_cap_trials t LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end<=? AND v.verified_at<=?
        ORDER BY t.time,t.checkpoint,t.policy""", (now,now))


def conditions(db):
    return db.execute("""WITH audited AS (
        SELECT p.family,p.version,p.pnl,p.entered-w.start AS entry_seconds,
          json_extract(d.evidence,'$.market_conditions') AS context
        FROM model_paper_positions p JOIN model_paper_windows w USING(slug)
        JOIN model_paper_decisions d ON d.family=p.family AND d.slug=p.slug AND d.time=p.entered
                                  AND d.action IN ('UP','DOWN') WHERE p.pnl IS NOT NULL
        ), bucketed AS (
          SELECT *,CAST(entry_seconds/15 AS INTEGER)*15 AS timing,
            CASE WHEN json_extract(context,'$.comparison_seconds') IS NULL THEN 'NO_RECENT_REFERENCE'
                 WHEN json_array_length(context,'$.alerts')>0 THEN 'CHANGE_ALERT'
                 ELSE 'NO_CHANGE_ALERT' END AS condition
          FROM audited
        )
        SELECT family,version,timing,condition,count(*) AS trades,sum(pnl>0) AS wins,
            sum(pnl<0) AS losses,sum(pnl) AS pnl,
            CASE WHEN condition='CHANGE_ALERT' THEN 0.0 ELSE sum(pnl) END AS guarded_pnl
        FROM bucketed GROUP BY family,version,timing,condition ORDER BY family,version,timing,condition""")


def feedback(db):
    """Attribute observable decision errors on matched, officially resolved entries."""
    rows=db.execute("""SELECT p.*,e.up,e.reason,e.evidence,x.reason AS exit_reason
        FROM model_paper_positions p JOIN model_paper_decisions e
            ON e.family=p.family AND e.slug=p.slug AND e.time=p.entered AND e.action=p.side
        LEFT JOIN model_paper_decisions x
            ON x.family=p.family AND x.slug=p.slug AND x.time=p.closed AND x.action='CASH_OUT'
        WHERE p.outcome IS NOT NULL AND p.pnl IS NOT NULL ORDER BY p.closed,p.family""")
    for r in rows:
        evidence=json.loads(r["evidence"])
        correct=r["side"]==r["outcome"]
        hold=r["quantity"]*correct-r["cost"]
        delta=r["pnl"]-hold
        b=evidence.get("bid",{}).get("price")
        a=evidence.get("ask",{}).get("price")
        mid=(b+a)/2 if b is not None and a is not None else None
        actual=int(r["outcome"]=="UP")
        entry_brier=(r["up"]-actual)**2 if r["up"] is not None else None
        market_brier=(mid-actual)**2 if mid is not None else None
        calibration=evidence.get("calibration_shadow") or {}
        adjusted=calibration.get("up")
        adjusted_brier=(adjusted-actual)**2 if adjusted is not None else None
        entry_note = ("DIRECTION_MISSED" if not correct else
                      "ENTRY_COST_EXCEEDED_PAYOUT" if hold<=0 else "DIRECTION_AND_PRICE_POSITIVE")
        exit_note = ("HELD" if r["status"]!="CASHED_OUT" else
                     "CASH_OUT_BEAT_HOLD" if delta>1e-9 else
                     "CASH_OUT_TRAILED_HOLD" if delta< -1e-9 else "CASH_OUT_MATCHED_HOLD")
        yield dict(family=r["family"],version=r["version"],market=r["slug"],entered=r["entered"],closed=r["closed"],
                   side=r["side"],outcome=r["outcome"],correct=correct,up=r["up"],market_up=mid,
                   entry_brier=entry_brier,market_brier=market_brier,prior_entries=calibration.get("prior_entries"),
                   adjusted_up=adjusted,adjusted_brier=adjusted_brier,pnl=r["pnl"],hold=hold,delta=delta,
                   entry_note=entry_note,exit_note=exit_note,entry_reason=r["reason"],exit_reason=r["exit_reason"])


def calibrate_entry(db, family, version, slug, up, now):
    """Shadow-only residual adjustment using earlier matched entries in the same probability band."""
    # Five bands and a fixed shrinkage rule are frozen for forward evaluation.
    band = min(4,int(up*5))
    observations = db.execute("""SELECT e.up,v.outcome FROM model_paper_positions p
         JOIN model_paper_decisions e ON e.family=p.family AND e.slug=p.slug
                AND e.time=p.entered AND e.action=p.side
         JOIN verified_markets v ON v.slug=p.slug
         WHERE p.family=? AND p.version=? AND p.slug!=? AND p.entered<v.end
            AND v.end<=? AND v.verified_at<=? AND e.up IS NOT NULL
            AND min(4,CAST(e.up*5 AS INTEGER))=?
         ORDER BY v.end DESC,v.slug LIMIT 100""",(family,version,slug,now,now,band)).fetchall()
    count=len(observations)
    adjusted=None
    if count>=20:
        residual=sum(int(r["outcome"]=="UP")-r["up"] for r in observations)
        adjusted=min(.98,max(.02,up+residual/(count+20)))
    return dict(method="ENTRY_RESIDUAL_SHADOW_V1",prior_entries=count,up=adjusted)

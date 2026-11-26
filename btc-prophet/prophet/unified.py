"""The 0.3.21 paper account and its matched, unfunded comparisons."""

import json
import math

from .analytics import streaks
from .execution import execute, paper_buy

STARTING_CASH = 1000
BASE_STAKE = 5
LARGE_STAKE = 100
MIN_EDGE = .10
EXIT_MARGIN = .03
STRESS_EDGE = .03
LARGE_MIN_ROUNDS = 100
LARGE_MAX_DRAWDOWN = 250


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS unified_account (
            id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL,
            balance REAL NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS unified_rounds (
            slug TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
            observed REAL NOT NULL, reason TEXT NOT NULL, model TEXT, up REAL,
            details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS unified_forecasts (
            slug TEXT NOT NULL, model TEXT NOT NULL, time REAL NOT NULL,
            raw_up REAL NOT NULL, adjusted_up REAL NOT NULL,
            PRIMARY KEY(slug,model));
        CREATE TABLE IF NOT EXISTS unified_trades (
            slug TEXT PRIMARY KEY REFERENCES unified_rounds(slug), time REAL NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('UP','DOWN')), model TEXT NOT NULL, up REAL NOT NULL,
            stake INTEGER NOT NULL CHECK(stake IN (5,100)), quantity REAL NOT NULL CHECK(quantity>0),
            cost REAL NOT NULL CHECK(cost>0 AND cost<=stake), fee REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','SOLD','SETTLED')),
            closed REAL, proceeds REAL, pnl REAL, details TEXT NOT NULL, exit_details TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS unified_one_open ON unified_trades(status) WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS unified_large_trials (
            slug TEXT PRIMARY KEY REFERENCES unified_rounds(slug), time REAL NOT NULL,
            side TEXT NOT NULL, quantity REAL NOT NULL, cost REAL NOT NULL,
            stress_quantity REAL NOT NULL, stress_cost REAL NOT NULL, details TEXT NOT NULL);
    """)


def calibrate(db, slug, model, raw_up, midpoint, now):
    """Fit an auditable confidence bucket with at most one causal label per market."""
    side = raw_up >= .5
    confidence = max(raw_up, 1 - raw_up)
    low = min(.9, math.floor(confidence * 10) / 10)
    hi = 1.00000001 if low == .9 else low + .1
    rows = db.execute("""SELECT f.raw_up, v.outcome FROM unified_forecasts f
        JOIN unified_rounds r ON r.slug=f.slug
        JOIN verified_markets v ON v.slug=f.slug AND v.end=r.end
        WHERE f.model=? AND f.slug<>? AND f.time<r.end AND v.end<=?
        AND v.verified_at<? AND max(f.raw_up,1-f.raw_up)>=?
        AND max(f.raw_up,1-f.raw_up)<?
        ORDER BY v.end DESC LIMIT 150""", (model, slug, now, now, low, hi)).fetchall()
    # Until similar settled forecasts accumulate, shrink raw confidence toward market odds.
    prior = min(.99, max(.01, .5 * raw_up + .5 * midpoint))
    matches = sum((row["raw_up"] >= .5) == (row["outcome"] == "UP") for row in rows)
    weight = len(rows)
    adjusted_side = (matches + 30 * (prior if side else 1-prior)) / (weight + 30)
    return adjusted_side if side else 1-adjusted_side, weight


def candidate(book, side, stake, probability, terms, low, high, edge, *, stressed=False):
    costs = dict(terms)
    if stressed:
        costs.update(theta=min(1, costs["theta"]+.02), slippage_ticks=2)
    fill, reason = paper_buy(book, side, stake, **costs)
    if not fill:
        return None, reason
    if not low-1e-10 <= fill["average_price"] <= high+1e-10 or fill["total"]/fill["quantity"] > .85:
        return None, "PRICE_OUTSIDE_BAND"
    result = dict(fill=fill, ev=probability*fill["quantity"]-fill["total"])
    if result["ev"] < edge * fill["total"]:
        return None, "EDGE_TOO_SMALL"
    return result, "ELIGIBLE"


def matched_large(db, now):
    results = db.execute("""SELECT (p.side=v.outcome)*p.quantity-p.cost AS pnl,
        (p.side=v.outcome)*p.stress_quantity-p.stress_cost AS stress_pnl
        FROM unified_large_trials p JOIN unified_rounds r ON r.slug=p.slug
        JOIN verified_markets v ON v.slug=p.slug AND v.end=r.end
        WHERE v.end<=? AND v.verified_at<?
        ORDER BY v.end DESC,p.slug DESC LIMIT 200""", (now,now)).fetchall()
    total = stressed = peak = drawdown = 0.0
    for row in reversed(results):
        total += row["pnl"]
        stressed += row["stress_pnl"]
        peak = max(peak, stressed)
        drawdown = max(drawdown, peak-stressed)
    wins = sum(r["stress_pnl"] > 0 for r in results)
    count = len(results)
    return dict(rounds=count, wins=wins, net=total, stress_net=stressed, drawdown=drawdown,
                qualified=count>=LARGE_MIN_ROUNDS and wins/count>=.9
                and total>0 and stressed>0 and drawdown<=LARGE_MAX_DRAWDOWN)


def close(db, trade, now, proceeds, status, audit):
    pnl = round(proceeds-trade["cost"], 10)
    result = db.execute("""UPDATE unified_trades SET status=?,closed=?,proceeds=?,pnl=?,exit_details=?
        WHERE slug=? AND status='OPEN'""",
        (status, now, proceeds, pnl, json.dumps(audit), trade["slug"]))
    if result.rowcount == 1:
        db.execute("UPDATE unified_account SET balance=round(balance+?,10) WHERE id=1", (proceeds,))


def settle(db, now):
    for row in db.execute("""SELECT t.*,v.outcome,v.verified_at FROM unified_trades t
        JOIN unified_rounds r ON r.slug=t.slug
        JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end
        WHERE t.status='OPEN' AND v.end<=? AND v.verified_at<=?""", (now,now)).fetchall():
        payout = row["quantity"] if row["side"]==row["outcome"] else 0
        close(db,row,now,payout,"SETTLED",dict(outcome=row["outcome"],verified_at=row["verified_at"]))


def premium_trial(db, slug, start, now, book, terms, audit, commit_time):
    # A shadow quote can qualify even if the primary account already entered for $5.
    if not start+90 <= now <= start+95:
        return None
    call = db.execute("""SELECT side FROM selective_forecasts
        WHERE slug=? AND status='CALL' AND observed BETWEEN ? AND ?""", (slug,start+90,now)).fetchone()
    if not call:
        return None
    from .selective_forecast import HISTORICAL_PROBABILITY

    side, probability = call["side"], HISTORICAL_PROBABILITY
    large, _ = candidate(book,side,LARGE_STAKE,probability,terms,.02,.85,STRESS_EDGE)
    stress, _ = candidate(book,side,LARGE_STAKE,probability,terms,.02,.85,STRESS_EDGE,stressed=True)
    if not (large and stress):
        return None
    checked = commit_time()
    if checked is None or checked > start+95:
        return None
    fill, harder = large["fill"], stress["fill"]
    db.execute("INSERT OR IGNORE INTO unified_large_trials VALUES (?,?,?,?,?,?,?,?)",
               (slug,checked,side,fill["quantity"],fill["total"],harder["quantity"],harder["total"],
                json.dumps(dict(audit,fill=fill,stress_fill=harder,probability=probability))))
    gate = matched_large(db,checked)
    audit["large_gate"] = gate
    return dict(candidate=large,side=side,up=probability if side=="UP" else 1-probability,gate=gate)


def observe(db, slug, now, mode, model, raw_up, reason, details, book, terms, commit_time):
    """Evaluate one current observation on the caller's verified book transaction."""
    end, start = details["window_end"], details["window_start"]
    block = reason  # set before any model ran: missing/stale data, empty book, invalid terms
    now = commit_time()
    if now is None or not start <= now < end:
        return
    db.execute("""INSERT OR IGNORE INTO unified_rounds VALUES (?,?,?,?,?,?,?,?)""",
               (slug,start,end,now,"WAITING",model,None,"{}"))
    order = db.execute("SELECT * FROM unified_trades WHERE slug=?", (slug,)).fetchone()
    up = None
    audit = {k: details.get(k) for k in ("reference_timestamp","book_timestamp","book_checked",
                                         "volatility","elapsed","reference_price","opening_price",
                                         "model_evidence")}
    audit["raw_up"] = raw_up
    if block:
        audit["data_block"] = True
    if not reason:
        midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
        up, labels = calibrate(db,slug,model,raw_up,midpoint,now)
        audit.update(calibration_labels=labels, midpoint=midpoint)
        db.execute("INSERT OR IGNORE INTO unified_forecasts VALUES (?,?,?,?,?)", (slug,model,now,raw_up,up))
    if mode != "SIMULATE":
        return
    premium = None if reason else premium_trial(db,slug,start,now,book,terms,audit,commit_time)
    if order and order["status"]!="OPEN":
        return
    balance = db.execute("SELECT balance FROM unified_account WHERE id=1").fetchone()[0]
    if order:
        entry_confirmation = json.loads(order["details"])["book_checked"]
        if (reason or now <= order["time"] or order["stake"]==LARGE_STAKE or order["model"]!=model
                or details["book_checked"] <= entry_confirmation):
            return
        held = up if order["side"]=="UP" else 1-up
        sale = execute(book,order["side"],quantity=order["quantity"],buying=False,**terms)
        if (sale["quantity"]+1e-9 >= order["quantity"]
                and sale["total"] >= held*order["quantity"]+EXIT_MARGIN*order["cost"]):
            checked = commit_time()
            if checked is not None:
                close(db,order,checked,sale["total"],"SOLD",dict(audit,fill=sale,hold_probability=held))
        return

    entry = None
    side = None
    stake = BASE_STAKE
    if not reason:
        if premium and premium["gate"]["qualified"] and balance >= LARGE_STAKE:
            entry, side, up = premium["candidate"], premium["side"], premium["up"]
            stake, model = LARGE_STAKE, "selective-90-v1"
        if not entry and balance >= BASE_STAKE:
            choices = [(side,*candidate(book,side,BASE_STAKE,p,terms,.40,.70,MIN_EDGE))
                       for side,p in (("UP",up),("DOWN",1-up))]
            eligible = [(direction,result) for direction,result,_ in choices if result]
            if eligible:
                side,entry = max(eligible,key=lambda item:item[1]["ev"])
            else:
                reason = "/".join(f"{direction}:{why}" for direction,_,why in choices)
        elif not entry:
            reason = "INSUFFICIENT_CASH"
    if entry and db.execute("SELECT 1 FROM unified_trades WHERE status='OPEN'").fetchone():
        entry, reason = None, "PRIOR_POSITION_OPEN"
    if entry:
        checked = commit_time()
        if checked is None or (stake==LARGE_STAKE and checked>start+95):
            return
        fill = entry["fill"]
        audit.update(fill=fill, expected_value=entry["ev"])
        db.execute("""INSERT INTO unified_trades
            (slug,time,side,model,up,stake,quantity,cost,fee,status,details)
            VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?)""",
            (slug,checked,side,model,up,stake,fill["quantity"],fill["total"],fill["fee"],json.dumps(audit)))
        debit = db.execute("""UPDATE unified_account SET balance=round(balance-?,10)
            WHERE id=1 AND balance>=?""", (fill["total"],fill["total"]))
        if debit.rowcount!=1:
            raise ValueError("Unified paper debit failed")
        now, reason = checked, "ENTERED"
    if block and reason != "ENTERED":
        # A later data-availability block (typically the venue emptying the book just before
        # the close) must not overwrite the reason the model already reached for this round.
        prior = db.execute("SELECT reason,details FROM unified_rounds WHERE slug=?", (slug,)).fetchone()
        if prior and prior[0] != "WAITING" and not json.loads(prior[1] or "{}").get("data_block"):
            db.execute("UPDATE unified_rounds SET observed=? WHERE slug=?", (now, slug))
            return
    db.execute("""UPDATE unified_rounds SET observed=?,reason=?,model=?,up=?,details=? WHERE slug=?""",
               (now,reason,model,up,json.dumps(audit),slug))


def history(db, limit=-1):
    return db.execute("""SELECT t.*,r.end,v.outcome,
        CASE WHEN v.outcome IS NOT NULL THEN (t.side=v.outcome)*t.quantity-t.cost END AS hold_pnl
        FROM unified_trades t JOIN unified_rounds r ON r.slug=t.slug
        LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end
        ORDER BY t.time DESC LIMIT ?""",(limit,))


def summary(db, now):
    account = db.execute("SELECT * FROM unified_account WHERE id=1").fetchone()
    if not account:
        return None
    rows = db.execute("""SELECT pnl FROM unified_trades WHERE status!='OPEN'
        ORDER BY closed,slug""").fetchall()
    pnl = [r["pnl"] for r in rows]
    results = ["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in pnl]
    pos = db.execute("""SELECT side,model,up,stake,slug,time FROM unified_trades
        WHERE status='OPEN'""").fetchone()
    latest = db.execute("""SELECT r.slug,r.reason,r.observed,t.status,t.closed FROM unified_rounds r
        LEFT JOIN unified_trades t ON t.slug=r.slug ORDER BY r.start DESC LIMIT 1""").fetchone()
    recent = [dict(r) for r in db.execute("SELECT slug,time,side,closed,status FROM unified_trades ORDER BY time DESC LIMIT 120")]
    played = len(pnl)
    return dict(start=account["started"],balance=account["balance"],played=played,
                wins=results.count("WIN"),losses=results.count("LOSS"),net=sum(pnl),
                streaks=streaks(results,"WIN","LOSS"),position=dict(pos) if pos else None,
                latest=dict(latest) if latest else None,recent=recent)

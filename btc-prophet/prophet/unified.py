"""The 0.3.21 paper account and its matched, unfunded comparisons."""

import json
import math

import logging

from . import brain, feed_check, fresh, selective50, shadow_rules
from .analytics import streaks
from .execution import execute, paper_buy

log = logging.getLogger(__name__)
STARTING_CASH = 1000
BASE_STAKE = 5
LARGE_STAKE = 100
MIN_EDGE = .10
EXIT_MARGIN = .03
STOP_LOSS_SHADOW = .50   # shadow only: a full sale at or below 50% of cost


def primary_exit():
    """'probability' (default since 0.3.23-dev11): sell early when the bid beats the model's own
    hold value by EXIT_MARGIN. The owner runs early exit; dev11 makes it the default so a restart
    without PROPHET_PRIMARY_EXIT can no longer silently switch the bot to holding.
    Set PROPHET_PRIMARY_EXIT=hold to hold every position to official settlement."""
    import os
    return "hold" if os.getenv("PROPHET_PRIMARY_EXIT", "probability").lower() == "hold" else "probability"
STRESS_EDGE = .03
LARGE_MIN_ROUNDS = 100
LARGE_MAX_DRAWDOWN = 250


def initialize(db):
    selective50.initialize(db)
    shadow_rules.initialize(db)
    feed_check.initialize(db)
    fresh.initialize(db)
    brain.initialize(db)
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
        CREATE TABLE IF NOT EXISTS unified_minute_forecasts (
            slug TEXT NOT NULL, model TEXT NOT NULL, minute INTEGER NOT NULL, time REAL NOT NULL,
            raw_up REAL NOT NULL, PRIMARY KEY(slug,model,minute));
        CREATE TABLE IF NOT EXISTS unified_trades (
            slug TEXT PRIMARY KEY REFERENCES unified_rounds(slug), time REAL NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('UP','DOWN')), model TEXT NOT NULL, up REAL NOT NULL,
            stake INTEGER NOT NULL CHECK(stake IN (5,100)), quantity REAL NOT NULL CHECK(quantity>0),
            cost REAL NOT NULL CHECK(cost>0 AND cost<=stake), fee REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','SOLD','SETTLED')),
            closed REAL, proceeds REAL, pnl REAL, details TEXT NOT NULL, exit_details TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS unified_one_open ON unified_trades(status) WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS unified_exit_shadows (
            slug TEXT NOT NULL REFERENCES unified_trades(slug), policy TEXT NOT NULL,
            time REAL NOT NULL, proceeds REAL NOT NULL, details TEXT NOT NULL,
            PRIMARY KEY(slug,policy));
        CREATE TABLE IF NOT EXISTS unified_large_trials (
            slug TEXT PRIMARY KEY REFERENCES unified_rounds(slug), time REAL NOT NULL,
            side TEXT NOT NULL, quantity REAL NOT NULL, cost REAL NOT NULL,
            stress_quantity REAL NOT NULL, stress_cost REAL NOT NULL, details TEXT NOT NULL);
    """)


MINUTE_LABELS = 30   # settled same-minute forecasts needed before per-minute learning takes over


EXIT_BOOK_MAX_AGE = 5


def _hourly_accounts(db):
    from . import hourly
    hourly.initialize(db)
    return hourly.accounts(db)


def calibrate(db, slug, model, raw_up, midpoint, now, minute=None):
    """Fit an auditable confidence bucket with at most one causal label per market.

    dev11: forecasts are also remembered once per minute of every round. When at least
    MINUTE_LABELS settled forecasts exist for the same model, minute and confidence bucket, the
    bot learns from those (a 70% call at minute 3 is judged against earlier minute-3 calls, not
    against minute-0 calls). Until then it uses the original first-forecast labels.
    Returns (probability, label count, source)."""
    side = raw_up >= .5
    confidence = max(raw_up, 1 - raw_up)
    low = min(.9, math.floor(confidence * 10) / 10)
    hi = 1.00000001 if low == .9 else low + .1
    prior = min(.99, max(.01, .5 * raw_up + .5 * midpoint))
    if minute is not None:
        rows = db.execute("""SELECT f.raw_up, v.outcome FROM unified_minute_forecasts f
            JOIN unified_rounds r ON r.slug=f.slug
            JOIN verified_markets v ON v.slug=f.slug AND v.end=r.end
            WHERE f.model=? AND f.minute=? AND f.slug<>? AND f.time<r.end AND v.end<=?
            AND v.verified_at<? AND max(f.raw_up,1-f.raw_up)>=? AND max(f.raw_up,1-f.raw_up)<?
            ORDER BY v.end DESC LIMIT 150""", (model, minute, slug, now, now, low, hi)).fetchall()
        if len(rows) >= MINUTE_LABELS:
            matches = sum((row["raw_up"] >= .5) == (row["outcome"] == "UP") for row in rows)
            adjusted_side = (matches + 30 * (prior if side else 1-prior)) / (len(rows) + 30)
            return (adjusted_side if side else 1-adjusted_side), len(rows), "minute"
    rows = db.execute("""SELECT f.raw_up, v.outcome FROM unified_forecasts f
        JOIN unified_rounds r ON r.slug=f.slug
        JOIN verified_markets v ON v.slug=f.slug AND v.end=r.end
        WHERE f.model=? AND f.slug<>? AND f.time<r.end AND v.end<=?
        AND v.verified_at<? AND max(f.raw_up,1-f.raw_up)>=?
        AND max(f.raw_up,1-f.raw_up)<?
        ORDER BY v.end DESC LIMIT 150""", (model, slug, now, now, low, hi)).fetchall()
    # Until similar settled forecasts accumulate, shrink raw confidence toward market odds.
    matches = sum((row["raw_up"] >= .5) == (row["outcome"] == "UP") for row in rows)
    weight = len(rows)
    adjusted_side = (matches + 30 * (prior if side else 1-prior)) / (weight + 30)
    return (adjusted_side if side else 1-adjusted_side), weight, "first_forecast"


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
    try:
        selective50.settle(db, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("Selective $50 settlement skipped: %s", exc)
    try:
        shadow_rules.settle(db, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("Shadow test accounts settlement skipped: %s", exc)
    db.execute("SAVEPOINT fresh_settle")
    try:
        fresh.settle(db, now)
        db.execute("RELEASE fresh_settle")
    except Exception as exc:  # noqa: BLE001 - research accounts must never stop the main bot
        db.execute("ROLLBACK TO fresh_settle")
        db.execute("RELEASE fresh_settle")
        log.warning("dev16 fresh-data settlement skipped: %s", exc)


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
    choice = fresh.main_model(db)   # owner switch: standard / settlement / settlement_live / selective
    up = None
    audit = {k: details.get(k) for k in ("reference_timestamp","book_timestamp","book_checked",
                                         "volatility","elapsed","reference_price","opening_price",
                                         "model_evidence")}
    audit["raw_up"] = raw_up
    if block:
        audit["data_block"] = True
    if not reason:
        midpoint = (book["bids"][0]["price"]+book["asks"][0]["price"])/2
        minute = min(4, max(0, int((now-start)//60)))
        up, labels, source = calibrate(db,slug,model,raw_up,midpoint,now,minute)
        audit.update(calibration_labels=labels, calibration_source=source, midpoint=midpoint)
        db.execute("INSERT OR IGNORE INTO unified_forecasts VALUES (?,?,?,?,?)", (slug,model,now,raw_up,up))
        db.execute("INSERT OR IGNORE INTO unified_minute_forecasts VALUES (?,?,?,?,?)",
                   (slug,model,minute,now,raw_up))
    if not reason:
        db.execute("SAVEPOINT fresh_observe")
        try:
            settle_up = fresh.observe(db,slug,now,details,book,terms,mode)
            db.execute("RELEASE fresh_observe")
        except Exception as exc:  # noqa: BLE001 - research must never stop the main bot
            db.execute("ROLLBACK TO fresh_observe")
            db.execute("RELEASE fresh_observe")
            log.warning("dev16 fresh-data observation skipped %s: %s", slug, exc)
            settle_up = None
        settle_up = settle_up or {}
        audit["settlement_up"], audit["settlement_live_up"] = settle_up.get("standard"), settle_up.get("live")
        pick = settle_up.get("live") if choice == "settlement_live" else settle_up.get("standard") if choice == "settlement" else None
        if pick is not None:
            # Owner switch (off by default): the main account uses the official-rule forecast.
            up = pick
            audit["main_forecast"] = choice
        try:
            brain.record(db, slug, now, start, dict(market=midpoint, main=up, settlement=settle_up.get("standard"),
                                                    settlement_live=settle_up.get("live")))
        except Exception as exc:  # noqa: BLE001 - the brain must never stop the bot
            log.warning("Brain record skipped %s: %s", slug, exc)
    try:
        feed_check.record(db,slug,now,start,details)
    except Exception as exc:  # noqa: BLE001 - logging must never stop the bot
        log.warning("Feed check skipped %s: %s", slug, exc)
    if mode != "SIMULATE":
        return
    try:
        selective50.observe(db,slug,now,start,end,details,raw_up,book,terms,bool(reason))
    except Exception as exc:  # noqa: BLE001 - the shadow account must never stop the main bot
        log.warning("Selective $50 account skipped %s: %s", slug, exc)
    db.execute("SAVEPOINT shadow_rules")
    try:
        shadow_rules.observe(db,slug,now,start,end,raw_up,up,book,terms,bool(reason))
        db.execute("RELEASE shadow_rules")
    except Exception as exc:  # noqa: BLE001 - shadow accounts must never stop the main bot
        db.execute("ROLLBACK TO shadow_rules")
        db.execute("RELEASE shadow_rules")
        log.warning("Shadow test accounts skipped %s: %s", slug, exc)
    premium = None if reason else premium_trial(db,slug,start,now,book,terms,audit,commit_time)
    if order and order["status"]!="OPEN":
        return
    balance = db.execute("SELECT balance FROM unified_account WHERE id=1").fetchone()[0]
    if order:
        entry_confirmation = json.loads(order["details"])["book_checked"]
        if (reason or now <= order["time"] or order["stake"]==LARGE_STAKE or order["model"]!=model
                or details["book_checked"] <= entry_confirmation):
            return
        if details.get("book_checked") and now - details["book_checked"] > EXIT_BOOK_MAX_AGE:
            return   # dev16: never sell (or record a shadow sale) on a book fetched more than 5 s ago
            # (book_timestamp is the exchange's last-trade time, not quote freshness, so it is not used)
        if choice == "selective":
            mirror = db.execute("SELECT status FROM selective50_trades WHERE slug=?", (slug,)).fetchone()
            if mirror and mirror["status"] == "SOLD":       # the Selective rule's reversal exit fired
                sale = execute(book,order["side"],quantity=order["quantity"],buying=False,**terms)
                checked = commit_time()
                if checked is not None and sale["quantity"]+1e-9 >= order["quantity"]:
                    close(db,order,checked,sale["total"],"SOLD",dict(audit,fill=sale,exit="selective_reversal"))
            return                                          # otherwise hold to the end, like the Selective rule
        held = up if order["side"]=="UP" else 1-up
        sale = execute(book,order["side"],quantity=order["quantity"],buying=False,**terms)
        if sale["quantity"]+1e-9 < order["quantity"]:
            return
        triggered = []
        if sale["total"] >= held*order["quantity"]+EXIT_MARGIN*order["cost"]:
            triggered.append("probability_exit")
        if sale["total"] <= STOP_LOSS_SHADOW*order["cost"]:
            triggered.append("stop_loss_50")
        if up is not None and held < .5:
            # Shadow only: the model now favours the other side (the "reversal exit" that held up
            # in research for the Selective rule). Compared in Exit Shadow Comparison.
            triggered.append("reversal_exit")
        if not triggered:
            return
        checked = commit_time()
        if checked is None:
            return
        evidence = json.dumps(dict(audit,fill=sale,hold_probability=held))
        for policy in triggered:
            db.execute("INSERT OR IGNORE INTO unified_exit_shadows VALUES (?,?,?,?,?)",
                       (order["slug"],policy,checked,sale["total"],evidence))
        if "probability_exit" in triggered and primary_exit() == "probability":
            close(db,order,checked,sale["total"],"SOLD",dict(audit,fill=sale,hold_probability=held))
        return

    entry = None
    side = None
    stake = BASE_STAKE
    if not reason:
        if choice == "selective":
            # Owner switch: the main account takes exactly the Selective rule's trades, at $5.
            mirror = db.execute("SELECT side FROM selective50_trades WHERE slug=? AND status='OPEN'", (slug,)).fetchone()
            reason = "SELECTIVE_RULE_WAITING"
            if mirror and balance >= BASE_STAKE:
                fill, why = paper_buy(book, mirror["side"], BASE_STAKE, **terms)
                if fill and fill["total"] / fill["quantity"] <= .90:
                    entry, side, reason = dict(fill=fill, ev=None), mirror["side"], None
                else:
                    reason = "SELECTIVE_" + (why or "PRICE_ABOVE_90C")
        elif premium and premium["gate"]["qualified"] and balance >= LARGE_STAKE:
            entry, side, up = premium["candidate"], premium["side"], premium["up"]
            stake, model = LARGE_STAKE, "selective-90-v1"
        if choice == "selective":
            pass
        elif not entry and balance >= BASE_STAKE:
            choices = [(side,*candidate(book,side,BASE_STAKE,p,terms,.40,.70,MIN_EDGE))
                       for side,p in (("UP",up),("DOWN",1-up))]
            eligible = [(direction,result) for direction,result,_ in choices if result]
            if eligible:
                side,entry = max(eligible,key=lambda item:item[1]["ev"])
            else:
                reason = "/".join(f"{direction}:{why}" for direction,_,why in choices)
        elif not entry and choice != "selective":
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


def exit_comparison(db):
    """Settled primary trades: actual P&L vs each shadow exit policy (first trigger, full sale)."""
    trades = db.execute("""SELECT t.slug,t.side,t.quantity,t.cost,t.pnl,t.status,v.outcome FROM unified_trades t
        JOIN unified_rounds r ON r.slug=t.slug JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end
        WHERE t.status<>'OPEN'""").fetchall()
    shadows = {}
    for r in db.execute("SELECT slug,policy,proceeds FROM unified_exit_shadows"):
        shadows.setdefault(r["policy"], {})[r["slug"]] = r["proceeds"]
    rows = []
    hold = [(t["side"]==t["outcome"])*t["quantity"]-t["cost"] for t in trades]
    rows.append(dict(policy="hold_to_settlement", trades=len(trades), exits=0,
                     wins=sum(v>0 for v in hold), pnl=round(sum(hold),4)))
    rows.append(dict(policy="actual", trades=len(trades), exits=None,
                     wins=sum(t["pnl"]>0 for t in trades), pnl=round(sum(t["pnl"] for t in trades),4)))
    # dev16 fix: trades the bot really sold count at their real sale price. Before, a sale with no
    # matching shadow row fell back to the hold result, so this sheet overstated early selling.
    sold = [(t,h) for t,h in zip(trades,hold) if t["status"]=="SOLD"]
    rows.append(dict(policy="actual_early_sales_vs_hold", trades=len(sold), exits=len(sold),
                     wins=sum(t["pnl"]>h for t,h in sold), pnl=round(sum(t["pnl"]-h for t,h in sold),4)))
    for policy in ("probability_exit", "stop_loss_50", "reversal_exit"):
        fired = shadows.get(policy, {})
        values = [(t["pnl"] if policy=="probability_exit" and t["status"]=="SOLD"
                   else (fired[t["slug"]]-t["cost"]) if t["slug"] in fired else h) for t,h in zip(trades,hold)]
        rows.append(dict(policy=policy, trades=len(trades), exits=sum(t["slug"] in fired for t in trades),
                         wins=sum(v>0 for v in values), pnl=round(sum(values),4)))
    return rows


def history(db, limit=-1):
    return db.execute("""SELECT t.*,r.end,v.outcome,
        CASE WHEN v.outcome IS NOT NULL THEN (t.side=v.outcome)*t.quantity-t.cost END AS hold_pnl
        FROM unified_trades t JOIN unified_rounds r ON r.slug=t.slug
        LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end
        ORDER BY t.time DESC LIMIT ?""",(limit,))


SCOREBOARD_KEY = "unified_scoreboard_since"


def scoreboard_since(db):
    """Display-only counting start. Trades, cash and history are never changed by a reset."""
    row = db.execute("SELECT payload FROM state WHERE key=?", (SCOREBOARD_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def reset_scoreboard(db, now):
    db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)", (SCOREBOARD_KEY, json.dumps(now), now))
    return now


def summary(db, now):
    account = db.execute("SELECT * FROM unified_account WHERE id=1").fetchone()
    if not account:
        return None
    since = scoreboard_since(db)
    every = db.execute("""SELECT pnl,time FROM unified_trades WHERE status!='OPEN'
        ORDER BY closed,slug""").fetchall()
    rows = [r for r in every if since is None or r["time"] >= since]
    pnl = [r["pnl"] for r in rows]
    results = ["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in pnl]
    pos = db.execute("""SELECT side,model,up,stake,slug,time FROM unified_trades
        WHERE status='OPEN'""").fetchone()
    latest = db.execute("""SELECT r.slug,r.reason,r.observed,t.status,t.closed FROM unified_rounds r
        LEFT JOIN unified_trades t ON t.slug=r.slug ORDER BY r.start DESC LIMIT 1""").fetchone()
    recent = [dict(r) for r in db.execute("SELECT slug,time,side,closed,status FROM unified_trades ORDER BY time DESC LIMIT 120")]
    played = len(pnl)
    all_pnl = [r["pnl"] for r in every]
    current = db.execute("""SELECT r.slug,r.start,r.end,r.reason,t.side,t.status FROM unified_rounds r
        LEFT JOIN unified_trades t ON t.slug=r.slug ORDER BY r.start DESC LIMIT 1""").fetchone()
    history = [dict(h) for h in db.execute("""SELECT r.slug,r.start,r.end,r.reason,t.side,t.pnl,t.status,v.outcome
        FROM unified_rounds r LEFT JOIN unified_trades t ON t.slug=r.slug
        LEFT JOIN verified_markets v ON v.slug=r.slug AND v.end=r.end
        WHERE r.end<=? ORDER BY r.start DESC LIMIT 25""", (now,))]
    last = db.execute("""SELECT t.slug,t.side,t.pnl,t.closed,v.outcome FROM unified_trades t
        JOIN unified_rounds r ON r.slug=t.slug
        LEFT JOIN verified_markets v ON v.slug=t.slug AND v.end=r.end
        WHERE t.status!='OPEN' ORDER BY t.closed DESC,t.slug DESC LIMIT 1""").fetchone()
    return dict(start=account["started"],balance=account["balance"],played=played,
                counting_since=since or account["started"],reset=since is not None,
                last_settled=dict(last) if last else None,
                current=dict(current, now=now) if current else None, history=history,
                all_time=dict(played=len(all_pnl),wins=sum(p>0 for p in all_pnl),
                              losses=sum(p<0 for p in all_pnl),net=sum(all_pnl)),
                wins=results.count("WIN"),losses=results.count("LOSS"),net=sum(pnl),
                streaks=streaks(results,"WIN","LOSS"),position=dict(pos) if pos else None,
                latest=dict(latest) if latest else None,recent=recent,
                selective50=selective50.summary(db), selective1000=selective50.summary(db,"selective1000"), shadow_rules=shadow_rules.summary(db), feed_check=feed_check.summary(db), fresh_accounts=fresh.accounts(db)+_hourly_accounts(db), freshness=fresh.lag_summary(db), main_settlement=fresh.main_uses_settlement(db), main_model=fresh.main_model(db))

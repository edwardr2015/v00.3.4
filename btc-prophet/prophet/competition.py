"""One comparable $1, 90-second account for each directional participant."""
import json
import math

from .analytics import streaks
from .execution import execute
from .models import PARTICIPANTS, VOTE_FAMILY
from .adaptive_entry import snapshot, freshness, get_state
from .providers import validate_market


def standings(db, now, catalog):
    versions = {item["id"]:item for item in catalog}
    data = snapshot(db)
    book = data.get("book") or {}
    market = data.get("market") or {}
    results = []
    for family,label in PARTICIPANTS.items():
        participant = versions.get(family,{})
        configs = db.execute("""SELECT * FROM lab_configs WHERE family=? AND checkpoint=90
            AND stake=1 AND exit='HOLD' AND id LIKE 'timing/v2/%'
            ORDER BY (status='FORWARD SHADOW') DESC,created DESC,id DESC LIMIT 1""",(family,)).fetchone()
        orders = db.execute("""SELECT * FROM lab_orders WHERE config=? ORDER BY settled,id""",
                            (configs["id"],)).fetchall() if configs else []
        cash = configs["cash"] if configs else 1000
        done = [o["pnl"] for o in orders if o["pnl"] is not None]
        open_orders = [o for o in orders if o["status"]=="OPEN"]
        exposure = math.fsum(o["cost"] for o in open_orders)
        equity = cash if not open_orders else None
        if len(open_orders)==1 and not freshness(data,now) and validate_market(market,now):
            order = open_orders[0]
            if book.get("slug")==market.get("slug")==order["slug"] and book.get("state")=="MARKET_STATE_OPEN":
                terms = json.loads(order["details"]).get("prediction",{})
                if all(k in terms for k in ("theta","tick","quantum")):
                    mark = execute(book,order["side"],quantity=order["remaining"],buying=False,
                        theta=terms["theta"],tick=terms["tick"],quantum=terms["quantum"])
                    if mark["quantity"]+1e-9>=order["remaining"]:
                        equity = cash+mark["total"]
        settled_equity = peak = 1000
        drawdown=0
        for pnl in done:
            settled_equity += pnl
            peak=max(peak,settled_equity)
            drawdown=max(drawdown,peak-settled_equity)
        stats=streaks(["WIN" if p>0 else "LOSS" if p<0 else "FLAT" for p in done],"WIN","LOSS")
        version = configs["version"] if configs else None
        skips = db.execute("SELECT count(*) FROM lab_skips WHERE config=?",(configs["id"],)).fetchone()[0] if configs else 0
        games = db.execute("""SELECT count(*) FROM (
            SELECT slug FROM lab_predictions WHERE version=? AND checkpoint=90
            UNION SELECT slug FROM lab_orders WHERE config=?
            UNION SELECT slug FROM lab_skips WHERE config=?)""",
            (version,configs["id"],configs["id"])).fetchone()[0] if configs else 0
        last = db.execute("""SELECT slug,time,up FROM lab_predictions
            WHERE version=? AND time<=? ORDER BY time DESC LIMIT 1""",(version,now)).fetchone() if version else None
        model_health = db.execute("SELECT status,detail FROM model_forecast_health WHERE version=?",
                                  (version,)).fetchone() if version else None
        status = participant.get("status", "NOT TRAINED")
        if status == "AVAILABLE":
            status = model_health["status"] if model_health else "WAITING FOR FORWARD FORECAST"
        vote_row = db.execute("""SELECT slug,checkpoint,time,evidence FROM model_vote_forecasts
                                  WHERE time<=? ORDER BY time DESC LIMIT 1""",(now,)).fetchone() if family==VOTE_FAMILY else None
        vote = dict(json.loads(vote_row["evidence"]), slug=vote_row["slug"],
                    checkpoint=vote_row["checkpoint"],time=vote_row["time"]) if vote_row else None
        results.append(dict(vote=vote,model=family,label=label,status=status,version=version,
            model_detail=model_health["detail"] if model_health else None,
            cash=cash,equity=equity,realized=math.fsum(done),net_pnl=equity-1000 if equity is not None else None,
            unrealized=equity-cash-exposure if equity is not None else None,exposure=exposure,drawdown=drawdown,
            wins=sum(p>0 for p in done),losses=sum(p<0 for p in done),settled=len(done),skips=skips,
            games=games,win_rate=sum(p>0 for p in done)/len(done) if done else None,
            streak=stats,prediction=dict(last) if last else None))
    return results

LEARNING_SECONDS = 48 * 3600


def preflight(db, now):
    """Require recent, simultaneous evidence from all active model versions."""
    clock = get_state(db, "competition_learning_clock") or {}
    elapsed = clock.get("seconds", 0)
    active = {r["family"]: r["version"] for r in db.execute("""SELECT family,version
        FROM lab_configs WHERE checkpoint=90 AND stake=1 AND exit='HOLD'
        AND status='FORWARD SHADOW' AND id LIKE 'timing/v2/%'""") if r["family"] in PARTICIPANTS}
    blocked_versions = {r["version"] for r in db.execute(
        "SELECT version FROM model_forecast_health WHERE status!='READY'")}
    rounds = {}
    for row in db.execute("""SELECT config_family,version,slug,up,time
        FROM lab_predictions WHERE checkpoint=90 AND time BETWEEN ? AND ?""", (now-600, now)):
        if (active.get(row["config_family"]) == row["version"]
                and row["version"] not in blocked_versions and 0 <= row["up"] <= 1):
            rounds.setdefault(row["slug"], set()).add(row["config_family"])
    shared = next((slug for slug, families in rounds.items() if families == set(PARTICIPANTS)), None)
    observed = set().union(*rounds.values())
    reasons = []
    if elapsed < LEARNING_SECONDS:
        reasons.append("48 hours of recorded active learning required")
    if not shared:
        missing = [label for family,label in PARTICIPANTS.items() if family not in observed]
        reasons.append("No recent common round with nine valid forecasts" +
                       (": missing " + ", ".join(missing) if missing else ""))
    data = snapshot(db)
    book, market = data.get("book") or {}, data.get("market") or {}
    block = freshness(data, now)
    if (block or not market or not validate_market(market, now)
            or book.get("slug") != market.get("slug") or book.get("state") != "MARKET_STATE_OPEN"
            or not book.get("bids") or not book.get("asks")):
        reasons.append("Fresh eligible market required" + (": " + block if block else ""))
    return dict(ready=not reasons, reasons=reasons, observed=sorted(observed), shared_round=shared,
                versions=active, learning_seconds=elapsed, learning_started_utc=clock.get("started"),
                learning_remaining_seconds=max(0, LEARNING_SECONDS-elapsed))

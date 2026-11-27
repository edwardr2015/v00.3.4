"""Shadow test accounts (dev11): three candidate fixes run side by side on paper.

Each account starts with $50, makes $3 trades, at most one per round, never spends money it does
not have (so it can never go below $0) and never has money added. Paper only: it never sends
orders and never touches the main $1,000 account or the Selective $50 account.

  skip_minute_2   The main rule exactly (same forecast, 40-70c, 10% edge, same early exit) but it
                  never enters during minute 2 (120-180 s), where Report 14 showed 8 right of 22.
  cheap_40_55     The main rule, but it only buys when it pays 40-55c, so a win pays more.
  agree_90s       From 90 s, buy the side the Reference Gap model and the market price agree on,
                  40-70c, 3% edge, hold to settlement (research rule 90s_gap_agreement_40_70).

dev14 adds three more, from the 60-day simulation and Report 18 (the model was overconfident:
it said 68% on average, the market said 58%, and 56% actually won):

  reality_check       The main forecast pulled 70% of the way back toward the market price
                      (blend = market + 0.3 x (model - market)). Buys only if the blended chance
                      still beats the price by 3%, 30-70c, sells early like the main rule.
  reality_check_hold  The same entry, but holds to the end.
  gap_40_49           The main rule, but only buys at 40-49c (best research band in Report 18).

The point is evidence: if one of these keeps beating the main account on fresh rounds, switching
the main account to it is a decision for the owner, not something this module does.
"""
import json

from .execution import execute, paper_buy

STARTING_CASH = 50.0
STAKE = 3.0
MAIN_EDGE = .10
EXIT_MARGIN = .03
BLEND = .30   # how much of the model's disagreement with the market to trust

RULES = {
    "skip_minute_2": dict(label="Main rule, skips minute 2", band=(.40, .70), edge=MAIN_EDGE,
                          exit="probability", describe="Same as main · never enters 2:00–3:00"),
    "cheap_40_55": dict(label="Main rule, pays 40–55¢ only", band=(.40, .55), edge=MAIN_EDGE,
                        exit="probability", describe="Same as main · only buys at 40–55¢"),
    "agree_90s": dict(label="90-second agreement", band=(.40, .70), edge=.03, exit="hold",
                      describe="From 1:30 · model and market agree · 40–70¢ · holds to the end"),
    "reality_check": dict(label="Reality check (model + market)", band=(.30, .70), edge=.03,
                          exit="probability", blend=True,
                          describe="Model pulled 70% toward market price · 30–70¢ · sells early"),
    "reality_check_hold": dict(label="Reality check, holds", band=(.30, .70), edge=.03, exit="hold",
                               blend=True, describe="Same reality check · holds to the end"),
    "gap_40_49": dict(label="Main rule, pays 40–49¢ only", band=(.40, .4999), edge=MAIN_EDGE,
                      exit="probability", describe="Same as main · only buys at 40–49¢"),
}


def blended(p, market):
    """Trust only part of the model's disagreement with the market price."""
    return market + BLEND * (p - market)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_rule_accounts (
            rule TEXT PRIMARY KEY, started REAL NOT NULL, balance REAL NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS shadow_rule_trades (
            rule TEXT NOT NULL, slug TEXT NOT NULL, time REAL NOT NULL, end REAL NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('UP','DOWN')), quantity REAL NOT NULL CHECK(quantity>0),
            cost REAL NOT NULL CHECK(cost>0 AND cost<=3), status TEXT NOT NULL
            CHECK(status IN ('OPEN','SOLD','SETTLED')), closed REAL, proceeds REAL, pnl REAL,
            outcome TEXT, details TEXT NOT NULL, PRIMARY KEY(rule,slug));""")


def _balance(db, rule, now):
    row = db.execute("SELECT balance FROM shadow_rule_accounts WHERE rule=?", (rule,)).fetchone()
    if row is None:
        db.execute("INSERT INTO shadow_rule_accounts VALUES (?,?,?)", (rule, now, STARTING_CASH))
        return STARTING_CASH
    return row[0]


def _wants(rule, elapsed, up, raw_up, midpoint):
    """Which side (and with what probability) this rule would buy now, or None."""
    if rule == "agree_90s":
        if not 90 <= elapsed < 240 or raw_up == .5 or midpoint == .5 or (raw_up > .5) != (midpoint > .5):
            return None
        return [("UP", raw_up)] if raw_up > .5 else [("DOWN", 1 - raw_up)]
    if up is None or (rule == "skip_minute_2" and 120 <= elapsed < 180):
        return None
    return [("UP", up), ("DOWN", 1 - up)]


def observe(db, slug, now, start, end, raw_up, up, book, terms, blocked):
    """One observation of the current round, after the main model has produced `up`."""
    if blocked or raw_up is None or not book.get("bids") or not book.get("asks"):
        return
    midpoint = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
    elapsed = now - start
    for rule, cfg in RULES.items():
        trade = db.execute("SELECT * FROM shadow_rule_trades WHERE rule=? AND slug=?", (rule, slug)).fetchone()
        if trade:
            if trade["status"] == "OPEN" and cfg["exit"] == "probability" and up is not None:
                held = up if trade["side"] == "UP" else 1 - up
                if cfg.get("blend"):
                    held = blended(held, midpoint if trade["side"] == "UP" else 1 - midpoint)
                sale = execute(book, trade["side"], quantity=trade["quantity"], buying=False, **terms)
                if (sale["quantity"] + 1e-9 >= trade["quantity"]
                        and sale["total"] >= held * trade["quantity"] + EXIT_MARGIN * trade["cost"]):
                    _close(db, rule, trade, now, sale["total"], "SOLD", None)
            continue
        wants = _wants(rule, elapsed, up, raw_up, midpoint)
        if not wants:
            continue
        if cfg.get("blend"):
            wants = [(side, blended(p, midpoint if side == "UP" else 1 - midpoint)) for side, p in wants]
        balance = _balance(db, rule, now)
        if balance < STAKE:
            continue                                   # out of money: stop, never borrow or top up
        low, high = cfg["band"]
        best = None
        for side, p in wants:
            fill, _ = paper_buy(book, side, STAKE, **terms)
            if not fill or fill["total"] > balance or not low - 1e-10 <= fill["average_price"] <= high + 1e-10:
                continue
            if fill["total"] / fill["quantity"] > .85:
                continue
            ev = p * fill["quantity"] - fill["total"]
            if ev < cfg["edge"] * fill["total"]:
                continue
            if best is None or ev > best[2]:
                best = (side, fill, ev, p)
        if not best:
            continue
        side, fill, ev, p = best
        db.execute("""INSERT INTO shadow_rule_trades(rule,slug,time,end,side,quantity,cost,status,details)
            VALUES (?,?,?,?,?,?,?,'OPEN',?)""", (rule, slug, now, end, side, fill["quantity"], fill["total"],
            json.dumps(dict(elapsed=elapsed, probability=p, midpoint=midpoint, raw_up=raw_up,
                            expected_value=ev, fee=fill["fee"], average_price=fill["average_price"]))))
        db.execute("UPDATE shadow_rule_accounts SET balance=round(balance-?,10) WHERE rule=?",
                   (fill["total"], rule))


def _close(db, rule, trade, now, proceeds, status, outcome):
    done = db.execute("""UPDATE shadow_rule_trades SET status=?,closed=?,proceeds=?,pnl=?,outcome=?
        WHERE rule=? AND slug=? AND status='OPEN'""",
        (status, now, proceeds, round(proceeds - trade["cost"], 10), outcome, rule, trade["slug"]))
    if done.rowcount == 1:
        db.execute("UPDATE shadow_rule_accounts SET balance=round(balance+?,10) WHERE rule=?", (proceeds, rule))


def settle(db, now):
    for t in db.execute("""SELECT t.*,v.outcome AS verified FROM shadow_rule_trades t
        JOIN verified_markets v ON v.slug=t.slug AND v.end=t.end
        WHERE t.status='OPEN' AND v.end<=? AND v.verified_at<=?""", (now, now)).fetchall():
        _close(db, t["rule"], t, now, t["quantity"] if t["side"] == t["verified"] else 0.0, "SETTLED", t["verified"])
    # Record the official outcome on early sales too, so "would holding have won?" is visible.
    db.execute("""UPDATE shadow_rule_trades SET outcome=(SELECT v.outcome FROM verified_markets v
        WHERE v.slug=shadow_rule_trades.slug AND v.end=shadow_rule_trades.end AND v.verified_at<=?)
        WHERE status='SOLD' AND outcome IS NULL""", (now,))


def summary(db):
    out = []
    for rule, cfg in RULES.items():
        acct = db.execute("SELECT * FROM shadow_rule_accounts WHERE rule=?", (rule,)).fetchone()
        rows = db.execute("SELECT pnl,status FROM shadow_rule_trades WHERE rule=? AND status!='OPEN'",
                          (rule,)).fetchall()
        pnl = [r["pnl"] for r in rows]
        out.append(dict(rule=rule, name=cfg["label"], describe=cfg["describe"], start=STARTING_CASH,
                        stake=STAKE, balance=acct["balance"] if acct else STARTING_CASH,
                        started=acct["started"] if acct else None, trades=len(pnl),
                        wins=sum(p > 0 for p in pnl), losses=sum(p <= 0 for p in pnl),
                        net=round(sum(pnl), 4), early_exits=sum(r["status"] == "SOLD" for r in rows),
                        open=db.execute("SELECT COUNT(*) FROM shadow_rule_trades WHERE rule=? AND status='OPEN'",
                                        (rule,)).fetchone()[0]))
    return out

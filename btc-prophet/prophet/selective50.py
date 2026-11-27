"""Selective $50 paper account: the best rule found in research, run live as its own shadow ledger.

Rule (research/verify_logic.py, +$30.62 on 94 replayed markets at $3):
  * enter only between 3:00 and 4:00 into the round,
  * Reference Gap side, the market price agrees, and gap+speed agrees (all three),
  * all-in price per contract <= 90 cents,
  * sell early if the Reference Gap signal flips to the other side (reversal exit), otherwise hold.
Money rules: starts with $50, $3 per trade, one trade per round, never spends money it does not have,
so the balance can never go below zero and nothing is ever added. Paper only; it never sends orders
and never touches the main dashboard account.

dev12 adds a second, separate paper ledger with the same rule at a bigger size: $1,000 start,
$50 per trade. It never forces a trade: it enters only when the same signals agree AND at least
95% of the $50 can be bought at <= 90c from the order book at that moment; otherwise it skips.
An early sale happens only if the whole position can be sold; otherwise it holds to the end.
"""
import json
import logging
import math

from .execution import execute, paper_buy

log = logging.getLogger(__name__)

STARTING_CASH = 50.0
STAKE = 3.0
ENTRY_FROM, ENTRY_TO = 180, 240
MAX_PRICE = .90
NAME = "Selective $50 (min 3-4, all 3 agree, <=90c, reversal exit)"
# Ledgers sharing the rule: prefix -> (name, starting cash, stake, minimum share of stake filled)
LEDGERS = {
    "selective50": (NAME, STARTING_CASH, STAKE, 0.0),
    "selective1000": ("Selective $1,000 at $50 (same rule, full fills only)", 1000.0, 50.0, .95),
}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS selective50_account (
            id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL,
            balance REAL NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS selective50_ticks (
            slug TEXT NOT NULL, t REAL NOT NULL, ref REAL NOT NULL, PRIMARY KEY(slug,t));
        CREATE TABLE IF NOT EXISTS selective50_trades (
            slug TEXT PRIMARY KEY, time REAL NOT NULL, end REAL NOT NULL, side TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity>0), cost REAL NOT NULL CHECK(cost>0 AND cost<=3),
            status TEXT NOT NULL, closed REAL, proceeds REAL, pnl REAL, outcome TEXT,
            details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS selective1000_account (
            id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL,
            balance REAL NOT NULL CHECK(balance>=0));
        CREATE TABLE IF NOT EXISTS selective1000_trades (
            slug TEXT PRIMARY KEY, time REAL NOT NULL, end REAL NOT NULL, side TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity>0), cost REAL NOT NULL CHECK(cost>0 AND cost<=50),
            status TEXT NOT NULL, closed REAL, proceeds REAL, pnl REAL, outcome TEXT,
            details TEXT NOT NULL);""")


def _phi(x):
    return .5 * (1 + math.erf(x / math.sqrt(2)))


def signals(db, slug, now, start, end, details, raw_up, book):
    """The three votes, using only this observation and earlier ones from the same round."""
    ref, opening, vol = details.get("reference_price"), details.get("opening_price"), details.get("volatility")
    if not (ref and opening and vol and raw_up is not None):
        return None
    left = max(end - now, 1.0)
    old = db.execute("""SELECT t,ref FROM selective50_ticks WHERE slug=? AND t>=? AND t<? ORDER BY t LIMIT 1""",
                     (slug, now - 30, now)).fetchone()
    vel = math.log(ref / old["ref"]) / (now - old["t"]) if old and now - old["t"] >= 5 else 0.0
    sigma = max(1e-5, vol * math.sqrt(max(1, left / 15)))
    gap = math.log(ref / opening)
    speed_up = min(.98, max(.02, _phi((gap + .5 * vel * min(left, 60)) / sigma)))
    mid = (book["bids"][0]["price"] + book["asks"][0]["price"]) / 2
    return dict(gap_up=raw_up, market_up=mid, speed_up=speed_up, elapsed=now - start)


def observe(db, slug, now, start, end, details, raw_up, book, terms, blocked):
    if blocked or not book.get("bids") or not book.get("asks"):
        return
    ref = details.get("reference_price")
    s = signals(db, slug, now, start, end, details, raw_up, book)
    if ref:
        db.execute("INSERT OR IGNORE INTO selective50_ticks VALUES (?,?,?)", (slug, now, ref))
    if s is None:
        return
    for prefix in LEDGERS:
        # Each ledger in its own savepoint: a problem in one can never change the other.
        db.execute(f"SAVEPOINT {prefix}")
        try:
            _act(db, prefix, slug, now, end, s, book, terms)
            db.execute(f"RELEASE {prefix}")
        except Exception as exc:  # noqa: BLE001
            db.execute(f"ROLLBACK TO {prefix}")
            db.execute(f"RELEASE {prefix}")
            log.warning("%s skipped %s: %s", prefix, slug, exc)


def _act(db, prefix, slug, now, end, s, book, terms):
    _, cash, stake, min_share = LEDGERS[prefix]
    trade = db.execute(f"SELECT * FROM {prefix}_trades WHERE slug=?", (slug,)).fetchone()
    if trade:
        if trade["status"] == "OPEN" and (s["gap_up"] >= .5) != (trade["side"] == "UP"):
            sale = execute(book, trade["side"], quantity=trade["quantity"], buying=False, **terms)
            if sale["quantity"] + 1e-9 >= trade["quantity"]:
                _close(db, trade, now, sale["total"], "SOLD", None, prefix)
        return
    if not ENTRY_FROM <= s["elapsed"] < ENTRY_TO:
        return
    side = "UP" if s["gap_up"] >= .5 else "DOWN"
    if (s["market_up"] >= .5) != (side == "UP") or (s["speed_up"] >= .5) != (side == "UP"):
        return
    balance = db.execute(f"SELECT balance FROM {prefix}_account WHERE id=1").fetchone()
    if balance is None:
        db.execute(f"INSERT INTO {prefix}_account VALUES (1,?,?)", (now, cash))
        balance = cash
    else:
        balance = balance[0]
    if balance < stake:
        return                                       # out of money: stop, never borrow or top up
    fill, _ = paper_buy(book, side, stake, **terms)
    if (not fill or fill["total"] > balance or fill["total"] / fill["quantity"] > MAX_PRICE
            or fill["total"] < min_share * stake):
        return                                       # never force a trade: skip thin or costly books
    db.execute(f"""INSERT INTO {prefix}_trades(slug,time,end,side,quantity,cost,status,details)
        VALUES (?,?,?,?,?,?,'OPEN',?)""", (slug, now, end, side, fill["quantity"], fill["total"],
                                           json.dumps(dict(s, fee=fill["fee"]))))
    db.execute(f"UPDATE {prefix}_account SET balance=round(balance-?,10) WHERE id=1", (fill["total"],))


def _close(db, trade, now, proceeds, status, outcome, prefix="selective50"):
    done = db.execute(f"""UPDATE {prefix}_trades SET status=?,closed=?,proceeds=?,pnl=?,outcome=?
        WHERE slug=? AND status='OPEN'""", (status, now, proceeds, round(proceeds - trade["cost"], 10),
                                            outcome, trade["slug"]))
    if done.rowcount == 1:
        db.execute(f"UPDATE {prefix}_account SET balance=round(balance+?,10) WHERE id=1", (proceeds,))


def settle(db, now):
    for prefix in LEDGERS:
        for t in db.execute(f"""SELECT t.*,v.outcome AS verified FROM {prefix}_trades t
            JOIN verified_markets v ON v.slug=t.slug AND v.end=t.end
            WHERE t.status='OPEN' AND v.end<=? AND v.verified_at<=?""", (now, now)).fetchall():
            _close(db, t, now, t["quantity"] if t["side"] == t["verified"] else 0.0, "SETTLED", t["verified"], prefix)
    db.execute("DELETE FROM selective50_ticks WHERE t<?", (now - 3600,))


def summary(db, prefix="selective50"):
    name, cash, stake, _ = LEDGERS[prefix]
    acct = db.execute(f"SELECT * FROM {prefix}_account WHERE id=1").fetchone()
    rows = db.execute(f"SELECT pnl,status FROM {prefix}_trades WHERE status!='OPEN'").fetchall()
    pnl = [r["pnl"] for r in rows]
    return dict(name=name, start=cash, stake=stake,
                balance=acct["balance"] if acct else cash, trades=len(pnl),
                wins=sum(p > 0 for p in pnl), losses=sum(p <= 0 for p in pnl), net=round(sum(pnl), 4),
                early_exits=sum(r["status"] == "SOLD" for r in rows),
                open=db.execute(f"SELECT COUNT(*) FROM {prefix}_trades WHERE status='OPEN'").fetchone()[0])

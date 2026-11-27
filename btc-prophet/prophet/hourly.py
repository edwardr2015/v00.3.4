"""dev16: 1-hour "fade the big move" paper test account.

Research (622 official 1-hour markets, Oct 31-Nov 26): at minute 45, if the UP price moved 20c or
more since minute 36, buying the other side made +17.7% per $1 on the earlier 60% of markets and
+17.0% on the later 40% (142 trades, about a 15% chance this is luck). It is tested here on paper,
live, before anyone trusts it.

$50 account, $3 trades, at most one per hour, never below $0, never topped up. Paper only.
"""
import asyncio
import datetime as dt
import json
import logging
import time

from .execution import paper_buy
from .providers import US_BASE, number, parse_book

log = logging.getLogger(__name__)
RULE = "hourly_fade_45"
STARTING_CASH, STAKE, MOVE = 50.0, 3.0, .20
SNAP_FROM, SNAP_TO = 2160, 2340      # minute 36-39: remember the UP price
ENTER_FROM, ENTER_TO = 2700, 2880    # minute 45-48: fade a 20c+ move


def slug_for(start):
    return "cpc-btc-updown-1h-" + dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime("%Y-%m-%d-%H%Mz")


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS hourly_rounds (
            slug TEXT PRIMARY KEY, start REAL NOT NULL, snap_time REAL, snap_up REAL, terms TEXT);
        CREATE TABLE IF NOT EXISTS hourly_trades (
            slug TEXT PRIMARY KEY, time REAL NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
            cost REAL NOT NULL CHECK(cost>0 AND cost<=3), status TEXT NOT NULL CHECK(status IN ('OPEN','SETTLED')),
            outcome TEXT, pnl REAL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hourly_account (id INTEGER PRIMARY KEY CHECK(id=1), started REAL NOT NULL,
            balance REAL NOT NULL CHECK(balance>=0));""")


def decide(db, slug, start, now, book, terms):
    """Pure decision step for one poll of the current hour's book (testable without network)."""
    if not book.get("bids") or not book.get("asks"):
        return None
    up_ask = book["asks"][0]["price"]
    elapsed = now - start
    db.execute("INSERT OR IGNORE INTO hourly_rounds(slug,start,terms) VALUES (?,?,?)", (slug, start, json.dumps(terms)))
    r = db.execute("SELECT * FROM hourly_rounds WHERE slug=?", (slug,)).fetchone()
    if SNAP_FROM <= elapsed <= SNAP_TO and r["snap_up"] is None:
        db.execute("UPDATE hourly_rounds SET snap_time=?, snap_up=? WHERE slug=?", (now, up_ask, slug))
        return "SNAPSHOT"
    if not ENTER_FROM <= elapsed <= ENTER_TO or r["snap_up"] is None:
        return None
    if db.execute("SELECT 1 FROM hourly_trades WHERE slug=?", (slug,)).fetchone():
        return None
    move = up_ask - r["snap_up"]
    if abs(move) < MOVE:
        return None
    side = "DOWN" if move > 0 else "UP"
    acct = db.execute("SELECT balance FROM hourly_account WHERE id=1").fetchone()
    if acct is None:
        db.execute("INSERT INTO hourly_account VALUES (1,?,?)", (now, STARTING_CASH))
        balance = STARTING_CASH
    else:
        balance = acct[0]
    if balance < STAKE:
        return None
    fill, why = paper_buy(book, side, STAKE, **terms)
    if not fill or fill["total"] > balance:
        return None
    db.execute("INSERT INTO hourly_trades VALUES (?,?,?,?,?,'OPEN',NULL,NULL,?)",
               (slug, now, side, fill["quantity"], fill["total"],
                json.dumps(dict(snap_up=r["snap_up"], up_ask=up_ask, move=move, elapsed=elapsed,
                                average_price=fill["average_price"], fee=fill["fee"]))))
    db.execute("UPDATE hourly_account SET balance=round(balance-?,10) WHERE id=1", (fill["total"],))
    return side


def settle_one(db, slug, settlement):
    t = db.execute("SELECT * FROM hourly_trades WHERE slug=? AND status='OPEN'", (slug,)).fetchone()
    if not t or settlement not in (0, 1):
        return
    outcome = "UP" if settlement == 1 else "DOWN"
    payout = t["quantity"] if outcome == t["side"] else 0.0
    db.execute("UPDATE hourly_trades SET status='SETTLED', outcome=?, pnl=? WHERE slug=?",
               (outcome, round(payout - t["cost"], 6), slug))
    db.execute("UPDATE hourly_account SET balance=round(balance+?,10) WHERE id=1", (payout,))


def accounts(db):
    acct = db.execute("SELECT * FROM hourly_account WHERE id=1").fetchone()
    pnl = [r[0] for r in db.execute("SELECT pnl FROM hourly_trades WHERE status='SETTLED'")]
    return [dict(rule=RULE, name="1-hour: fade a big move", describe="Minute 45 · UP moved 20¢+ since minute 36 → buy the other side · holds",
                 start=STARTING_CASH, stake=STAKE, balance=acct["balance"] if acct else STARTING_CASH,
                 trades=len(pnl), wins=sum(p > 0 for p in pnl), losses=sum(p <= 0 for p in pnl), net=round(sum(pnl), 4),
                 open=db.execute("SELECT count(*) FROM hourly_trades WHERE status='OPEN'").fetchone()[0])]


class Hourly:
    def __init__(self, store, collector):
        self.store, self.collector, self.task = store, collector, None
        with store.connect() as db:
            initialize(db)

    async def step(self, now=None):
        api = getattr(self.collector, "api", None)
        if api is None:
            return
        now = time.time() if now is None else now
        start = int(now // 3600 * 3600)
        slug = slug_for(start)
        elapsed = now - start
        if SNAP_FROM <= elapsed <= SNAP_TO or ENTER_FROM <= elapsed <= ENTER_TO:
            market = (await api.get(US_BASE, "/v1/markets", slug=slug)).get("markets") or []
            if market:
                m = market[0]
                terms = dict(theta=max(.095, number(m.get("feeCoefficient", .11), True)),
                             tick=number(m["orderPriceMinTickSize"], True), quantum=number(m["minimumTradeQty"], True))
                book = parse_book(await api.get(US_BASE, "/v1/markets/" + slug + "/book", limit=20), slug)
                if book.get("state") == "MARKET_STATE_OPEN":
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        decide(db, slug, start, now, book, terms)
        with self.store.connect() as db:
            open_ = [r[0] for r in db.execute("SELECT slug FROM hourly_trades WHERE status='OPEN'")]
        for s in open_:
            if s == slug:
                continue
            result = await api.get(US_BASE, "/v1/markets/" + s + "/settlement")
            with self.store.connect() as db:
                settle_one(db, s, result.get("settlement"))

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception:  # noqa: BLE001 - research must never stop the bot
                log.warning("1-hour test account step failed", exc_info=True)
            await asyncio.sleep(30)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

"""Version 2.1 live trader: real orders on Polymarket US, following the Selective rule.

Owner's rules (built in, not settings):
  * Bankroll $50 that the bot tracks itself; $3 per trade; it never adds money and never spends
    below $0. It also checks the exchange's buying power before every order.
  * One open position at a time, at most 40 trades a day, stops for the day after -$10.
  * Reversal exit, like the Selective rule on paper: when the paper Selective trade is sold because
    the model flipped sides, the live position is sold too (once, at no worse than one tick below
    the bid). Otherwise it is held to the end.
  * An order is sent ONCE. The SDK has no idempotency key, so an error is never retried: the trader
    switches itself OFF and waits for the owner.
  * Modes: OFF (default) -> PREVIEW (asks the exchange to check each order, sends nothing) -> LIVE.
    The mode lives in data/timing/live-mode and can only be changed from this computer.
  * Keys live in data/timing/live-keys.json (permission 600), written by scripts/setup_live.py.
    They are never printed, logged or sent anywhere except Polymarket.
"""
import asyncio
import datetime as dt
import json
import logging
import math
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

BANKROLL, STAKE, MAX_PRICE = 50.0, 3.0, .90
MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT = 40, 10.0
MIN_SECONDS_LEFT, MAX_BOOK_AGE = 30, 3.0
FEE_RATE = .11


def folder(store):
    return Path(store.path).parent


def mode(store):
    try:
        value = (folder(store) / "live-mode").read_text().strip().upper()
    except OSError:
        return "OFF"
    return value if value in ("OFF", "PREVIEW", "LIVE") else "OFF"


def set_mode(store, value, reason):
    value = value.upper()
    if value not in ("OFF", "PREVIEW", "LIVE"):
        raise ValueError("Use off, preview or live")
    if value != "OFF" and not keys(store):
        raise ValueError("Run the key setup first (scripts/setup_live.py)")
    if value == "LIVE" and convention(store) is None:
        raise ValueError("Run the test order step first so the DOWN price convention is confirmed")
    path = folder(store) / "live-mode"
    path.write_text(value)
    os.chmod(path, 0o600)
    with store.connect() as db:
        initialize(db)
        db.execute("INSERT INTO live_events(time,kind,detail) VALUES (?,?,?)", (time.time(), "MODE", f"{value}: {reason}"))
    return value


def keys(store):
    try:
        data = json.loads((folder(store) / "live-keys.json").read_text())
        return data if data.get("key_id") and data.get("secret_key") else None
    except (OSError, ValueError):
        return None


def convention(store):
    """How the exchange reads the price of a DOWN (short) order: 'down' or 'up'. Set by the test step."""
    try:
        value = json.loads((folder(store) / "live-convention.json").read_text()).get("short_price")
    except (OSError, ValueError):
        return None
    return value if value in ("down", "up") else None


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS live_orders (
            slug TEXT PRIMARY KEY, time REAL NOT NULL, mode TEXT NOT NULL, side TEXT NOT NULL,
            limit_price REAL NOT NULL, quantity REAL NOT NULL, request TEXT NOT NULL, response TEXT,
            state TEXT NOT NULL, filled REAL, avg_price REAL, cost REAL, fees REAL, outcome TEXT, pnl REAL,
            end REAL NOT NULL, exit_state TEXT, exit_request TEXT, exit_response TEXT, exit_filled REAL,
            exit_proceeds REAL, exit_fees REAL);
        CREATE TABLE IF NOT EXISTS live_events (id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
            detail TEXT NOT NULL);""")


def _day(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


def ledger(db, now=None):
    now = time.time() if now is None else now
    rows = db.execute("SELECT * FROM live_orders WHERE mode='LIVE'").fetchall()
    settled = [r for r in rows if r["pnl"] is not None]
    open_cost = sum((r["cost"] or 0) + (r["fees"] or 0) for r in rows if r["pnl"] is None and (r["filled"] or 0) > 0)
    pnl = sum(r["pnl"] for r in settled)
    today = [r for r in rows if _day(r["time"]) == _day(now) and (r["filled"] or 0) > 0]
    return dict(cash=BANKROLL + pnl - open_cost, pnl=pnl, trades=len(settled),
                wins=sum(r["pnl"] > 0 for r in settled), losses=sum(r["pnl"] <= 0 for r in settled),
                open=sum(1 for r in rows if r["pnl"] is None and (r["filled"] or 0) > 0),
                today_trades=len(today), today_pnl=sum(r["pnl"] or 0 for r in today))


def plan(db, store_book, slug, side, now, end):
    """The exact order the trader would send, or (None, reason). Pure, testable."""
    book = store_book or {}
    if book.get("slug") != slug or not book.get("bids") or not book.get("asks"):
        return None, "NO_BOOK"
    if end - now < MIN_SECONDS_LEFT:
        return None, "TOO_LATE"
    up_bid, up_ask = book["bids"][0]["price"], book["asks"][0]["price"]
    price = up_ask if side == "UP" else round(1 - up_bid, 4)
    limit = round(min(MAX_PRICE, price + .01), 2)          # at most one tick worse than the quote
    if price > MAX_PRICE:
        return None, "PRICE_ABOVE_90C"
    qty = math.floor(STAKE / (limit * (1 + FEE_RATE * (1 - limit))) * 100) / 100
    if qty <= 0:
        return None, "TOO_SMALL"
    return dict(side=side, price=price, limit=limit, quantity=qty,
                max_cost=round(qty * limit * (1 + FEE_RATE * (1 - limit)), 4)), None


def request_for(slug, order, short_convention):
    intent = "ORDER_INTENT_BUY_LONG" if order["side"] == "UP" else "ORDER_INTENT_BUY_SHORT"
    price = order["limit"] if order["side"] == "UP" or short_convention == "down" else round(1 - order["limit"], 2)
    return dict(marketSlug=slug, intent=intent, type="ORDER_TYPE_LIMIT",
                price=dict(value=f"{price:.2f}", currency="USD"), quantity=order["quantity"],
                tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", manualOrderIndicator="MANUAL_ORDER_INDICATOR_AUTOMATIC",
                synchronousExecution=True)


def sell_plan(store_book, slug, side, quantity):
    """Exit order for the shares we own: limit one tick below the current bid for that side."""
    book = store_book or {}
    if book.get("slug") != slug or not book.get("bids") or not book.get("asks"):
        return None, "NO_BOOK"
    up_bid, up_ask = book["bids"][0]["price"], book["asks"][0]["price"]
    bid = up_bid if side == "UP" else round(1 - up_ask, 4)
    limit = round(max(.01, bid - .01), 2)
    return dict(side=side, price=bid, limit=limit, quantity=quantity), None


def sell_request_for(slug, order, short_convention):
    intent = "ORDER_INTENT_SELL_LONG" if order["side"] == "UP" else "ORDER_INTENT_SELL_SHORT"
    price = order["limit"] if order["side"] == "UP" or short_convention == "down" else round(1 - order["limit"], 2)
    return dict(marketSlug=slug, intent=intent, type="ORDER_TYPE_LIMIT",
                price=dict(value=f"{price:.2f}", currency="USD"), quantity=order["quantity"],
                tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", manualOrderIndicator="MANUAL_ORDER_INDICATOR_AUTOMATIC",
                synchronousExecution=True)


def _num(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_fill(response):
    """Filled quantity, average price and fees from a create-order response."""
    filled = cost = fees = 0.0
    for e in (response or {}).get("executions", []) or []:
        q, p = _num(e.get("lastShares")), _num(e.get("lastPx"))
        if q and p and e.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
            filled += q
            cost += q * p
            fees += _num(e.get("commissionNotionalCollected")) or 0.0
    return filled, (cost / filled if filled else None), cost, fees


class LiveTrader:
    def __init__(self, store):
        self.store, self.task, self.client = store, None, None
        with store.connect() as db:
            initialize(db)

    def _client(self):
        if self.client is None:
            from polymarket_us import PolymarketUS
            k = keys(self.store)
            self.client = PolymarketUS(key_id=k["key_id"], secret_key=k["secret_key"], max_retries=0)
        return self.client

    def _event(self, kind, detail):
        with self.store.connect() as db:
            db.execute("INSERT INTO live_events(time,kind,detail) VALUES (?,?,?)", (time.time(), kind, detail[:500]))

    def _stop(self, why):
        set_mode(self.store, "OFF", why)
        self._event("STOPPED", why)
        log.warning("Live trader stopped: %s", why)

    async def step(self, now=None):
        current = mode(self.store)
        now = time.time() if now is None else now
        with self.store.connect() as db:
            self._settle(db)
            if current == "OFF":
                return
            book = json.loads(db.execute("SELECT payload FROM state WHERE key='book'").fetchone()[0]) \
                if db.execute("SELECT 1 FROM state WHERE key='book'").fetchone() else None
            checked = db.execute("SELECT payload FROM state WHERE key='health_market'").fetchone()
            checked = json.loads(checked[0]).get("checked") if checked else None
            exiting = db.execute("""SELECT o.* FROM live_orders o JOIN selective50_trades s ON s.slug=o.slug
                WHERE s.status='SOLD' AND o.filled>0 AND o.pnl IS NULL AND o.exit_state IS NULL
                AND o.mode='LIVE' LIMIT 1""").fetchone()
        if exiting and current == "LIVE":
            await self._exit(exiting, book)
            return
        with self.store.connect() as db:
            new = db.execute("""SELECT s.slug, s.side, s.end FROM selective50_trades s
                LEFT JOIN live_orders o ON o.slug=s.slug WHERE s.status='OPEN' AND o.slug IS NULL
                ORDER BY s.time DESC LIMIT 1""").fetchone()
            if not new:
                return
            l = ledger(db, now)
            reasons = []
            if not checked or now - checked > MAX_BOOK_AGE:
                reasons.append("STALE_BOOK")
            if l["open"] and current == "LIVE":
                reasons.append("POSITION_OPEN")
            if l["cash"] < STAKE + .2:
                reasons.append("BANKROLL_USED_UP")
            if l["today_trades"] >= MAX_TRADES_PER_DAY:
                reasons.append("DAILY_TRADE_LIMIT")
            if l["today_pnl"] <= -DAILY_LOSS_LIMIT:
                reasons.append("DAILY_LOSS_LIMIT")
            order, why = plan(db, book, new["slug"], new["side"], now, new["end"])
            if why:
                reasons.append(why)
            if reasons:
                db.execute("INSERT OR IGNORE INTO live_orders(slug,time,mode,side,limit_price,quantity,request,state,end) "
                           "VALUES (?,?,?,?,0,0,'{}',?,?)", (new["slug"], now, current, new["side"], "SKIPPED:" + "/".join(reasons), new["end"]))
                return
            request = request_for(new["slug"], order, convention(self.store) or "down")
            db.execute("INSERT INTO live_orders(slug,time,mode,side,limit_price,quantity,request,state,end) VALUES (?,?,?,?,?,?,?,?,?)",
                       (new["slug"], now, current, order["side"], order["limit"], order["quantity"], json.dumps(request), "SENDING", new["end"]))
        try:
            client = self._client()
            if current == "PREVIEW":
                response = await asyncio.to_thread(client.orders.preview, dict(request=request))
                state = "PREVIEWED"
            else:
                balances = await asyncio.to_thread(client.account.balances)
                power = min((_num(b.get("buyingPower")) or 0) for b in balances.get("balances", [{}])) if balances.get("balances") else 0
                if power < order["max_cost"]:
                    with self.store.connect() as db:
                        db.execute("UPDATE live_orders SET state=? WHERE slug=?", ("SKIPPED:EXCHANGE_BUYING_POWER", new["slug"]))
                    return
                response = await asyncio.to_thread(client.orders.create, request)   # sent ONCE, never retried
                state = "SENT"
        except Exception as exc:  # noqa: BLE001
            with self.store.connect() as db:
                db.execute("UPDATE live_orders SET state=?, response=? WHERE slug=?",
                           ("ERROR", json.dumps(dict(error=type(exc).__name__, message=str(exc)[:300])), new["slug"]))
            if current == "LIVE":
                self._stop(f"order error on {new['slug']}: {type(exc).__name__}. Not retried. Check positions on Polymarket.")
            else:
                self._event("PREVIEW_ERROR", f"{new['slug']}: {type(exc).__name__}: {str(exc)[:200]}")
            return
        filled, avg, cost, fees = read_fill(response) if state == "SENT" else (0.0, None, 0.0, 0.0)
        with self.store.connect() as db:
            db.execute("UPDATE live_orders SET state=?, response=?, filled=?, avg_price=?, cost=?, fees=? WHERE slug=?",
                       ("FILLED" if filled else state if state == "PREVIEWED" else "NOT_FILLED", json.dumps(response)[:4000],
                        filled, avg, cost, fees, new["slug"]))
            if state == "SENT" and cost + fees > STAKE * 1.1:
                self._stop(f"fill cost ${cost + fees:.2f} above the $3 limit on {new['slug']}")

    async def _exit(self, order, book):
        """Reversal exit: sell the shares we own, once. Never retried."""
        plan_, why = sell_plan(book, order["slug"], order["side"], order["filled"])
        if why:
            with self.store.connect() as db:
                db.execute("UPDATE live_orders SET exit_state=? WHERE slug=?", ("SKIPPED:" + why, order["slug"]))
            return
        request = sell_request_for(order["slug"], plan_, convention(self.store) or "down")
        with self.store.connect() as db:
            db.execute("UPDATE live_orders SET exit_state='SENDING', exit_request=? WHERE slug=?", (json.dumps(request), order["slug"]))
        try:
            response = await asyncio.to_thread(self._client().orders.create, request)   # sent ONCE
        except Exception as exc:  # noqa: BLE001
            with self.store.connect() as db:
                db.execute("UPDATE live_orders SET exit_state='ERROR', exit_response=? WHERE slug=?",
                           (json.dumps(dict(error=type(exc).__name__, message=str(exc)[:300])), order["slug"]))
            self._stop(f"exit order error on {order['slug']}: {type(exc).__name__}. Not retried. The position may still be open.")
            return
        sold, avg, proceeds, fees = read_fill(response)
        with self.store.connect() as db:
            db.execute("""UPDATE live_orders SET exit_state=?, exit_response=?, exit_filled=?, exit_proceeds=?, exit_fees=?
                WHERE slug=?""", ("SOLD" if sold >= order["filled"] - 1e-9 else "PARTIAL" if sold else "NOT_FILLED",
                                  json.dumps(response)[:4000], sold, proceeds, fees, order["slug"]))

    def _settle(self, db):
        for r in db.execute("""SELECT o.*, v.outcome AS verified FROM live_orders o JOIN verified_markets v
                ON v.slug=o.slug WHERE o.pnl IS NULL AND o.filled>0""").fetchall():
            held = r["filled"] - (r["exit_filled"] or 0)          # shares still owned at the end
            payout = held if r["side"] == r["verified"] else 0.0
            pnl = payout + (r["exit_proceeds"] or 0) - (r["exit_fees"] or 0) - r["cost"] - r["fees"]
            db.execute("UPDATE live_orders SET outcome=?, pnl=?, state='SETTLED' WHERE slug=?",
                       (r["verified"], round(pnl, 6), r["slug"]))

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception:  # noqa: BLE001
                log.warning("Live trader step failed", exc_info=True)
            await asyncio.sleep(1)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


def status(store):
    with store.connect() as db:
        initialize(db)
        l = ledger(db)
        recent = [dict(r) for r in db.execute(
            "SELECT slug,time,mode,side,limit_price,quantity,state,filled,avg_price,cost,fees,exit_state,exit_filled,exit_proceeds,outcome,pnl FROM live_orders ORDER BY time DESC LIMIT 20")]
        events = [dict(r) for r in db.execute("SELECT time,kind,detail FROM live_events ORDER BY id DESC LIMIT 10")]
    return dict(mode=mode(store), keys=bool(keys(store)), convention=convention(store), bankroll=BANKROLL,
                stake=STAKE, **l, recent=recent, events=events)

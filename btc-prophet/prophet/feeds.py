"""dev17 feed referee: live BTC/USD from the exchanges inside BRTI, checked against each other and
against Polymarket's official settlement values.

Each second:
  1. collect   Coinbase, Kraken, Bitstamp, Gemini WebSockets push best bid/ask (mid-price, USD only).
  2. read      one parser per exchange turns each message into (bid, ask, exchange_time).
  3. check     a feed is healthy only if: connected, updated in the last 2 s, not frozen 10 s while
               others move, within 0.3% of the median, and its exchange clock within 2 s of ours.
  4. combine   consensus = median mid of healthy feeds (needs 2+). Otherwise: no live price (callers
               fall back to Polymarket's BRTI, flagged slow).
  5. correct   live BRTI estimate = consensus + basis; basis = median(official value - our 60 s average)
               over recent settled rounds (self-calibrating).
  6. score     per feed: error vs official values, latency, uptime, times dropped.
  7. prove     the lag check (best time shift and error vs official) is run on the consensus too; it
               should be 0 s and under $1 if the live price is right.

Paper/research only. It never places orders.
"""
import asyncio
import datetime as dt
import json
import logging
import statistics
import time

log = logging.getLogger(__name__)

STALE_AFTER, FROZEN_AFTER, MAX_DEVIATION, MAX_CLOCK_SKEW, MIN_HEALTHY = 2.0, 10.0, .003, 2.0, 2
FROZEN_MOVE = .0003   # others moved 0.03% (~$18) while this feed's price did not change
BASIS_ROUNDS = 50

FEEDS = {
    "coinbase": ("wss://ws-feed.exchange.coinbase.com",
                 {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}),
    "kraken": ("wss://ws.kraken.com/v2",
               {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"], "event_trigger": "bbo"}}),
    "bitstamp": ("wss://ws.bitstamp.net", {"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}}),
    "gemini": ("wss://api.gemini.com/v1/marketdata/BTCUSD?top_of_book=true", None),
}


def _iso(text):
    """ISO-8601 UTC text (any fractional precision) -> epoch seconds."""
    t = text.strip().rstrip("Z").replace("+00:00", "")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
    return dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).timestamp()


class Parser:
    """Turns one raw message into (bid, ask, exchange_time) or None. Keeps per-exchange state."""

    def __init__(self, name):
        self.name, self.bid, self.ask = name, None, None

    def __call__(self, raw):
        msg = json.loads(raw)
        n = self.name
        if n == "coinbase":
            if msg.get("type") != "ticker" or msg.get("product_id") != "BTC-USD":
                return None
            return float(msg["best_bid"]), float(msg["best_ask"]), _iso(msg["time"])
        if n == "kraken":
            if msg.get("channel") != "ticker" or msg.get("type") not in ("snapshot", "update"):
                return None
            d = msg["data"][0]
            if d.get("symbol") != "BTC/USD":
                return None
            return float(d["bid"]), float(d["ask"]), _iso(d["timestamp"]) if d.get("timestamp") else None
        if n == "bitstamp":
            if msg.get("event") != "data" or msg.get("channel") != "order_book_btcusd":
                return None
            d = msg["data"]
            if not d.get("bids") or not d.get("asks"):
                return None
            return float(d["bids"][0][0]), float(d["asks"][0][0]), int(d["microtimestamp"]) / 1e6
        if n == "gemini":
            if msg.get("type") != "update":
                return None
            for e in msg.get("events", []):
                if e.get("type") == "change" and float(e.get("remaining", 0)) > 0:
                    if e["side"] == "bid":
                        self.bid = float(e["price"])
                    elif e["side"] == "ask":
                        self.ask = float(e["price"])
            if self.bid is None or self.ask is None or self.bid >= self.ask:
                return None
            ts = msg.get("timestampms")
            return self.bid, self.ask, (ts / 1000 if ts else None)
        return None


class FeedState:
    def __init__(self, name):
        self.name = name
        self.connected = False
        self.bid = self.ask = self.mid = None
        self.exchange_time = self.received = self.changed = None
        self.messages = self.drops = self.reconnects = 0
        self.healthy_seconds = self.checked_seconds = 0
        self.reason = "NOT_CONNECTED"
        self.latency = None
        self.alive = None              # last message of any kind (prices, heartbeats)
        self.mark_time = None          # when the referee last saw this feed's price change
        self.median_at_change = None   # the market median at that moment

    def update(self, bid, ask, exchange_time, now):
        mid = (bid + ask) / 2
        if mid != self.mid:
            self.changed = now
        self.bid, self.ask, self.mid, self.exchange_time, self.received = bid, ask, mid, exchange_time, now
        if exchange_time:
            self.latency = now - exchange_time
        self.messages += 1


def _seen(s):
    """Last sign of life: a price update or any other message (exchanges stay quiet when nothing changes)."""
    return max(s.received or 0, s.alive or 0) or None


def referee(states, now):
    """Health-check every feed and return (consensus, healthy names)."""
    live = {n: s for n, s in states.items() if s.connected and s.mid and _seen(s) and now - _seen(s) <= STALE_AFTER}
    median = statistics.median(s.mid for s in live.values()) if live else None
    healthy = []
    for n, s in states.items():
        s.checked_seconds += 1
        if s.changed and s.changed != s.mark_time:
            s.mark_time, s.median_at_change = s.changed, median
        others_moved = (median and s.median_at_change and abs(median / s.median_at_change - 1) > FROZEN_MOVE)
        if not s.connected:
            reason = "NOT_CONNECTED"
        elif not s.mid or not _seen(s) or now - _seen(s) > STALE_AFTER:
            reason = "STALE"
        elif s.changed and now - s.changed > FROZEN_AFTER and others_moved:
            reason = "FROZEN"
        elif median and abs(s.mid / median - 1) > MAX_DEVIATION:
            reason = "OUTLIER"
        elif s.latency is not None and abs(s.latency) > MAX_CLOCK_SKEW:
            reason = "CLOCK_SKEW"
        else:
            reason = "OK"
        if reason != "OK" and s.reason == "OK":
            s.drops += 1
        s.reason = reason
        if reason == "OK":
            s.healthy_seconds += 1
            healthy.append(n)
    consensus = statistics.median(states[n].mid for n in healthy) if len(healthy) >= MIN_HEALTHY else None
    return consensus, healthy


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS feed_ticks (
            time INTEGER PRIMARY KEY, consensus REAL, healthy INTEGER NOT NULL, feeds TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feed_round_checks (
            slug TEXT PRIMARY KEY, end REAL NOT NULL, official_open REAL, official_close REAL,
            consensus_open REAL, consensus_close REAL, best_shift INTEGER, error_at_zero REAL,
            per_feed TEXT);""")


def _avg(rows, start, stop, key="consensus"):
    vals = [r[key] for t, r in rows.items() if start <= t < stop and r.get(key)]
    return (sum(vals) / len(vals)) if len(vals) >= 50 else None


def basis(db):
    """Median gap between official values and our consensus 60 s averages (recent rounds)."""
    gaps = []
    for r in db.execute("""SELECT official_open, official_close, consensus_open, consensus_close
            FROM feed_round_checks WHERE consensus_open IS NOT NULL ORDER BY end DESC LIMIT ?""", (BASIS_ROUNDS,)):
        gaps += [r[0] - r[2], r[1] - r[3]]
    return statistics.median(gaps) if len(gaps) >= 6 else 0.0


def live_brti(db, now, max_age=2):
    """Live BRTI estimate (consensus + basis) at `now`, or None if the feeds are not healthy."""
    row = db.execute("SELECT consensus FROM feed_ticks WHERE time<=? AND time>=? AND consensus IS NOT NULL "
                     "ORDER BY time DESC LIMIT 1", (int(now), int(now - max_age))).fetchone()
    return (row[0] + basis(db)) if row else None


def check_rounds(db, limit=20):
    """Compare consensus (and each feed) 60 s averages with official values; find the best time shift."""
    for m in db.execute("""SELECT v.slug, v.start, v.end, v.opening, v.closing FROM verified_markets v
            LEFT JOIN feed_round_checks c ON c.slug=v.slug WHERE c.slug IS NULL ORDER BY v.end DESC LIMIT ?""",
                        (limit,)).fetchall():
        rows = {}
        for t, consensus, feeds in db.execute("SELECT time, consensus, feeds FROM feed_ticks WHERE time>=? AND time<?",
                                              (m["start"] - 130, m["end"] + 70)):
            rows[t] = dict(json.loads(feeds), consensus=consensus)
        co, cc = _avg(rows, m["start"] - 60, m["start"]), _avg(rows, m["end"] - 60, m["end"])
        if co is None or cc is None:
            if m["end"] < time.time() - 600:
                db.execute("INSERT OR IGNORE INTO feed_round_checks(slug,end) VALUES (?,?)", (m["slug"], m["end"]))
            continue
        b = basis(db)
        errors = {}
        for shift in range(-60, 61, 2):
            o, c = _avg(rows, m["start"] - 60 + shift, m["start"] + shift), _avg(rows, m["end"] - 60 + shift, m["end"] + shift)
            if o and c:
                # Compare the MOVE (close - open), so a steady price offset cannot fake a time shift.
                errors[shift] = abs((c - o) - (m["closing"] - m["opening"]))
        per = {}
        for name in FEEDS:
            o, c = _avg(rows, m["start"] - 60, m["start"], name), _avg(rows, m["end"] - 60, m["end"], name)
            if o and c:
                per[name] = dict(open_gap=m["opening"] - o, close_gap=m["closing"] - c)
        level_error = (abs(co + b - m["opening"]) + abs(cc + b - m["closing"])) / 2
        db.execute("INSERT OR REPLACE INTO feed_round_checks VALUES (?,?,?,?,?,?,?,?,?)",
                   (m["slug"], m["end"], m["opening"], m["closing"], co, cc,
                    min(errors, key=errors.get) if errors else None, level_error, json.dumps(per)))


def summary(db, states=None):
    rows = db.execute("SELECT best_shift, error_at_zero, per_feed FROM feed_round_checks WHERE best_shift IS NOT NULL "
                      "ORDER BY end DESC LIMIT 200").fetchall()
    shifts = sorted(r[0] for r in rows)
    errs = sorted(r[1] for r in rows if r[1] is not None)
    per = {}
    for r in rows:
        for name, g in json.loads(r[2] or "{}").items():
            per.setdefault(name, []).append((g["open_gap"] + g["close_gap"]) / 2)
    b = basis(db)
    feeds = {}
    for name in FEEDS:
        s = (states or {}).get(name)
        gaps = per.get(name, [])
        spread = statistics.pstdev(gaps) if len(gaps) >= 3 else None
        feeds[name] = dict(
            status=s.reason if s else "UNKNOWN", mid=s.mid if s else None,
            age=(time.time() - s.received) if s and s.received else None,
            latency=s.latency if s else None, uptime=(s.healthy_seconds / s.checked_seconds) if s and s.checked_seconds else None,
            drops=s.drops if s else None, reconnects=s.reconnects if s else None,
            error_vs_official=spread, rounds=len(gaps))
    last = db.execute("SELECT time, consensus, healthy FROM feed_ticks ORDER BY time DESC LIMIT 1").fetchone()
    return dict(rounds=len(rows), typical_shift=shifts[len(shifts) // 2] if shifts else None,
                typical_error=errs[len(errs) // 2] if errs else None, basis=b,
                consensus=last[1] if last else None, healthy=last[2] if last else 0,
                live_brti=(last[1] + b) if last and last[1] else None, feeds=feeds)


class Referee:
    def __init__(self, store):
        self.store = store
        self.states = {n: FeedState(n) for n in FEEDS}
        self.tasks = []
        with store.connect() as db:
            initialize(db)

    async def _stream(self, name):
        try:
            import websockets
        except ImportError:
            log.warning("Live feeds off: the websockets package is not installed")
            return
        url, sub = FEEDS[name]
        state, delay = self.states[name], 1
        while True:
            parser = Parser(name)
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=20, max_size=2 ** 22) as ws:
                    if sub:
                        await ws.send(json.dumps(sub))
                    state.connected, delay = True, 1
                    async for raw in ws:
                        state.alive = time.time()
                        try:
                            got = parser(raw)
                        except (ValueError, KeyError, TypeError, IndexError):
                            continue
                        if got:
                            state.update(*got, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect whatever happened
                log.info("%s feed disconnected: %s", name, exc)
            state.connected = False
            state.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    async def _tick(self):
        last_check = 0
        while True:
            now = time.time()
            consensus, healthy = referee(self.states, now)
            feeds = {n: s.mid for n, s in self.states.items() if n in healthy}
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO feed_ticks VALUES (?,?,?,?)",
                           (int(now), consensus, len(healthy), json.dumps(feeds)))
                if now - last_check > 30:
                    check_rounds(db)
                    db.execute("DELETE FROM feed_ticks WHERE time<?", (int(now - 14 * 86400),))
                    last_check = now
            await asyncio.sleep(1 - (time.time() % 1))

    def start(self):
        self.tasks = [asyncio.create_task(self._stream(n)) for n in FEEDS] + [asyncio.create_task(self._tick())]

    async def close(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

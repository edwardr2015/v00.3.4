"""dev17 feed referee: real exchange messages are parsed the same way, unhealthy feeds are dropped,
the consensus needs two healthy feeds, and the basis/lag check calibrate against official values."""
import json
from pathlib import Path

from prophet import feeds, fresh
from prophet.app import create_app
from prophet.store import Store

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "exchange_messages.json").read_text())


def test_every_exchange_parses_real_messages_to_usd_bid_ask_and_time():
    mids = {}
    for name, msgs in SAMPLES.items():
        parser = feeds.Parser(name)
        got = [parser(m) for m in msgs["price"]]
        got = [g for g in got if g]
        assert got, name
        bid, ask, ts = got[-1]
        assert 10000 < bid < ask < 1000000, name          # USD BTC prices, bid below ask
        assert ask - bid < 50, name                        # a real top of book, not a stale level
        if ts is not None:
            assert 1.7e9 < ts < 2.0e9, name                # seconds since 1970, not ms or µs
        mids[name] = (bid + ask) / 2
        for m in msgs["other"]:
            assert parser(m) is None or isinstance(parser(m), tuple)
    spread = max(mids.values()) / min(mids.values()) - 1
    assert spread < .003                                    # all four read the same market (same USD price)


def state(name, mid, now, changed=None, latency=.1, connected=True):
    s = feeds.FeedState(name)
    s.connected = connected
    s.update(mid - .5, mid + .5, now - latency, now)
    s.changed = now if changed is None else changed
    return s


def test_referee_drops_stale_frozen_outlier_and_skewed_feeds():
    now = 1000.0
    states = {"coinbase": state("coinbase", 60000, now), "kraken": state("kraken", 60002, now),
              "bitstamp": state("bitstamp", 60001, now), "gemini": state("gemini", 60500, now)}  # gemini 0.8% off
    consensus, healthy = feeds.referee(states, now)
    assert set(healthy) == {"coinbase", "kraken", "bitstamp"} and states["gemini"].reason == "OUTLIER"
    assert consensus == 60001
    feeds.referee(states, now)                                 # remember each feed's price at its last change
    for n in ("coinbase", "kraken", "gemini"):                 # the market moves $60 ...
        states[n].update(60059.5, 60060.5, now + 19.9, now + 20)
    states["bitstamp"].received = now + 20                     # ... bitstamp keeps sending the same price
    now += 20
    feeds.referee(states, now)
    assert states["bitstamp"].reason == "FROZEN"
    states["kraken"].received = states["kraken"].alive = now - 5   # stale: no message at all for 5 s
    states["gemini"] = state("gemini", 60060, now, latency=5)  # clock 5 s off
    consensus, healthy = feeds.referee(states, now)
    assert states["kraken"].reason == "STALE" and states["gemini"].reason == "CLOCK_SKEW"
    assert healthy == ["coinbase"] and states["bitstamp"].reason == "FROZEN"
    assert consensus is None                                   # fewer than 2 healthy: no live price


def test_feed_that_recovers_rejoins_and_drops_are_counted():
    now = 1000.0
    states = {n: state(n, 60000 + i, now) for i, n in enumerate(feeds.FEEDS)}
    states["coinbase"].connected = False
    feeds.referee(states, now)
    assert states["coinbase"].reason == "NOT_CONNECTED"
    states["coinbase"] = state("coinbase", 60001, now + 1)
    _, healthy = feeds.referee(states, now + 1)
    assert "coinbase" in healthy


import math


def price(t):
    return 60000 + 30 * math.sin(t / 40) + .01 * t


def test_basis_and_lag_check_calibrate_to_official_values(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    with store.connect() as db:
        feeds.initialize(db)
        for k in range(6):                                     # 6 settled rounds, consensus $20 below BRTI
            start = 10000 + 300 * k
            for t in range(start - 130, start + 370):
                db.execute("INSERT OR REPLACE INTO feed_ticks VALUES (?,?,?,?)",
                           (t, price(t), 4, json.dumps({"coinbase": price(t)})))
            o = sum(price(t) for t in range(start - 60, start)) / 60 + 20
            c = sum(price(t) for t in range(start + 240, start + 300)) / 60 + 20
            db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"r{k}", start, start + 300, "UP", o, c, start + 400, "fixture", "{}"))
            feeds.check_rounds(db)
        assert abs(feeds.basis(db) - 20) < 1e-6
        rows = db.execute("SELECT best_shift FROM feed_round_checks").fetchall()
        assert all(r[0] == 0 for r in rows)                    # live consensus lines up at shift 0
        live = feeds.live_brti(db, 10000 + 300 * 5 + 100)
        assert abs(live - (price(10000 + 1600) + 20)) < 1e-6
        assert feeds.live_brti(db, 99999) is None              # no recent tick: no live price
        s = feeds.summary(db)
        assert s["rounds"] == 6 and s["typical_shift"] == 0 and abs(s["basis"] - 20) < 1e-6


def test_lag_check_detects_a_late_feed(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    with store.connect() as db:
        feeds.initialize(db)
        db.executescript("CREATE TABLE IF NOT EXISTS verified_markets (slug TEXT PRIMARY KEY, start REAL, end REAL, outcome TEXT, opening REAL, closing REAL, verified_at REAL, source TEXT, evidence TEXT)")
        start = 20000
        for t in range(start - 130, start + 370):              # our copy runs 30 s late
            db.execute("INSERT INTO feed_ticks VALUES (?,?,?,?)", (t + 30, price(t), 4, "{}"))
        o = sum(price(t) for t in range(start - 60, start)) / 60
        c = sum(price(t) for t in range(start + 240, start + 300)) / 60
        db.execute("INSERT INTO verified_markets VALUES ('late',?,?,'UP',?,?,0,'f','{}')", (start, start + 300, o, c))
        feeds.check_rounds(db)
        assert db.execute("SELECT best_shift FROM feed_round_checks").fetchone()[0] == 30


def test_main_switch_accepts_live_option(tmp_path):
    from fastapi.testclient import TestClient
    store = Store(str(tmp_path / "p.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    with TestClient(app) as c:
        h = {"X-Prophet-Control": "local-dashboard"}
        assert c.post("/api/remote/main-model/settlement_live", headers=h).status_code == 200
        with store.connect() as db:
            assert fresh.main_model(db) == "settlement_live" and fresh.main_uses_settlement(db)
        assert c.get("/api/feeds").json()["feeds"].keys() == feeds.FEEDS.keys()
        c.post("/api/remote/main-model/standard", headers=h)
        with store.connect() as db:
            assert fresh.main_model(db) == "standard"


def test_quiet_market_is_not_frozen():
    now = 1000.0
    states = {n: state(n, 60000 + i, now) for i, n in enumerate(feeds.FEEDS)}
    feeds.referee(states, now)
    for s in states.values():                                  # nobody's price changes for 30 s
        s.received = now + 30
    _, healthy = feeds.referee(states, now + 30)
    assert len(healthy) == 4


def test_heartbeat_keeps_an_unchanged_feed_alive():
    now = 1000.0
    states = {n: state(n, 60000 + i, now) for i, n in enumerate(feeds.FEEDS)}
    states["kraken"].received = now - 8                        # no new price for 8 s ...
    states["kraken"].alive = now - .5                          # ... but a heartbeat half a second ago
    _, healthy = feeds.referee(states, now)
    assert "kraken" in healthy


def test_system_audit_reconciles_and_flags(tmp_path):
    from fastapi.testclient import TestClient
    from prophet import audit
    store = Store(str(tmp_path / "p.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    with TestClient(app) as c:
        a = c.get("/api/audit").json()
    names = {x["name"]: x for x in a["checks"]}
    assert names["Paper engine stays paper"]["status"] == "PASS"
    assert names["No negative balances"]["status"] == "PASS"
    assert names["Main $1,000 reconciles"]["status"] == "PASS"
    with store.connect() as db:                       # break the books on purpose: audit must catch it
        db.execute("UPDATE unified_account SET balance=balance+7 WHERE id=1")
        r = {x["name"]: x for x in audit.run(db)["checks"]}
    assert r["Main $1,000 reconciles"]["status"] == "FAIL" and abs(r["Main $1,000 reconciles"]["numbers"]["gap"] - 7) < 1e-6


def test_profit_check_only_calls_proven_with_100_trades_and_positive_range():
    from prophet import audit
    lo, hi = audit._range([.1] * 50 + [-.05] * 50)
    assert lo > 0
    lo, hi = audit._range([1, -1] * 60)
    assert lo < 0 < hi


def test_last_minute_uses_live_feeds_for_the_locked_part(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    with store.connect() as db:
        feeds.initialize(db)
        start, end, now = 50000, 50300, 50290                 # 10 s left: 50 of 60 closing seconds are locked
        for t in range(end - 60, now + 1):                     # live: price well ABOVE the 60000 opening
            db.execute("INSERT INTO feed_ticks VALUES (?,?,?,?)", (t, 60100.0, 4, "{}"))
            db.execute("INSERT INTO candles VALUES ('BRTI',?,?,?,?,?,1)", (t, 59900.0, 59900, 59900, 59900))  # delayed: below
        p_live, info = fresh.settlement_up(db, now, start, end, 60000.0, 60100.0, .0005, live=True)
        p_old, _ = fresh.settlement_up(db, now, start, end, 60000.0, 60100.0, .0005, live=False)
        assert info["fixed_average"] == 60100.0 and p_live > .95   # live locked part says UP
        assert p_old < p_live                                       # delayed feed would understate it
        db.execute("DELETE FROM feed_ticks WHERE time>?", (end - 30,))   # gap in live data: refuse to guess
        assert fresh.settlement_up(db, now, start, end, 60000.0, 60100.0, .0005, live=True) == (None, None)


def test_main_follows_selective_rule_when_switched_on(tmp_path):
    from prophet import unified
    store = Store(str(tmp_path / "p.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    store.put("main_model", "selective")
    terms = dict(theta=.11, tick=.01, quantum=.01)
    level = dict(bids=[dict(price=.60, quantity=5000)], asks=[dict(price=.62, quantity=5000)])
    def obs(db, now, raw):
        d = dict(window_start=1000, window_end=1300, book_checked=now, reference_timestamp=now,
                 reference_price=100.2, opening_price=100.0, volatility=.0005, book_timestamp=now)
        unified.observe(db, "r", now, "SIMULATE", "fixture", raw, None, d, level, terms, lambda: now)
    with store.connect() as db:
        obs(db, 1100, .8)                               # minute 1: Selective rule does not trade yet
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0] == 0
        for t in range(1180, 1200, 6):                  # minute 3: build price history, then all 3 agree on UP
            obs(db, t, .8)
        sel = db.execute("SELECT side FROM selective50_trades WHERE slug='r'").fetchone()
        main = db.execute("SELECT side, cost FROM unified_trades WHERE slug='r'").fetchone()
        assert sel is not None and main is not None     # main trades exactly when the Selective rule does
        assert main["side"] == sel["side"] == "UP" and main["cost"] <= 5
        obs(db, 1215, .3)                               # model flips: Selective rule's reversal exit ...
        assert db.execute("SELECT status FROM selective50_trades WHERE slug='r'").fetchone()[0] == "SOLD"
        assert db.execute("SELECT status FROM unified_trades WHERE slug='r'").fetchone()[0] == "SOLD"   # ... main follows

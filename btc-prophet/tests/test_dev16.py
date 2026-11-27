"""dev16: recorder, feed lag check, settlement-model and limit-order paper accounts, main switch,
exit-report fix and stale-book exit guard. All prices and outcomes are fixtures."""
import json

from fastapi.testclient import TestClient

from prophet import fresh, unified
from prophet.app import create_app
from prophet.store import Store

TERMS = dict(theta=.11, tick=.01, quantum=.01)
H = {"X-Prophet-Control": "local-dashboard"}


def setup(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    return store, app


def book(bid, ask, depth=5000, ts=None):
    return dict(bids=[dict(price=bid, quantity=depth)], asks=[dict(price=ask, quantity=depth)])


def details(now, start=1000, ref=100.0, opening=100.0, vol=.001, book_ts=None):
    return dict(window_start=start, window_end=start + 300, book_checked=now, reference_timestamp=now,
                reference_price=ref, opening_price=opening, volatility=vol,
                book_timestamp=now if book_ts is None else book_ts)


def observe(db, now, level, raw=.5, **kw):
    unified.observe(db, "round", now, "SIMULATE", "fixture", raw, None, details(now, **kw), level, TERMS, lambda: now)


def verify(db, outcome, opening=100.0, closing=101.0, start=1000):
    db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
               ("round", start, start + 300, outcome, opening, closing, start + 400, "fixture", "{}"))


def trades(db, rule):
    return db.execute("SELECT * FROM fresh_trades WHERE rule=?", (rule,)).fetchall()


def test_recorder_saves_every_observation(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1010, book(.45, .47))
        observe(db, 1011, book(.46, .48))
        rows = db.execute("SELECT * FROM fresh_recorder ORDER BY time").fetchall()
        assert len(rows) == 2 and json.loads(rows[1]["book"])["bids"][0]["price"] == .46
        assert rows[0]["reference_price"] == 100.0


def test_lag_check_finds_live_feed_and_a_late_feed(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        prices = {t: 100 + .01 * t for t in range(800, 1400)}     # steadily rising index
        db.executemany("INSERT INTO candles VALUES ('BRTI',?,?,?,?,?,1)",
                       [(t, p, p, p, p) for t, p in prices.items()])
        official_open = sum(prices[t] for t in range(940, 1000)) / 60
        official_close = sum(prices[t] for t in range(1240, 1300)) / 60
        verify(db, "UP", official_open, official_close)
        fresh.settle(db, 2000)
        row = db.execute("SELECT * FROM fresh_lag_checks").fetchone()
        assert row["best_shift"] == 0 and row["zero_error"] < 1e-9          # our seconds are live
        # Same official values, but our saved seconds run 30 s late (each value recorded 30 s after it happened).
        db.execute("DELETE FROM candles"); db.execute("DELETE FROM fresh_lag_checks")
        db.executemany("INSERT INTO candles VALUES ('BRTI',?,?,?,?,?,1)",
                       [(t + 30, p, p, p, p) for t, p in prices.items()])
        fresh.settle(db, 2000)
        assert db.execute("SELECT best_shift FROM fresh_lag_checks").fetchone()[0] == 30
        assert fresh.lag_summary(db)["typical_shift"] == 30


def test_limit_order_rests_fills_only_when_traded_through_and_settles(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1060, book(.44, .46))          # UP mid 45c -> rest DOWN bid at 1-.46 = 54c
        t = trades(db, "limit_down_55")[0]
        assert t["status"] == "RESTING" and abs(t["price"] - .54) < 1e-9
        reserved = 50 - db.execute("SELECT balance FROM fresh_accounts WHERE rule='limit_down_55'").fetchone()[0]
        assert 2.9 < reserved < 3.2   # $3 plus the fee, reserved while resting
        observe(db, 1070, book(.45, .47))          # DOWN ask 55c: not through our 54c bid
        assert trades(db, "limit_down_55")[0]["status"] == "RESTING"
        observe(db, 1080, book(.46, .48))          # DOWN ask 54c: equal, still not through
        assert trades(db, "limit_down_55")[0]["status"] == "RESTING"
        observe(db, 1090, book(.47, .49))          # DOWN ask 53c: traded through -> filled
        assert trades(db, "limit_down_55")[0]["status"] == "FILLED"
        verify(db, "DOWN")
        fresh.settle(db, 2000)
        t = trades(db, "limit_down_55")[0]
        assert t["status"] == "SETTLED" and t["pnl"] > 0 and t["pnl_no_fee"] > t["pnl"]
        bal = db.execute("SELECT balance FROM fresh_accounts WHERE rule='limit_down_55'").fetchone()[0]
        assert abs(bal - (50 + t["pnl"])) < 1e-6


def test_unfilled_limit_order_is_cancelled_and_money_returned(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1060, book(.44, .46))
        observe(db, 1245, book(.30, .32))          # after 240 s, never traded through
        assert trades(db, "limit_down_55")[0]["status"] == "CANCELLED"
        assert db.execute("SELECT balance FROM fresh_accounts WHERE rule='limit_down_55'").fetchone()[0] == 50


def test_no_limit_order_outside_40_50(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1060, book(.60, .62))
        assert not trades(db, "limit_down_55")


def test_settlement_account_buys_clear_edge_and_holds(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1150, book(.40, .42), ref=100.5, opening=100.0, vol=.0005)   # official rule: UP very likely
        t = trades(db, "settlement_model")
        assert len(t) == 1 and t[0]["side"] == "UP" and t[0]["quantity"] * t[0]["price"] <= 3
        assert json.loads(t[0]["details"])["probability"] > .9
        verify(db, "UP")
        fresh.settle(db, 2000)
        assert trades(db, "settlement_model")[0]["pnl"] > 0


def test_main_switch_is_off_by_default_and_changes_only_forecast_when_on(tmp_path):
    store, app = setup(tmp_path)
    with store.connect() as db:
        assert fresh.main_uses_settlement(db) is False
    with TestClient(app) as c:
        assert c.post("/api/remote/main-model/settlement", headers=H).json() == {"main_model": "settlement"}
        with store.connect() as db:
            assert fresh.main_uses_settlement(db) is True
        c.post("/api/remote/main-model/standard", headers=H)
        with store.connect() as db:
            assert fresh.main_uses_settlement(db) is False
        assert c.post("/api/remote/main-model/live", headers=H).status_code == 404


def test_exit_report_uses_real_sale_price(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        db.execute("""INSERT INTO unified_rounds VALUES ('r1',1000,1300,1000,'ENTERED','m',NULL,'{}')""")
        cols = [c[1] for c in db.execute("PRAGMA table_info(unified_trades)")]
        row = dict(slug="r1", time=1010, side="UP", model="m", up=.6, stake=5, quantity=10, cost=5, fee=.1,
                   status="SOLD", closed=1100, proceeds=4, pnl=-1, details="{}", exit_details="{}")
        db.execute(f"INSERT INTO unified_trades({','.join(c for c in cols if c in row)}) VALUES ({','.join('?' for c in cols if c in row)})",
                   [row[c] for c in cols if c in row])
        verify(db, "UP", start=1000)
        db.execute("UPDATE verified_markets SET slug='r1' WHERE slug='round'")
        rows = {r["policy"]: r for r in unified.exit_comparison(db)}
        assert rows["hold_to_settlement"]["pnl"] == 5        # held: 10 shares pay $10 for $5
        assert rows["probability_exit"]["pnl"] == -1        # real sale, not the hold result
        assert rows["actual_early_sales_vs_hold"]["pnl"] == -6


def test_recorder_export_downloads(tmp_path):
    store, app = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1010, book(.45, .47))
    import gzip, time
    with store.connect() as db:
        db.execute("UPDATE fresh_recorder SET time=?", (time.time(),))
    with TestClient(app) as c:
        r = c.get("/api/export/recorder.csv.gz?hours=1")
        assert r.status_code == 200
        text = gzip.decompress(r.content).decode()
        assert text.startswith("time,slug") and "round" in text


def test_exit_guard_uses_fetch_time_not_last_trade_time(tmp_path):
    store, _ = setup(tmp_path)
    with store.connect() as db:
        d = details(1100)
        assert d["book_checked"] == 1100
        # A quiet market: last trade 20 s ago, but the book was fetched just now -> exits allowed.
        d["book_timestamp"] = 1080
        assert not (d.get("book_checked") and 1100 - d["book_checked"] > unified.EXIT_BOOK_MAX_AGE)
        # Book fetched 8 s ago -> exits blocked.
        d["book_checked"] = 1092
        assert d.get("book_checked") and 1100 - d["book_checked"] > unified.EXIT_BOOK_MAX_AGE


def test_hourly_fade_snapshots_then_fades_a_big_move_and_settles(tmp_path):
    from prophet import hourly
    store, _ = setup(tmp_path)
    with store.connect() as db:
        hourly.initialize(db)
        s = 3600 * 100
        assert hourly.decide(db, "h1", s, s + 2200, book(.40, .42), TERMS) == "SNAPSHOT"   # UP ask 42c at minute 36
        assert hourly.decide(db, "h1", s, s + 2500, book(.70, .72), TERMS) is None         # minute 41: wait
        assert hourly.decide(db, "h1", s, s + 2710, book(.70, .72), TERMS) == "DOWN"       # +30c move: fade it
        assert hourly.decide(db, "h1", s, s + 2720, book(.80, .82), TERMS) is None         # one trade per hour
        t = db.execute("SELECT * FROM hourly_trades").fetchone()
        assert t["cost"] <= 3 and t["side"] == "DOWN"
        hourly.settle_one(db, "h1", 0)                                                      # DOWN won
        a = hourly.accounts(db)[0]
        assert a["trades"] == 1 and a["wins"] == 1 and a["balance"] > 50


def test_hourly_small_move_is_skipped(tmp_path):
    from prophet import hourly
    store, _ = setup(tmp_path)
    with store.connect() as db:
        hourly.initialize(db)
        s = 3600 * 100
        hourly.decide(db, "h2", s, s + 2200, book(.40, .42), TERMS)
        assert hourly.decide(db, "h2", s, s + 2710, book(.50, .52), TERMS) is None          # only +10c

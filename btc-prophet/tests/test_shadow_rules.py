"""dev11 shadow test accounts and per-minute learning. All prices and outcomes are fixtures."""
from fastapi.testclient import TestClient

from prophet import shadow_rules, unified
from prophet.app import create_app
from prophet.store import Store

TERMS = dict(theta=.095, tick=.01, quantum=.01)


def initialize(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    return store, app


def book(bid=.45, ask=.48, depth=5000):
    return dict(bids=[dict(price=bid, quantity=depth)], asks=[dict(price=ask, quantity=depth)])


def observe(db, now, *, start=1000, raw=.9, level=None, slug="round"):
    details = dict(window_start=start, window_end=start+300, book_checked=now, reference_timestamp=now)
    unified.observe(db, slug, now, "SIMULATE", "fixture", raw, None, details, level or book(), TERMS,
                    lambda: now)


def verify(db, slug, start, outcome, published):
    db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
               (slug, start, start+300, outcome, 100, 101 if outcome == "UP" else 99, published, "fixture", "{}"))


def trades(db, rule):
    return db.execute("SELECT * FROM shadow_rule_trades WHERE rule=?", (rule,)).fetchall()


def test_skip_minute_2_waits_out_minute_two_and_main_account_is_untouched(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        observe(db, 1130)                                # minute 2: main enters, the rule does not
        main = db.execute("SELECT * FROM unified_trades").fetchone()
        assert main and main["stake"] == 5
        assert not trades(db, "skip_minute_2")
        assert trades(db, "cheap_40_55")                 # same rule at 40-55c may enter now
        observe(db, 1190)                                # minute 3: allowed
        t = trades(db, "skip_minute_2")
        assert len(t) == 1 and t[0]["side"] == "UP" and t[0]["cost"] <= 3
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0] == 1
        assert abs(db.execute("SELECT balance FROM unified_account").fetchone()[0] - (1000 - main["cost"])) < 1e-9


def test_cheap_rule_refuses_prices_above_55_cents(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        observe(db, 1030, raw=.95, level=book(.58, .61))
        assert not trades(db, "cheap_40_55")


def test_agree_90s_needs_both_agreement_and_90_seconds_and_holds(tmp_path):
    store, app = initialize(tmp_path)
    with store.connect() as db:
        observe(db, 1095, raw=.9, level=book(.40, .43))  # market says DOWN, model UP: no trade
        assert not trades(db, "agree_90s")
    with store.connect() as db:
        observe(db, 1050, slug="r2", start=1000, raw=.9, level=book(.55, .58))  # too early
        assert not db.execute("SELECT 1 FROM shadow_rule_trades WHERE rule='agree_90s' AND slug='r2'").fetchone()
        observe(db, 1095, slug="r2", start=1000, raw=.9, level=book(.55, .58))
        t = db.execute("SELECT * FROM shadow_rule_trades WHERE rule='agree_90s' AND slug='r2'").fetchone()
        assert t and t["side"] == "UP"
        observe(db, 1200, slug="r2", start=1000, raw=.1, level=book(.97, .99))  # never sells early
        assert db.execute("SELECT status FROM shadow_rule_trades WHERE rule='agree_90s' AND slug='r2'").fetchone()[0] == "OPEN"
        verify(db, "r2", 1000, "UP", 1305)
    app.state.paper.settle(1310)
    with store.connect() as db:
        t = db.execute("SELECT * FROM shadow_rule_trades WHERE rule='agree_90s' AND slug='r2'").fetchone()
        assert t["status"] == "SETTLED" and t["proceeds"] == t["quantity"] and t["pnl"] > 0
        s = {r["rule"]: r for r in shadow_rules.summary(db)}["agree_90s"]
        assert s["wins"] == 1 and abs(s["balance"] - (50 + t["pnl"])) < 1e-9


def test_accounts_never_go_below_zero_or_get_topped_up(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        db.execute("INSERT INTO shadow_rule_accounts VALUES ('skip_minute_2',900,2.5)")
        observe(db, 1030)
        assert not trades(db, "skip_minute_2")
        assert db.execute("SELECT balance FROM shadow_rule_accounts WHERE rule='skip_minute_2'").fetchone()[0] == 2.5


def test_a_shadow_failure_never_stops_the_main_bot(tmp_path, monkeypatch):
    store, _ = initialize(tmp_path)

    def broken(db, *args):
        db.execute("INSERT INTO shadow_rule_accounts VALUES ('x',1,1)")
        raise RuntimeError("boom")
    monkeypatch.setattr(shadow_rules, "observe", broken)
    with store.connect() as db:
        observe(db, 1030)
        assert db.execute("SELECT 1 FROM unified_trades").fetchone()
        assert not db.execute("SELECT 1 FROM shadow_rule_accounts WHERE rule='x'").fetchone()


def test_per_minute_learning_takes_over_after_enough_same_minute_labels(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        for i in range(unified.MINUTE_LABELS):
            slug, start = f"old{i}", -10000 + i*300
            db.execute("INSERT INTO unified_rounds VALUES (?,?,?,?,?,?,?,?)",
                       (slug, start, start+300, start, "SKIP", "fixture", None, "{}"))
            db.execute("INSERT INTO unified_minute_forecasts VALUES (?,?,?,?,?)", (slug, "fixture", 3, start+200, .75))
            verify(db, slug, start, "DOWN", start+305)     # every minute-3 "75% UP" call was wrong
        up, n, source = unified.calibrate(db, "new", "fixture", .75, .5, 2000, minute=3)
        assert source == "minute" and n == unified.MINUTE_LABELS and up < .5
        up0, n0, source0 = unified.calibrate(db, "new", "fixture", .75, .5, 2000, minute=1)
        assert source0 == "first_forecast" and n0 == 0 and up0 > .5


def test_dashboard_and_report_show_shadow_accounts_and_every_model(tmp_path):
    store, app = initialize(tmp_path)
    with store.connect() as db:
        observe(db, 1190)
    client = TestClient(app)
    rules = client.get("/api/unified").json()["shadow_rules"]
    assert {r["rule"] for r in rules} == set(shadow_rules.RULES)
    overview = client.get("/api/models-overview").json()
    assert overview["primary"]["exit"] == "probability"
    labels = {m["label"] for m in overview["models"]}
    assert {"Momentum", "XGBoost", "Model Vote", "Reference Gap + Market Agreement"} <= labels
    assert all(m["learning"] in ("Retrains every hour", "Adjusts its confidence", "Fixed rule, does not learn",
                                 "Learns from settled rounds") for m in overview["models"])
    page = client.get("/").text
    assert 'id="models-card"' in page and 'id="shadow-card"' in page


def test_quick_report_has_test_accounts_sheet(tmp_path):
    from openpyxl import load_workbook
    from prophet.v2reporting import write_quick_workbook
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        observe(db, 1190)
    path = tmp_path / "quick.xlsx"
    write_quick_workbook(store.path, path)
    wb = load_workbook(path, read_only=True, data_only=True)
    rows = list(wb["Test Accounts"].values)
    assert rows[0][0] == "Account" and any(str(r[0]).startswith("Summary: ") for r in rows)
    assert any(r[0] == "Main rule, skips minute 2" and r[1] == "round" for r in rows)
    exits = {r[0] for r in list(wb["Exit Shadow Comparison"].values)[1:]}
    assert "reversal_exit" in exits
    wb.close()

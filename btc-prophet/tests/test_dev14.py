"""dev14: reality-check test accounts, 40-49c account and the price feed check. Fixtures only."""
from prophet import feed_check, shadow_rules, unified
from prophet.app import create_app
from prophet.store import Store

TERMS = dict(theta=.095, tick=.01, quantum=.01)


def setup(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    return store


def book(bid=.45, ask=.48, depth=5000):
    return dict(bids=[dict(price=bid, quantity=depth)], asks=[dict(price=ask, quantity=depth)])


def observe(db, now, *, raw=.9, level=None, slug="round", ref=101.0, opening=100.0, start=1000):
    details = dict(window_start=start, window_end=start + 300, book_checked=now, reference_timestamp=now - 1,
                   reference_price=ref, opening_price=opening)
    unified.observe(db, slug, now, "SIMULATE", "fixture", raw, None, details, level or book(), TERMS, lambda: now)


def rule_trades(db, rule):
    return db.execute("SELECT * FROM shadow_rule_trades WHERE rule=?", (rule,)).fetchall()


def test_blend_trusts_only_30_percent_of_disagreement():
    assert abs(shadow_rules.blended(.80, .50) - .59) < 1e-12
    assert abs(shadow_rules.blended(.50, .50) - .50) < 1e-12


def direct(db, now, up, level, slug="r1"):
    shadow_rules.observe(db, slug, now, 1000, 1300, up, up, level, TERMS, False)


def test_reality_check_skips_small_model_edge_that_main_rule_takes(tmp_path):
    store = setup(tmp_path)
    with store.connect() as db:
        direct(db, 1030, .62, book(.45, .48))      # model 62% vs 48c: 29% edge, blended ~51% is not enough
        assert rule_trades(db, "cheap_40_55")       # the plain main-style rule buys
        assert not rule_trades(db, "reality_check")
        assert not rule_trades(db, "reality_check_hold")


def test_reality_check_enters_on_very_strong_signal_and_stays_paper(tmp_path):
    store = setup(tmp_path)
    with store.connect() as db:
        direct(db, 1030, .99, book(.30, .32))      # blended ~51% vs ~33c all-in: clear edge
        for rule in ("reality_check", "reality_check_hold"):
            t = rule_trades(db, rule)
            assert len(t) == 1 and t[0]["side"] == "UP" and t[0]["cost"] <= 3
        bal = db.execute("SELECT balance FROM shadow_rule_accounts WHERE rule='reality_check'").fetchone()[0]
        assert 47 <= bal < 50


def test_gap_40_49_rejects_50c_and_up(tmp_path):
    store = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1030, raw=.95, level=book(.50, .52))
        assert not rule_trades(db, "gap_40_49")


def test_new_accounts_listed_in_summary(tmp_path):
    store = setup(tmp_path)
    with store.connect() as db:
        names = {a["rule"] for a in shadow_rules.summary(db)}
    assert {"reality_check", "reality_check_hold", "gap_40_49", "skip_minute_2"} <= names


def test_feed_check_compares_bot_open_with_official_open(tmp_path):
    store = setup(tmp_path)
    with store.connect() as db:
        observe(db, 1010, ref=100.5, opening=100.0)
        observe(db, 1250, ref=101.0, opening=100.0)
        db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                   ("round", 1000, 1300, "UP", 100.4, 101.2, 1400, "fixture", "{}"))
        rows = feed_check.rows(db)
        assert len(rows) == 1
        r = rows[0]
        assert abs(r["open_difference"] - (-0.4)) < 1e-9 and r["bot_direction_matches"] is True
        s = feed_check.summary(db)
        assert s["rounds"] == 1 and s["minute4_direction_right"] == 1.0 and abs(s["median_feed_age"] - 1) < 1e-9
        assert unified.summary(db, 1400)["feed_check"]["rounds"] == 1

"""Selective $50 shadow account: rule, money limits and exits. All prices are fixtures."""
from prophet import selective50, unified
from test_unified_v321 import TERMS, book, initialize, verify


def tick(db, now, *, start=1000, raw=.9, ref=100.1, level=None, slug="round"):
    details = dict(window_start=start, window_end=start + 300, book_checked=now, reference_timestamp=now,
                   reference_price=ref, opening_price=100.0, volatility=.0005)
    unified.observe(db, slug, now, "SIMULATE", "fixture", raw, None, details, level or book(.60, .62), TERMS, lambda: now)


def trade(db, slug="round"):
    return db.execute("SELECT * FROM selective50_trades WHERE slug=?", (slug,)).fetchone()


def test_enters_only_in_minute_three_when_all_three_agree(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1100)                                     # 1:40 in: too early
        assert trade(db) is None
        tick(db, 1190, level=book(.40, .42))               # market disagrees (UP mid 41c)
        assert trade(db) is None
        tick(db, 1195, raw=.9)
        t = trade(db)
        assert t["side"] == "UP" and t["cost"] <= 3 and t["status"] == "OPEN"
        s = selective50.summary(db)
        assert abs(s["balance"] - (50 - t["cost"])) < 1e-9 and s["open"] == 1
        verify(db, "round", 1000, "UP", 1305); unified.settle(db, 1310)
        t = trade(db)
        assert t["status"] == "SETTLED" and t["pnl"] > 0
        assert abs(selective50.summary(db)["balance"] - (50 - t["cost"] + t["quantity"])) < 1e-9


def test_price_cap_and_reversal_exit(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1190, level=book(.93, .95))               # costs more than 90c: skip
        assert trade(db) is None
        tick(db, 1200, slug="b", start=1010)
        assert trade(db, "b")["side"] == "UP"
        tick(db, 1250, slug="b", start=1010, raw=.3, ref=99.9)   # Reference Gap flips to DOWN: sell
        t = trade(db, "b")
        assert t["status"] == "SOLD" and t["proceeds"] > 0
        assert selective50.summary(db)["early_exits"] == 1


def test_never_goes_negative_or_adds_money(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1190)
        db.execute("UPDATE selective50_account SET balance=2.5 WHERE id=1")
        tick(db, 1490, start=1300, slug="next")            # under $3 left: no trade
        assert trade(db, "next") is None
        assert selective50.summary(db)["balance"] == 2.5


def big(db, slug="round"):
    return db.execute("SELECT * FROM selective1000_trades WHERE slug=?", (slug,)).fetchone()


def test_thousand_dollar_ledger_trades_fifty_with_the_same_rule_and_settles(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1100)                                     # too early for both ledgers
        assert big(db) is None
        tick(db, 1195)
        b, s = big(db), trade(db)
        assert b["side"] == s["side"] == "UP" and 47.5 <= b["cost"] <= 50 and s["cost"] <= 3
        verify(db, "round", 1000, "UP", 1305); unified.settle(db, 1310)
        b = big(db)
        assert b["status"] == "SETTLED" and b["pnl"] > 0
        summ = selective50.summary(db, "selective1000")
        assert summ["start"] == 1000 and summ["stake"] == 50 and summ["wins"] == 1
        assert abs(summ["balance"] - (1000 + b["pnl"])) < 1e-9


def test_thousand_dollar_ledger_never_forces_a_partial_fill(tmp_path):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1195, level=book(.60, .62, 20))           # book too thin to buy $50
        assert big(db) is None


def test_thousand_dollar_ledger_stops_below_fifty_and_is_isolated(tmp_path, monkeypatch):
    store, _ = initialize(tmp_path)
    with store.connect() as db:
        tick(db, 1100)
        db.execute("INSERT INTO selective1000_account VALUES (1,900,49.0)")
        tick(db, 1195)
        assert big(db) is None and trade(db) is not None   # $1,000 ledger out of money; $50 ledger trades
        assert selective50.summary(db, "selective1000")["balance"] == 49.0


def test_a_failure_in_one_ledger_never_changes_the_other(tmp_path, monkeypatch):
    store, _ = initialize(tmp_path)
    real = selective50._act

    def flaky(db, prefix, *args):
        if prefix == "selective1000":
            db.execute("INSERT INTO selective1000_account VALUES (1,1,1)")
            raise RuntimeError("boom")
        return real(db, prefix, *args)
    monkeypatch.setattr(selective50, "_act", flaky)
    with store.connect() as db:
        tick(db, 1100)
        tick(db, 1195)
        assert trade(db) is not None
        assert not db.execute("SELECT 1 FROM selective1000_account").fetchone()

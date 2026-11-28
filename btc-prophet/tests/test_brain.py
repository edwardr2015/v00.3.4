"""2.2 brain: records each voice once per minute, grades them, learns trust, ranks accounts."""
import json
import random

from prophet import brain
from prophet.app import create_app
from prophet.store import Store


def test_brain_learns_market_is_better_when_model_is_noise(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    rng = random.Random(1)
    with store.connect() as db:
        brain.initialize(db)
        for i in range(120):
            truth = rng.random()
            up = rng.random() < truth
            slug = f"r{i}"
            brain.record(db, slug, 1000 + 60, 1000, dict(market=truth, main=rng.random(), settlement=None))
            brain.record(db, slug, 1000 + 70, 1000, dict(market=.5, main=.5))      # same minute: ignored
            db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                       (slug, 1000, 1300, "UP" if up else "DOWN", 1, 2, 1400, "f", "{}"))
        assert db.execute("SELECT count(*) FROM brain_thoughts").fetchone()[0] == 120
        sc = brain.scores(db)[1]
        assert sc["brier"]["market"] < sc["brier"]["main"]          # the market really is better here
        assert sc["trust"]["main"] <= .2                              # so it learns to trust the model little
        t = brain.thinking(db)
        assert any("most accurate = market" in line for line in t["summary"])


def test_brain_learns_to_trust_a_good_model(tmp_path):
    store = Store(str(tmp_path / "p.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    rng = random.Random(2)
    with store.connect() as db:
        brain.initialize(db)
        for i in range(150):
            truth = rng.random()
            up = rng.random() < truth
            brain.record(db, f"g{i}", 1130, 1000, dict(market=.5, settlement_live=truth))   # model knows, market doesn't
            db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"g{i}", 1000, 1300, "UP" if up else "DOWN", 1, 2, 1400, "f", "{}"))
        assert brain.scores(db)[2]["trust"]["settlement_live"] >= .8


def test_leaderboard_ranks_accounts_and_api_works(tmp_path):
    from fastapi.testclient import TestClient
    store = Store(str(tmp_path / "p.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    with store.connect() as db:
        from prophet import selective50
        selective50.initialize(db)
        for i in range(12):
            db.execute("INSERT INTO selective50_trades(slug,time,end,side,quantity,cost,status,pnl,details) VALUES (?,?,?,'UP',5,3,'SETTLED',?,'{}')",
                       (f"s{i}", i, i, 1.0 if i % 4 else -3.0))
    with TestClient(app) as c:
        b = c.get("/api/brain").json()
    top = b["leaderboard"][0]
    assert top["name"] == "Selective $50" and top["trades"] == 12 and top["wins"] == 9

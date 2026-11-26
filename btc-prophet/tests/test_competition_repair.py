"""Isolated synthetic regressions for the paper-ledger and formal preflight repairs."""
import json

import pytest
from fastapi.testclient import TestClient

from prophet import hold_research
from prophet.app import create_app
from prophet.competition import LEARNING_SECONDS, preflight
from prophet.learning import Learning
from prophet.models import BASE_PARTICIPANTS, PARTICIPANTS
from prophet.v2paper import Engine
from test_timing_update import market_inputs
from test_v2 import setup, verify


def test_down_forecast_can_buy_up_on_favorable_executable_odds(tmp_path):
    s, e, now = setup(tmp_path)
    e.probability = lambda *args, **kwargs: (.4, dict(momentum_60=-1, trend_300=-1, volatility=.002))
    market_inputs(s, now, bid=.18, ask=.21)
    e.mode("SIMULATE", now)
    e.tick(now)
    with s.connect() as db:
        orders = db.execute("SELECT * FROM v2_orders ORDER BY id").fetchall()
        assert len(orders) == 3
        for order in orders:
            audit = json.loads(order["details"])
            assert order["probability"] == .4 and order["side"] == "UP"
            assert audit["prediction"]["selected"]["ev"] == pytest.approx(
                .4*order["quantity"]-order["cost"]
            )
            assert audit["fill"]["total"] == pytest.approx(audit["fill"]["gross"]+order["fee"])
            assert audit["fill"]["average_price"] >= .22
            assert audit["prediction"]["candidates"][1]["ev"] < 0


def test_down_hold_wins_when_shadow_flip_to_up_loses(tmp_path):
    s, e, now = setup(tmp_path)
    e.mode("SIMULATE", now)
    e.probability = lambda *args, **kwargs: (.15, dict(momentum_60=-1, trend_300=-1, volatility=.002))
    market_inputs(s, now, bid=.36, ask=.38)
    e.tick(now)
    with s.connect() as db:
        cash = db.execute("SELECT balance FROM v2_accounts WHERE id='adaptive-3'").fetchone()[0]
    e.probability = lambda *args, **kwargs: (.98, dict(momentum_60=1, trend_300=1, volatility=.002))
    market_inputs(s, now+40, bid=.24, ask=.25)
    e.tick(now+40)
    with s.connect() as db:
        assert db.execute("SELECT balance FROM v2_accounts WHERE id='adaptive-3'").fetchone()[0] == cash
        assert not db.execute("SELECT 1 FROM paper_flips").fetchone()
    verify(s, now, "DOWN")
    e.tick(now+300)
    with s.connect() as db:
        comparison = next(hold_research.rows(db))
        assert comparison["side"] == "DOWN" and comparison["flip_side"] == "UP"
        assert comparison["pnl"] == pytest.approx(comparison["hold_pnl"])
        assert comparison["hold_pnl"] > 0 > comparison["flip_pnl"]
        assert comparison["flip_mode"] == "UNFUNDED SHADOW"


def test_delayed_official_settlement_waits_for_verification(tmp_path):
    s, e, now = setup(tmp_path)
    e.mode("SIMULATE", now)
    e.tick(now)
    verify(s, now+200)
    e.tick(now+300)
    assert all(row["status"] == "OPEN" for row in e.summary()["orders"])
    assert len(e.summary()["orders"]) == 3
    e.tick(now+500)
    assert all(row["status"] == "SETTLED" for row in e.summary()["orders"])
    e.tick(now+501)
    assert all(a["balance"] == pytest.approx(1000+a["realized"])
               for a in e.summary()["accounts"] if a["id"].startswith("adaptive-"))


def test_formal_preflight_persists_and_blocks_unready(tmp_path):
    s, e, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    with TestClient(app) as c:
        headers = {"X-Prophet-Control":"local-dashboard"}
        assert c.post("/api/control/formal-start", headers=headers).status_code == 409
        assert c.post("/api/control/start", headers=headers).status_code == 200
        assert s.get("v2_experiment") is None
    learning = app.state.learning
    learning.record_learning_time(now)
    learning.record_learning_time(now+10)
    reopened = Learning(s)
    reopened.record_learning_time(now+100)
    reopened.record_learning_time(now+110)
    assert s.get("competition_learning_clock") == dict(started=now, seconds=20)
    s.put("competition_learning_clock", dict(started=now-LEARNING_SECONDS, seconds=LEARNING_SECONDS))
    market_inputs(s, now)
    slug = s.get("market")["slug"]
    with s.connect() as db:
        for model in PARTICIPANTS:
            config=f"timing/v2/fixture-{model}/90/HOLD/1"
            db.execute("UPDATE lab_configs SET status='ARCHIVED VERSION' WHERE family=?", (model,))
            db.execute("INSERT INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                       (config,model,f"fixture-{model}",90,"HOLD",1,"FORWARD SHADOW",1000,now-90))
            db.execute("INSERT INTO lab_predictions VALUES (?,?,?,?,?,?,?)",
                       (model,f"fixture-{model}",slug,90,now-1,.61,"[]"))
        assert preflight(db, now)["ready"]
    e.mode("SIMULATE", now, formal=True)
    experiment = s.get("v2_experiment")
    assert experiment["start"] == now and experiment["end"] == now+7*86400
    assert experiment["timezone"] == "UTC" and len(experiment["preflight"]["versions"]) == 9
    assert Engine(s).store.get("v2_experiment") == experiment


def test_upgrade_preserves_unverified_experiment_and_existing_cash(tmp_path):
    s, e, now = setup(tmp_path)
    prior = dict(start=now-10,end=now+500,status="RUNNING",model_hash="legacy")
    s.put("v2_experiment", prior)
    s.put("competition_repair_v1", False)
    s.put("emergency", True)
    with s.connect() as db:
        db.execute("UPDATE v2_accounts SET balance=987.25,halted='review' WHERE id='adaptive-3'")
    Engine(s)
    assert s.get("pre_competition_repair_experiment") == prior
    assert s.get("v2_experiment") is None and s.get("emergency")
    with s.connect() as db:
        assert tuple(db.execute("SELECT balance,halted FROM v2_accounts WHERE id='adaptive-3'").fetchone()) == (987.25, "review")


def test_future_model_version_cannot_predict_earlier_capture(tmp_path, monkeypatch):
    s, e, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    lab = app.state.agreement.lab
    market_inputs(s, now)
    e.mode("LEARN", now)
    e.tick(now)
    lab.refreshed = now
    with s.connect() as db:
        db.execute("INSERT INTO model_registry VALUES ('future','logistic',?,'fixture','file','hash','{}')",
                   (now+1,))
        db.execute("INSERT INTO lab_configs VALUES ('timing/v2/future/90/HOLD/1','logistic','future',90,'HOLD',1,'FORWARD SHADOW',1000,?)",
                   (now+1,))
    monkeypatch.setattr(lab,"predict",lambda *args: .63)
    lab.tick(now+1)
    with s.connect() as db:
        assert db.execute("SELECT 1 FROM lab_predictions WHERE version='momentum-v2'").fetchone()
        assert not db.execute("SELECT 1 FROM lab_predictions WHERE version='future'").fetchone()


@pytest.mark.parametrize("failed_model", tuple(BASE_PARTICIPANTS))
def test_each_forecast_failure_isolated_from_other_seven(tmp_path, monkeypatch, failed_model):
    from prophet import pattern_odds

    s, e, now = setup(tmp_path)
    lab = create_app(s.path, collect=False, auth_required=False).state.agreement.lab
    lab.refreshed = now
    versions = {family: pattern_odds.VERSION if family == "pattern_odds" else f"fixture-{family}"
                for family in BASE_PARTICIPANTS}
    with s.connect() as db:
        db.execute("UPDATE lab_configs SET status='ARCHIVED VERSION'")
        for family, version in versions.items():
            db.execute("INSERT OR IGNORE INTO model_registry VALUES (?,?,?,'fixture','file','hash','{}')",
                       (version,family,now-1))
            db.execute("INSERT OR REPLACE INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"timing/v2/{version}/90/HOLD/1",family,version,90,"HOLD",1,"FORWARD SHADOW",1000,now-1))
    def estimate(version, *args):
        if version == versions[failed_model]:
            raise RuntimeError("Isolated fixture inference fault")
        return .65
    monkeypatch.setattr(lab,"predict",estimate)
    monkeypatch.setattr(pattern_odds,"forecast",lambda *args: (estimate(pattern_odds.VERSION), {}))
    market_inputs(s, now)
    e.mode("SIMULATE", now)
    e.tick(now)
    lab.tick(now)
    with s.connect() as db:
        forecasts = dict(db.execute("SELECT config_family,up FROM lab_predictions WHERE checkpoint=90"))
        assert forecasts == {family:.65 for family in BASE_PARTICIPANTS if family!=failed_model}
        assert db.execute("SELECT status FROM model_forecast_health WHERE version=?",
                          (versions[failed_model],)).fetchone()[0] == "INFERENCE FAILED"
        assert db.execute("SELECT COUNT(*) FROM lab_orders").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM lab_skips WHERE reason='MODEL_PREDICTION_UNAVAILABLE'").fetchone()[0] == 1


def test_emergency_blocks_commit_while_evaluation_is_in_flight(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    s, _, now = setup(tmp_path)
    app = create_app(s.path, collect=False, auth_required=False)
    e = app.state.paper
    e.mode("SIMULATE", now)
    market_inputs(s, now)
    started, release = Event(), Event()
    def estimate(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return .9, dict(momentum_60=1, trend_300=1, volatility=.002)
    e.probability = estimate
    with TestClient(app) as c, ThreadPoolExecutor(max_workers=1) as pool:
        evaluation = pool.submit(e.continuous.evaluate,now,"SIMULATE",False)
        try:
            assert started.wait(5)
            assert c.post("/api/control/emergency",
                          headers={"X-Prophet-Control":"local-dashboard"}).status_code == 200
        finally:
            release.set()
        evaluation.result()
    assert s.get("emergency") and not e.summary()["orders"]
    assert all(a["balance"] == 1000 for a in e.summary()["accounts"] if a["id"].startswith("adaptive-"))


def test_database_writer_lock_resolves_without_a_duplicate_debit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    from threading import Event

    s, e, now = setup(tmp_path)
    market_inputs(s, now)
    e.mode("SIMULATE", now)
    attempted = Event()
    estimate = e.probability
    def mark(*args, **kwargs):
        attempted.set()
        return estimate(*args, **kwargs)
    e.probability = mark
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = sqlite3.connect(s.path)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE v2_accounts SET balance=0 WHERE id='adaptive-3'")
            result = pool.submit(e.continuous.evaluate,now,"SIMULATE",False)
            assert attempted.wait(5)
            assert not result.done()
        finally:
            writer.rollback()
            writer.close()
        result.result()
    e.continuous.evaluate(now,"SIMULATE",False)
    orders = e.summary()["orders"]
    assert len(orders) == 3
    assert len({row["account"] for row in orders}) == 3
    cost = {row["account"]: row["cost"] for row in orders}
    assert all(a["balance"] == pytest.approx(1000-cost[a["id"]])
               for a in e.summary()["accounts"] if a["id"] in cost)


def test_adaptive_equity_uses_fresh_executable_liquidation(tmp_path, monkeypatch):
    s, e, now = setup(tmp_path)
    e.mode("SIMULATE", now)
    market_inputs(s, now, bid=.25, ask=.28)
    e.tick(now)
    market_inputs(s, now+4, bid=.48, ask=.51)
    monkeypatch.setattr("time.time",lambda:now+4)
    account = next(row for row in e.summary()["accounts"] if row["id"]=="adaptive-3")
    assert account["realized"] == 0
    assert account["market_equity"] > 1000
    assert account["unrealized_pnl"] == pytest.approx(account["market_equity"]-1000)
    monkeypatch.setattr("time.time",lambda:now+20)
    account = next(row for row in e.summary()["accounts"] if row["id"]=="adaptive-3")
    assert account["market_equity"] is None and account["unrealized_pnl"] is None


def test_excel_distinguishes_forecast_activity_from_formal_start(tmp_path):
    from io import BytesIO
    from openpyxl import load_workbook

    store, _, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.mode("LEARN", now)
    slug = store.get("market")["slug"]
    with store.connect() as db:
        db.execute("INSERT INTO lab_predictions VALUES (?,?,?,?,?,?,?)",
                   ("momentum", "momentum-v2", slug, 90, now, .63, "[]"))
        db.execute("INSERT INTO lab_predictions VALUES (?,?,?,?,?,?,?)",
                   ("lstm", "archived-lstm", slug, 90, now, .61, "[]"))
    verify(store, now, "DOWN")
    with TestClient(app) as client:
        exported = client.get("/api/export.xlsx")
        assert exported.status_code == 200
    with BytesIO(exported.content) as content:
        book = load_workbook(content, read_only=True)
        dashboard = dict(book["Performance Dashboard"].iter_rows(max_col=2, values_only=True))
        assert dashboard["Current decision mode"] == "LEARN"
        assert dashboard["Formal seven-day experiment"] == "NOT STARTED"
        assert dashboard["Competition forecasts recorded"] == 1
        assert dashboard["Competition model families with forecasts"] == 1
        assert dashboard["Trade records"] == 0
        assert "shadow research" in dashboard["Paper accounts"]
        assert [row[7:9] for row in list(book["Challenger Outcomes"].values)[1:]] == [("DOWN", False)] * 2
        book.close()

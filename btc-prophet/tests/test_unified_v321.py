"""Isolated regression tests. All prices and outcomes here are fixtures."""
import json

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet import unified
from prophet.app import create_app
from prophet.learning import matched_forecasts
from prophet.store import Store
from prophet.v2paper import Engine
from prophet.v2reporting import write_quick_workbook

TERMS = dict(theta=.095, tick=.01, quantum=.01)


def initialize(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    engine = create_app(store.path,collect=False,auth_required=False).state.paper
    engine.recover_startup(1000)
    return store, engine


def book(bid=.45, ask=.48, depth=5000):
    return dict(bids=[dict(price=bid,quantity=depth)], asks=[dict(price=ask,quantity=depth)])


def observe(db, now=1100, *, start=1000, raw=.9, level=None, block=None, mode="SIMULATE", slug="round", clock=None):
    details = dict(window_start=start,window_end=start+300,book_checked=now,reference_timestamp=now)
    unified.observe(db,slug,now,mode,"fixture",raw,block,details,level or book(),TERMS,clock or (lambda: now))


def verify(db, slug, start, outcome, published):
    db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
               (slug,start,start+300,outcome,100,101 if outcome=="UP" else 99,published,"fixture","{}"))


def test_recovery_resumes_collection_clears_only_legacy_restart_preserves_account(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    engine = Engine(store)
    store.put("collection_paused",True)
    store.put("health_market",{"checked":950})
    engine.halt("SERVICE_RESTART_REVIEW")
    engine.halt("CRITICAL_SYSTEM_ERROR","adaptive-1")
    engine.recover_startup(1000)
    assert store.get("mode")=="OFF"
    assert store.get("collection_paused") is False and store.get("health_market") is None
    with store.connect() as db:
        assert db.execute("SELECT halted FROM v2_accounts WHERE id='adaptive-1'").fetchone()[0]=="CRITICAL_SYSTEM_ERROR"
        assert db.execute("SELECT status FROM intervention_requests WHERE issue='SERVICE_RESTART_REVIEW'").fetchone()[0]=="RECOVERED"
        assert unified.summary(db,1000)["balance"]==1000
    engine.review()
    engine.mode("SIMULATE",1001)
    again = Engine(store)
    again.recover_startup(1010)
    assert store.get("mode")=="SIMULATE"
    again.mode("OFF",1020)
    again.recover_startup(1030)
    assert store.get("mode")=="OFF"


def test_wait_then_adaptive_fill_once_at_any_eligible_time_and_settle(tmp_path):
    store, engine = initialize(tmp_path)
    with store.connect() as db:
        observe(db,1017,block="STALE_REFERENCE")
        observe(db,1092,level=book(.33,.36))
        observe(db,1231)
        observe(db,1232)
        observe(db,1300)
        observe(db,1320,start=1300,slug="next")
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0]==1
        order=db.execute("SELECT * FROM unified_trades").fetchone()
        assert order["time"]==1231 and order["side"]=="UP" and order["stake"]==5
        assert db.execute("SELECT reason FROM unified_rounds WHERE slug='next'").fetchone()[0]=="PRIOR_POSITION_OPEN"
        verify(db,"round",1000,"UP",1315)
    engine.settle(1310)
    with store.connect() as db:
        assert unified.summary(db,1310)["position"]
    engine.settle(1316)
    engine.settle(1317)
    with store.connect() as db:
        result=unified.summary(db,1317)
        assert result["wins"]==1 and result["position"] is None
        assert abs(result["balance"]-(1000+order["quantity"]-order["cost"]))<1e-8


def test_probability_exit_requires_new_full_quote_and_records_hold(tmp_path):
    store,engine = initialize(tmp_path)
    with store.connect() as db:
        observe(db)
        observe(db,1101,raw=.15,level=book(.72,.75,1))
        observe(db,1102,raw=.15,level=book(.72,.75),clock=iter([1102,None]).__next__)
        assert unified.summary(db,1102)["position"]
        observe(db,1103,raw=.15,level=book(.72,.75))
        sold = db.execute("SELECT * FROM unified_trades").fetchone()
        assert sold["status"]=="SOLD" and sold["pnl"]>0
        verify(db,"round",1000,"UP",1305)
    engine.settle(1310)
    with store.connect() as db:
        order=next(unified.history(db))
        assert order["status"]=="SOLD" and order["hold_pnl"]>order["pnl"]


def test_learning_thin_depth_insufficient_cash_and_close_clock_never_fill(tmp_path):
    store,_ = initialize(tmp_path)
    with store.connect() as db:
        observe(db,mode="LEARN")
        observe(db,1101,level=book(depth=1))
        observe(db,1102,clock=iter([1102,None]).__next__)
        db.execute("UPDATE unified_account SET balance=4.99")
        observe(db,1103)
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0]==0
        assert db.execute("SELECT reason FROM unified_rounds").fetchone()[0]=="INSUFFICIENT_CASH"


def test_premium_shadow_does_not_debit_until_proven_higher_cost_sample(tmp_path):
    store,_ = initialize(tmp_path)
    with store.connect() as db:
        db.execute("""INSERT INTO selective_forecasts(slug,start,end,status,observed,side)
                   VALUES ('round',1000,1300,'CALL',1090,'UP')""")
        observe(db,1091,level=book(.73,.76))
        assert db.execute("SELECT count(*) FROM unified_large_trials").fetchone()[0]==1
        assert not unified.summary(db,1091)["position"]
        assert not unified.matched_large(db,1091)["qualified"]
        for i in range(100):
            start=1500+i*300
            slug=f"archive-{i}"
            db.execute("INSERT INTO unified_rounds VALUES (?,?,?,?,?,NULL,NULL,'{}')",
                       (slug,start,start+300,start+90,"TEST"))
            db.execute("INSERT INTO unified_large_trials VALUES (?,?,?,?,?,?,?,?)",
                       (slug,start+91,"UP",140,100,135,100,"{}"))
            verify(db,slug,start,"UP" if i%10 else "DOWN",start+302)
        start=33000
        db.execute("""INSERT INTO selective_forecasts(slug,start,end,status,observed,side)
                   VALUES ('new',?,?, 'CALL',?,'UP')""",(start,start+300,start+90))
        observe(db,start+92,start=start,slug="new",level=book(.73,.76))
        assert unified.matched_large(db,start+92)["qualified"]
        assert unified.summary(db,start+92)["position"]["stake"]==100
        observe(db,start+99,start=start,slug="new",raw=.05,level=book(.82,.84))
        assert unified.summary(db,start+99)["position"]["stake"]==100


def test_calibration_and_challenger_ignore_future_and_match_capture(tmp_path):
    store,_ = initialize(tmp_path)
    with store.connect() as db:
        observe(db,1100,mode="LEARN")
        verify(db,"round",1000,"UP",1305)
        assert unified.calibrate(db,"next","fixture",.9,.5,1304)[1]==0
        assert unified.calibrate(db,"next","fixture",.9,.5,1306)[1]==1
        db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
                   ("round",1100,"fixture",.7,"UP","TEST",0,"{}"))
        db.execute("INSERT INTO model_shadows VALUES (?,?,?,?,?,?,?)",
                   ("challenger","round",1101,.2,1,.64,json.dumps(dict(feature_time=1100))))
    store.put("shadow_version","challenger")
    with store.connect() as db:
        assert matched_forecasts(db,1304)["matched"]==0
        result=matched_forecasts(db,1306)
        assert result["matched"]==1 and result["baseline_right"]==1 and result["challenger_right"]==0


def test_scoreboard_api_and_quick_excel(tmp_path):
    store,engine = initialize(tmp_path)
    app = create_app(store.path,collect=False,auth_required=False)
    with store.connect() as db:
        observe(db)
        verify(db,"round",1000,"UP",1305)
    engine.settle(1310)
    with TestClient(app) as client:
        assert client.get("/").status_code==200
        assert client.get("/api/unified").json()["wins"]==1
        assert client.get("/api/research-comparison").json()["large"]["rounds"]==0
    path=tmp_path/"quick.xlsx"
    write_quick_workbook(store.path,path)
    workbook=load_workbook(path,read_only=True,data_only=True)
    assert "Unified Trades" in workbook.sheetnames
    assert list(workbook["Unified Trades"].values)[1][0]=="round"
    workbook.close()


def test_startup_migrates_paused_collection_without_manual_control(tmp_path, monkeypatch):
    from prophet.collector import Collector

    async def fixture_start(collector):
        collector.store.put("test_workers_enabled",collector.enabled)

    monkeypatch.setattr(Collector,"start",fixture_start)
    store = Store(str(tmp_path/"app.sqlite"))
    previous = create_app(store.path,collect=False,auth_required=False)
    previous.state.paper.halt("SERVICE_RESTART_REVIEW")
    store.put("collection_paused",True)
    with TestClient(create_app(store.path,auth_required=False)) as client:
        body = client.get("/api/state").json()
        assert body["version"].startswith(client.get("/openapi.json").json()["info"]["version"])
        assert body["collecting"] and body["mode"]=="SIMULATE"
        assert store.get("test_workers_enabled")
        assert client.get("/api/unified").json()["balance"]==1000
        assert client.get("/api/state").json()["primary_signal"]["side"] is None
    with TestClient(create_app(store.path,auth_required=False)) as client:
        assert client.get("/api/unified").json()["balance"]==1000
        assert client.get("/api/state").json()["mode"]=="SIMULATE"


def test_actual_engine_entry_uses_fresh_book_and_preserves_old_ledgers(tmp_path):
    from test_timing_update import market_inputs
    from test_v2 import setup

    store, _, now = setup(tmp_path)
    engine = create_app(store.path,collect=False,auth_required=False).state.paper
    engine.recover_startup(now)
    engine.probability = lambda *args, **kwargs: (.95, {"momentum_60":.01})
    engine.tick(now)
    with store.connect() as db:
        assert not unified.summary(db,now)["position"]
    market_inputs(store,now+1,bid=.45,ask=.48)
    engine.tick(now+1)
    with store.connect() as db:
        assert unified.summary(db,now+1)["position"]["side"]=="UP"
        assert db.execute("SELECT count(*) FROM v2_orders").fetchone()[0]==0

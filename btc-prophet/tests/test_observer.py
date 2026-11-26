"""Synthetic observer safety, comparison and dashboard checks."""
from datetime import datetime, timezone
import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet import observer
from prophet.agreement import CHECKPOINTS
from prophet.analytics import record_market
from prophet.app import create_app
from prophet.v2paper import fingerprint
from test_timing_update import market_inputs
from test_v2 import setup

HEADERS = {"X-Prophet-Control": "local-dashboard"}
FILL = dict(total=1., quantity=2., fee=.01)


def result(store, slug, now, side):
    record_market(store, dict(slug=slug, opening=100, closing=101 if side=="UP" else 99,
                            outcome=side, yes_payout=int(side=="UP"), received=now,
                            source="SYNTHETIC TEST"), now-300, now)


def read(store, query, params=()):
    with store.connect() as db:
        return db.execute(query, params).fetchall()


def test_observer_dashboard_pick_stays_fixed_and_exports_scenarios(tmp_path, monkeypatch):
    store, _, now = setup(tmp_path)
    app = create_app(store.path, collect=False, auth_required=False)
    paper, agreement = app.state.paper, app.state.agreement
    clock = [now-90]
    monkeypatch.setattr("time.time", lambda: clock[0])
    start = clock[0]
    store.save_candles([dict(time=t,open=100+(t-start+315)*.01,high=100+(t-start+315)*.01,
                            low=100+(t-start+315)*.01,close=100+(t-start+315)*.01,ticks=1)
                        for t in range(start-315,start+300)])
    with TestClient(app) as c:
        assert "BTC Prophet" in c.get("/").text
        assert c.get("/api/pick").json()["call"] is None
        assert c.post("/api/observer/start",headers=HEADERS).status_code==200
        assert c.get("/api/pick").json()["observer"]["cash"]==1000
        for offset in CHECKPOINTS:
            clock[0]=start+offset
            market_inputs(store,clock[0],bid=.28,ask=.30)
            paper.tick(clock[0])
            agreement.tick(clock[0])
        slug=store.get("market")["slug"]
        datum=c.get("/api/pick").json()
        assert datum["call"]["decision"]=="UP" and datum["call"]["checkpoint"]==15
        assert datum["observer"]["cash"]<1000
        assert datum["mode"]=="LEARN"
        assert len(read(store,"SELECT * FROM observer_rounds WHERE account='observer'"))==1
        assert len(read(store,"SELECT * FROM agreement_research"))==24
        assert not read(store,"SELECT * FROM v2_orders")
        assert not read(store,"SELECT * FROM lab_orders")
        clock[0]=start+300
        result(store,slug,clock[0],"UP")
        agreement.tick(clock[0])
        assert c.get("/api/pick").json()["call"] is None
        assert c.get("/api/pick").json()["observer"]["cash"]>1000
        table=c.get("/api/agreement-research").json()
        assert len(table)==24 and all(r["starting_cash"]==1000 and r["daily_limit"] is None and r["streak_limit"]==3 for r in table)
        assert all(r["settled_rounds"]==1 and r["wins"]==1 for r in table)
        wb=load_workbook(io.BytesIO(c.get("/api/export.xlsx").content),read_only=True)
        assert {"Observer Rules","Observer Account Ledger","Agreement Scenarios","Agreement Round Audit"}<=set(wb.sheetnames)
        assert len(list(wb["Agreement Scenarios"].values))==25
        bias = list(wb["Shadow Bias Round Audit"].values)[1:]
        assert len(bias)==6 and all(row[9:13]==("UP",1,0,1) for row in bias)
        summary = list(wb["Shadow Bias Summary"].values)[1:]
        assert len(summary)==18 and all(row[2]==1 for row in summary)
        assert "Recent Skip Reasons" in wb.sheetnames
        challenger = list(wb["Challenger Outcomes"].values)[1:]
        assert len(challenger)==6
        assert all(r[7]=="UP" and r[8] and 0<=r[9]<.25 for r in challenger)
        assert "Challenger Live Shadows" in wb.sheetnames
        assert c.post("/api/observer/stop",headers=HEADERS).status_code==200
        assert c.get("/api/pick").json()["enabled"] is False


def test_shadow_accounts_pause_and_resume_independently(tmp_path):
    store,_,now=setup(tmp_path)
    create_app(store.path,collect=False,auth_required=False)
    for i,actual in enumerate(("DOWN","DOWN","DOWN","DOWN","UP","UP")):
        t=now+i*300
        slug=f"observer-settlement-{i}"
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            observer.record(db,"observer",slug,t,t,"UP",FILL,"NO_FILL")
            observer.record(db,"control@15",slug,t,t,actual,FILL,"NO_FILL")
        result(store,slug,t+299,actual)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            observer.settle(db,t+299)
            mode=observer.status(db,"observer",t+299)["mode"]
            assert mode==("LEARN" if i in (2,3) else "OBSERVE")
    results=read(store,"SELECT pnl,reason FROM observer_rounds WHERE account='observer' ORDER BY time")
    assert [r["pnl"] for r in results]==[-1,-1,-1,None,None,1]
    assert all(r["reason"]=="LOSS_STREAK_REVIEW" for r in results[3:5])
    assert all(r[0]==1 for r in read(store,"SELECT pnl FROM observer_rounds WHERE account='control@15'"))
    app=create_app(store.path,collect=False,auth_required=False)
    assert app.state.agreement
    assert read(store,"SELECT cash FROM observer_accounts WHERE account='observer'")[0][0]==998


def test_no_daily_limit_but_unsettled_and_duplicate_guards_remain(tmp_path):
    store,_,_=setup(tmp_path)
    create_app(store.path,collect=False,auth_required=False)
    now=datetime(2026,11,22,20,tzinfo=timezone.utc).timestamp()
    with store.connect() as db:
        db.execute("""INSERT INTO observer_rounds(account,slug,time,side,status,reason,settlement_day,pnl)
                    VALUES ('observer','earlier',?,'UP','SETTLED','TEST',?,-99.30)""",
                   (now-3600,observer.local_day(now)))
        observer.record(db,"observer","capped",now,now,"UP",FILL,"NO_FILL")
        observer.record(db,"observer","capped",now,now,"UP",FILL,"NO_FILL")
        observer.record(db,"observer","still-open",now+300,now+300,"UP",FILL,"NO_FILL")
        assert observer.status(db,"observer",now)["cash"]==999
        assert observer.status(db,"observer",now)["daily_limit"] is None
    rows=read(store,"SELECT slug,reason FROM observer_rounds WHERE slug!='earlier' ORDER BY time")
    assert [(r[0],r[1]) for r in rows]==[("capped","PAPER_OBSERVATION"),("still-open","PREVIOUS_ROUND_UNSETTLED")]


def test_agreement_gates_price_and_direction_and_does_not_reopen_late(tmp_path):
    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    p,s=app.state.paper,app.state.agreement
    p.mode("LEARN",now-90)
    market_inputs(store,now,ask=.8,bid=.79)
    p.tick(now)
    s.observe(now)
    vals={r["scenario"]:r["reason"] for r in read(store,"SELECT scenario,reason FROM agreement_research WHERE checkpoint=90")}
    assert vals=={"control":"HYPOTHETICAL_FILL","agreement":"HYPOTHETICAL_FILL",
                  "edge_3":"BELOW_AFTER_COST_EDGE","edge_5":"BELOW_AFTER_COST_EDGE"}
    before=len(read(store,"SELECT * FROM observer_rounds WHERE status='OPEN'"))
    s.observe(now+300)
    assert len(read(store,"SELECT * FROM observer_rounds WHERE status='OPEN'"))==before

"""Synthetic checks for the Agreement cap and causal market-change evidence."""
from io import BytesIO
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet import viability
from prophet.app import create_app
from test_timing_update import market_inputs
from test_v2 import setup, verify


def test_agreement_cap_uses_fee_inclusive_price_and_waits_for_verification(tmp_path):
    store,_,now=setup(tmp_path)
    create_app(store.path,collect=False)
    slug=store.get("market")["slug"]
    terms=dict(theta=.095,tick=.01,quantum=.01,book_timestamp=now,book_checked=now)
    with store.connect() as db:
        for checkpoint,ask in ((45,.60),(60,.84),(90,.60)):
            book=dict(bids=[dict(price=ask-.01,quantity=300)],asks=[dict(price=ask,quantity=300)])
            for _ in range(2):
                viability.agreement_entry(db,slug,checkpoint,now,"UP",.69,9,
                    "MISSED_CHECKPOINT" if checkpoint==90 else None,book,terms)
        rows={ (r["checkpoint"],r["policy"]):r for r in viability.cap_results(db,now)}
        assert len(rows)==6
        assert rows[45,"CAP_85"]["quantity"]>0
        assert rows[60,"UNCAPPED"]["quantity"]>0
        assert rows[60,"CAP_85"]["quantity"] is None
        assert json.loads(rows[60,"CAP_85"]["evidence"])["all_in"]>.85
        assert rows[60,"CAP_85"]["reason"]=="ABOVE_85_CENT_CAP"
        assert rows[90,"CAP_85"]["reason"]=="MISSED_CHECKPOINT"
    verify(store,now,"UP")
    with store.connect() as db:
        assert all(r["outcome"] is None and r["pnl"] is None for r in viability.cap_results(db,now+299))
        assert sum(r["pnl"] is not None for r in viability.cap_results(db,now+300))==3


def test_change_alerts_are_causal_and_reported_with_matched_pnl(tmp_path, monkeypatch):
    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    model=app.state.agreement.model_paper
    app.state.paper.mode("SIMULATE",now)
    monkeypatch.setattr(model,"versions",lambda *args:{"momentum":"test-v1"})
    features={"volatility":.001}
    probability=[.5]
    monkeypatch.setattr(model,"forecast",lambda *args:({"momentum":probability[0]},
                                                        {"features":dict(features)}))
    market_inputs(store,now,bid=.48,ask=.50)
    model.tick(now)
    market_inputs(store,now+16,bid=.40,ask=.45)
    book=store.get("book")
    for row in book["bids"]+book["asks"]:
        row["quantity"]=60
    store.put("book",book)
    probability[0]=.9
    features["volatility"]=.0016
    model.tick(now+16)
    model.tick(now+16)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_market_samples").fetchone()[0]==2
        decision=db.execute("SELECT evidence FROM model_paper_decisions WHERE action='UP'").fetchone()
        flags=json.loads(decision["evidence"])["market_conditions"]
        assert set(flags["alerts"])=={"SPREAD_WIDENING","DEPTH_DROP","VOLATILITY_JUMP"}
        assert flags["comparison_seconds"]==16
    verify(store,now,"DOWN")
    model.tick(now+301)
    with store.connect() as db:
        rows=list(viability.conditions(db))
        assert len(rows)==1 and rows[0]["timing"]==105
        assert rows[0]["condition"]=="CHANGE_ALERT"
        assert rows[0]["guarded_pnl"]==0 and rows[0]["pnl"]==pytest.approx(-3,abs=.15)
        trade,=list(viability.feedback(db))
        assert trade["entry_note"]=="DIRECTION_MISSED"
        assert trade["exit_note"]=="HELD"
        assert trade["entry_brier"]>trade["market_brier"]
        assert trade["delta"]==0
    with TestClient(app) as client:
        response=client.get("/api/export.xlsx")
        assert response.status_code==200
    book=load_workbook(BytesIO(response.content),read_only=True)
    assert {"Agreement Price Cap Audit","Agreement Price Cap Results","Market Change Audit",
            "Timing and Market Conditions","Model Trade Review"} <= set(book.sheetnames)
    assert len(list(book["Timing and Market Conditions"].values))==2
    book.close()


def test_model_feedback_calibration_ignores_future_outcomes_and_other_models(tmp_path):
    store,_,now=setup(tmp_path)
    create_app(store.path,collect=False)
    with store.connect() as db:
        for i in range(22):
            slug=f"calibration-prior-{i}"
            family="momentum" if i<21 else "logistic"
            entered=now-600-i*10
            end=entered+300
            verified=now+90 if i==20 else end+1
            db.execute("INSERT INTO verified_markets VALUES (?,?,?,?,?,?,?,?,?)",
                       (slug,entered-60,end,"DOWN",100,99,verified,"SYNTHETIC","{}"))
            db.execute("""INSERT INTO model_paper_positions
                (family,slug,version,side,entered,quantity,cost,terms,entry_fee,status)
                VALUES (?,?,'v1','UP',?,5,3,'{}',.01,'SETTLED')""",(family,slug,entered))
            db.execute("""INSERT INTO model_paper_decisions
                (family,slug,time,version,up,action,reason,observation,evidence)
                VALUES (?,?,?,'v1',.7,'UP','TEST','sample','{}')""",(family,slug,entered))
        result=viability.calibrate_entry(db,"momentum","v1","next",.7,now)
        assert result["prior_entries"]==20 and result["up"]==pytest.approx(.35)
        assert viability.calibrate_entry(db,"logistic","v1","next",.7,now)["up"] is None

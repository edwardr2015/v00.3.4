"""Synthetic acceptance tests for the unified eight-model application."""
from hashlib import sha256
from io import BytesIO
import json
import math
from zipfile import ZipFile

from fastapi.testclient import TestClient
from openpyxl import load_workbook
import pytest

from prophet import operations,pattern_odds,updates
from prophet.analytics import record_market
from prophet.app import create_app
from prophet.models import BASE_PARTICIPANTS, Models,PARTICIPANTS
from test_timing_update import market_inputs
from test_v2 import setup,verify

HEADERS={"X-Prophet-Control":"local-dashboard"}


def release_fixture():
    files={"pyproject.toml":b'[project]\nname="ai-btc-prophet"\nversion="0.3.4"\n',
           "Start Prophet.command":b"#!/bin/sh\nexit 0\n",
           "prophet/__init__.py":b""}
    manifest=[dict(path=name,bytes=len(data),sha256=sha256(data).hexdigest()) for name,data in files.items()]
    files["MANIFEST.json"]=json.dumps(manifest).encode()
    handle=BytesIO()
    with ZipFile(handle,"w") as zip_file:
        for name,data in files.items():
            zip_file.writestr("ai-btc-prophet-fixture/"+name,data)
    return handle.getvalue()


def test_unified_controls_and_safe_update(tmp_path):
    app=create_app(str(tmp_path/"private.sqlite"),collect=False,auth_required=False)
    with TestClient(app) as client:
        assert "Nine-model paper leaderboard" in client.get("/").text
        assert client.get("/research-dashboard",follow_redirects=False).headers["location"]=="/"
        rows=client.get("/api/competition").json()
        assert {r["model"] for r in rows}==set(PARTICIPANTS) and len(rows)==9
        assert all(r["cash"]==1000 and r["wins"]==0 for r in rows)
        assert client.post("/api/control/start",headers=HEADERS).status_code==200
        assert client.post("/api/control/pause",headers=HEADERS).status_code==200
        assert client.get("/api/state").json()["mode"]=="OFF"
        assert client.post("/api/control/resume",headers=HEADERS).status_code==200
        assert client.get("/api/state").json()["mode"]=="SIMULATE"
        assert client.post("/api/control/collector-pause",headers=HEADERS).status_code==200
        assert app.state.paper.data_block(1800000000)=="COLLECTOR_PAUSED"
        assert client.post("/api/control/collector-resume",headers=HEADERS).status_code==200
        assert client.post("/api/control/emergency",headers=HEADERS).status_code==200
        assert client.post("/api/control/resume",headers=HEADERS).status_code==409
        assert client.post("/api/control/collector-pause",headers=HEADERS).status_code==409
        assert client.post("/api/diagnostics/diagnose",headers=HEADERS).json()["integrity"]=="ok"
        assert client.post("/api/diagnostics/reconnect",headers=HEADERS).status_code==409
        assert client.post("/api/diagnostics/alternative",headers=HEADERS).status_code==200
        data=release_fixture()
        checksum=sha256(data).hexdigest()
        assert client.post("/api/updates/test",headers={**HEADERS,"X-Prophet-Checksum":"0"*64},content=data).status_code==422
        assert client.post("/api/updates/test",headers={**HEADERS,"X-Prophet-Checksum":checksum},content=data).status_code==200
        response=client.post("/api/updates/stage/"+checksum,headers=HEADERS)
        assert response.status_code==200
        receipt=response.json()
        assert (tmp_path/"updates"/receipt["backup"]).is_file()
        assert (tmp_path/"updates"/receipt["directory"]/"Start Prophet.command").is_file()
        assert client.get("/api/state").json()["emergency"] is True
        assert client.post("/api/updates/stage/"+checksum,headers=HEADERS).status_code==409
        assert client.post("/api/updates/activate/"+checksum,headers=HEADERS).json()["restart_required"]
        assert client.post("/api/updates/activate/rollback",headers=HEADERS).json()["selection"] is None
        assert len(client.get("/api/updates").json()["history"])==4


def test_update_refuses_path_escape():
    io=BytesIO()
    with ZipFile(io,"w") as z:
        z.writestr("ai-btc-prophet-escape/../outside.txt","x")
    with pytest.raises(ValueError,match="archive path"):
        updates.verify(io.getvalue(),sha256(io.getvalue()).hexdigest())


def test_pattern_is_causal_and_independent_with_paper_results(tmp_path):
    store,_,now=setup(tmp_path)
    app=create_app(store.path,collect=False,auth_required=False)
    s=now-90
    for i in range(32):
        t=s-(i+1)*300
        is_up=i%5<4
        record_market(store,dict(slug=f"sequence-{i}",opening=100,closing=101 if is_up else 99,
            outcome="UP" if is_up else "DOWN",yes_payout=int(is_up),
            received=t+1 if i>=2 else now+100,source="SYNTHETIC CAUSAL"),t-300,t)
    p=app.state.paper
    p.mode("SIMULATE",s)
    market_inputs(store,now,bid=.23,ask=.29)
    p.tick(now)
    app.state.agreement.tick(now)
    with store.connect() as db:
        forecast=db.execute("SELECT * FROM pattern_forecasts WHERE checkpoint=90").fetchone()
        assert .02<=forecast["up"]<=.98
        detail=json.loads(forecast["evidence"])
        assert detail["sequence"]["outcomes"]==30
        baseline=db.execute("SELECT up FROM lab_predictions WHERE version='momentum-v2' AND checkpoint=90").fetchone()[0]
        assert forecast["up"]!=baseline
    context=dict(detail["indicators"],window_end=s+300,reference_price=100,volatility=.002,
                 momentum_60=1,trend_300=1,bid_depth=200,ask_depth=200,theta=.095,tick=.01,quantum=.01)
    estimate=p.continuous.selection.estimate(pattern_odds.VERSION,store.get("market")["slug"],now+1,
                 context,(context,store.get("book"),100))
    assert 0.02<=estimate<=0.98
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM lab_orders WHERE config LIKE '%pattern-odds-v1/90%'").fetchone()[0]==3
        assert db.execute("SELECT count(*) FROM lab_counterfactuals WHERE quantity IS NULL").fetchone()[0]>0
    verify(store,now,"UP")
    p.tick(now+300)
    app.state.agreement.tick(now+300)
    with TestClient(app) as c:
        summary=next(r for r in c.get("/api/competition").json() if r["model"]=="pattern_odds")
        assert summary["wins"]==1 and summary["cash"]>1000 and summary["exposure"]==0
        workbook=load_workbook(BytesIO(c.get("/api/export.xlsx").content),read_only=True)
        assert {"Pattern Odds Predictions","Nine Model Leaderboard","Sequence Analysis","SKIP Counterfactuals",
                "Repair History","Reliability and Downtime"} <= set(workbook.sheetnames)
        assert len(list(workbook["Nine Model Leaderboard"].values))==10


def test_eight_families_have_independent_paths_and_seven_artifacts_reopen(tmp_path):
    store,_,_=setup(tmp_path)
    registry=Models(store)
    rows=[]
    for i in range(360):
        x=[math.sin(i/7),math.cos(i/9),-5+.15*math.cos(i)]
        rows.append(dict(slug=f"train-{i}",decision_time=300*i,label_end=300*i+200,x=x,y=int(x[0]+x[1]*.6>0)))
    predictions={}
    for family in BASE_PARTICIPANTS:
        if family=="pattern_odds":
            continue
        version,metadata=registry.train(family,rows,now=200000)
        assert metadata["training_end"]<=metadata["validation_start"]<metadata["test_start"]
        assert 0<=metadata["test"]["brier"]<=1
        predictions[family]=Models(store).predict(version,[.25,-.15,-5])
        registry_row,_ = registry.load(version)
        from pathlib import Path
        Path(registry_row["path"]).write_bytes(b"synthetic corrupted artifact")
        with pytest.raises(ValueError,match="integrity"):
            Models(store).load(version)
    assert len(predictions)==7 and len(set(predictions.values()))>=4


def test_pattern_methods_use_bounded_chronological_windows():
    seq=list("UUUDUDDDUUUDDUDDUDUUDDDUDUUDDDUDUUUD"*2)
    observed=["UP" if c=="U" else "DOWN" for c in seq]
    experiment=list(pattern_odds.walk_forward(observed))
    assert len(experiment)==len(observed)-20
    assert len(list(pattern_odds.walk_forward(observed[:30])))==10
    assert experiment[:10]==list(pattern_odds.walk_forward(observed[:30]))
    result=pattern_odds.sequence_analysis(observed)
    assert len(result["suffixes"])==6
    assert all(abs(sum(row.values())-1)<1e-9 for row in result["transition"].values())
    assert pattern_odds.holm([.03,.001,.02])==[.04,.003,.04]
    bounds=pattern_odds.uncertainty([0,1]*20,batches=100)
    assert bounds["lower"]<=.5<=bounds["upper"]
    assert 0<=pattern_odds.change_signal(observed)["p_bound"]<=1


def test_operational_downtime_is_recorded_and_recovers(tmp_path):
    app=create_app(str(tmp_path/"private.sqlite"),collect=False,auth_required=False)
    s=app.state.store
    s.put("health_market",dict(ok=False,last_success=90))
    assert operations.sample(s,100)["incidents"][0]["resolved"] is None
    operations.sample(s,105)
    s.put("health_market",dict(ok=True,last_success=111))
    resolved=[r for r in operations.sample(s,112)["incidents"] if r["kind"]=="MARKET_FEED"]
    assert len(resolved)==1 and resolved[0]["opened"]==100 and resolved[0]["resolved"]==112

"""Deterministic fixtures for the unfunded research worker."""
import json

from fastapi.testclient import TestClient

from prophet import corrective
from prophet.app import create_app
from prophet.models import BASE_PARTICIPANTS
from test_timing_update import market_inputs
from test_v2 import setup


def panel(up_count, *, missing=()):
    members = {k:dict(version=k+"-fixture",up=.85 if i<up_count else .15)
               for i,k in enumerate(BASE_PARTICIPANTS) if k not in missing}
    return corrective.vote(members,BASE_PARTICIPANTS)


def test_thresholds_use_all_eight_real_forecasts():
    for n in (6,7,8):
        p=panel(n)
        for minimum in (6,7,8):
            assert (corrective.agreement_side(p,f"vote{minimum}")=="UP")==(n>=minimum)
    assert corrective.agreement_side(panel(8,missing=(next(iter(BASE_PARTICIPANTS)),)),"vote6") is None
    assert corrective.scores(panel(8),[(panel(8),"UP")]*19) is None
    assert corrective.scores(panel(8),[(panel(8),"UP")]*20)>.8
    assert corrective.interval(9,10)[0]<.60


def test_verify_time_and_exact_end_prevent_future_calibration(tmp_path):
    s,_,now=setup(tmp_path)
    create_app(s.path,collect=False)
    with s.connect() as db:
        db.execute("INSERT INTO corrective_rounds VALUES ('one',100,400,100)")
        db.execute("INSERT INTO corrective_panels VALUES ('one',90,190,?)",(json.dumps(panel(8)),))
        db.execute("INSERT INTO verified_markets VALUES ('one',100,400,'UP',50,51,500,'fixture','{}')")
        assert corrective.prior_panels(db,90,499)==[]
        assert len(corrective.prior_panels(db,90,501))==1
        db.execute("UPDATE verified_markets SET end=401 WHERE slug='one'")
        assert corrective.prior_panels(db,90,501)==[]


def test_quote_fill_rejects_thin_or_expensive_and_reprices_stress():
    terms=dict(theta=.095,tick=.01,quantum=.01)
    b=dict(bids=[dict(price=.49,quantity=400)],asks=[dict(price=.52,quantity=1)])
    assert corrective.choice(b,terms,"UP",.9,(.4,.7),.03)[1]=="UNDERSIZED_FILL"
    b["asks"][0]["quantity"]=400
    a,_=corrective.choice(b,terms,"UP",.9,(.4,.7),.03)
    c,_=corrective.choice(b,terms,"UP",.9,(.4,.7),.03,stress=True)
    assert a["average_price"]<c["average_price"] and a["quantity"]>c["quantity"]
    b["asks"][0]["price"]=.95
    assert corrective.choice(b,terms,"UP",.99,(.4,.7),.03)[1]=="PRICE_OUTSIDE_BAND"


def test_forward_worker_no_ledger_writes_with_real_tick(tmp_path,monkeypatch):
    s,_,now=setup(tmp_path)
    app=create_app(s.path,collect=False,auth_required=False)
    p=app.state.paper
    p.mode("SIMULATE",now)
    audit=corrective.Research(s,p)
    monkeypatch.setattr(p,"probability",lambda *_args,**_kw:
                        (.85,dict(momentum_60=.005,trend_300=.004,volatility=.001)))
    market_inputs(s,now+1,bid=.51,ask=.53)
    stale=s.get("health_reference")
    s.put("health_reference",dict(stale,checked=now-40))
    audit.tick(now+1)
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM corrective_trials").fetchone()[0]==0
    market_inputs(s,now+2,bid=.51,ask=.53)
    ref=s.get("reference")
    s.put("reference",dict(ref,price=101))
    audit.tick(now+2)
    audit.tick(now+2)
    with s.connect() as db:
        assert db.execute("SELECT count(*) FROM corrective_trials").fetchone()[0]>0
        assert db.execute("SELECT count(*) FROM v2_orders").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0]==0
        entries=db.execute("SELECT evidence,time FROM corrective_observations").fetchall()
        assert len(entries)==2
        evidence=json.loads(entries[-1]["evidence"])
        assert evidence["received"]<=evidence["decision"]
        assert "gap_agreement_40_70" in evidence["rejections"]
        for row in db.execute("SELECT payload FROM corrective_panels"):
            body=json.loads(row["payload"])
            assert set(body["missing"])==body["missing_reasons"].keys()
    with TestClient(app) as client:
        response=client.get("/api/corrective-research")
        assert response.status_code==200
        assert response.json()["verified_markets"]==0
        assert response.json()["target"]==200


def test_lightweight_challengers_and_trade_quality_use_uncertainty():
    from prophet import trade_quality
    book = dict(bids=[dict(price=.45,quantity=1000)],asks=[dict(price=.50,quantity=1000)])
    closes = [100 + i*.01 for i in range(24)]
    forecasts = trade_quality.signals(closes,101,100,180,book,[True]*15+[False]*10)
    assert set(forecasts)==set(trade_quality.CHALLENGERS)
    assert all(0<p<1 for p in forecasts.values())
    context = dict(seconds_left=120,spread=.05,disagreement=.1)
    terms=dict(theta=.095,tick=.01,quantum=.01)
    assert trade_quality.quality(book,terms,"UP",.95,None,context)[0]=="WAIT"
    assert trade_quality.quality(book,terms,"UP",.95,.51,context)[0]=="WAIT"
    assert trade_quality.quality(book,terms,"UP",.95,.8,context)[0]=="ENTER UP"
    assert trade_quality.quality(book,terms,"UP",.95,.8,dict(context,seconds_left=0))[0]=="SKIP"

"""Synthetic model selection, flip settlement and safeguards."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest

from prophet.analytics import record_market
from prophet.lab import Lab
from prophet.models import Models
from prophet.v2paper import Engine
from test_timing_update import market_inputs
from test_v2 import setup, verify


def test_flip_requires_fresh_full_liquidity_and_only_settles_once(tmp_path):
    store, engine, now = setup(tmp_path)
    engine.mode("SIMULATE", now-90)
    market_inputs(store, now, bid=.24, ask=.25)
    engine.tick(now)
    assert len(engine.summary()["orders"])==3
    assert {r["side"] for r in engine.summary()["orders"]}=={"UP"}

    engine.probability = lambda *_,**__: (.02,dict(momentum_60=-3,trend_300=-3,volatility=.002))
    # Four usable units cannot cover both the full exit and a new buy.
    market_inputs(store,now+10,bid=.60,ask=.61)
    book=store.get("book")
    book["bids"][0]["quantity"]=16
    store.put("book",book)
    engine.tick(now+10)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM shadow_paper_flips").fetchone()
    market_inputs(store,now+25,bid=.60,ask=.61)
    store.put("health_market",dict(ok=False,checked=now+25,last_success=now+10))
    engine.tick(now+25)
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM shadow_paper_flips").fetchone()
    market_inputs(store,now+26,bid=.60,ask=.61)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(engine.tick,[now+26]*8))
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM paper_flips").fetchone()
        assert db.execute("SELECT count(*) FROM shadow_paper_flips").fetchone()[0]==3
    engine.probability = lambda *_,**__: (.98,dict(momentum_60=3,trend_300=3,volatility=.002))
    market_inputs(store,now+30,bid=.24,ask=.25)
    engine.tick(now+30)
    verify(store,now,"DOWN")
    Engine(store).tick(now+300)
    Engine(store).tick(now+310)
    summary=engine.summary()
    assert len(summary["orders"])==3
    assert all(r["status"]=="SETTLED" and r["side"]=="UP" and r["flipped_side"] is None for r in summary["orders"])
    for account in [a for a in summary["accounts"] if a["id"].startswith("adaptive-")]:
        assert account["trades"]==1 and account["losses"]==1
        assert account["streaks"]["longest"]["LOSS"]==1
        assert account["balance"]==pytest.approx(1000+account["realized"])


def test_market_regime_model_selection_uses_settled_matched_rounds(tmp_path, monkeypatch):
    store, engine, now = setup(tmp_path)
    lab = Lab(store,engine)
    version="synthetic-lstm-candidate"
    created=now-90000
    with store.connect() as db:
        db.execute("INSERT INTO model_registry VALUES (?,?,?,?,?,?,?)",
                   (version,"lstm",created,"fixture","fixture","fixture","{}"))
        config=f"timing/v2/{version}/90/HOLD/1"
        db.execute("INSERT INTO lab_configs VALUES (?,?,?,?,?,?,?,?,?)",
                   (config,"lstm",version,90,"HOLD",1,"FORWARD SHADOW",1000,created))
    for i in range(20):
        slug=f"matched-{i}"
        t=now-1000-i*300
        record_market(store,dict(slug=slug,opening=100,closing=101,outcome="UP",yes_payout=1,
                                 received=t,source="SYNTHETIC"),t-300,t)
        with store.connect() as db:
            for name,pnl in ((config,.22),("timing/v2/momentum-v2/90/HOLD/1",-.15)):
                db.execute("""INSERT INTO lab_orders(config,slug,side,time,status,pnl,settled,details)
                    VALUES (?,?,'UP',?,'SETTLED',?,?,?)""",(name,slug,t-210,pnl,t,
                    json.dumps(dict(prediction=dict(volatility=.002)))))
    policy=engine.continuous.selection
    assert policy.rank("unseen",.0005,now)[1]==[]
    assert policy.rank("unseen",.002,now)[1][0]["matched"]==20
    assert policy.rank("unseen",.002,now-1001)[1]==[]
    engine.probability=lambda *a,**kw:(.8,dict(momentum_60=1,trend_300=1,volatility=.002))
    monkeypatch.setattr(Models,"predict",lambda *a,**kw:.92)
    engine.mode("SIMULATE",now-90)
    market_inputs(store,now,bid=.30,ask=.32)
    engine.tick(now)
    assert {x["model"] for x in engine.summary()["orders"]}=={version}
    with store.connect() as db:
        assert db.execute("SELECT model FROM paper_model_choices").fetchone()[0]==version

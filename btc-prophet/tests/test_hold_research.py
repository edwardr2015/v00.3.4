"""Synthetic matched paper research and failure-path regression tests."""
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet import hold_research
from prophet.adaptive_entry import opportunities
from prophet.app import create_app
from prophet.autonomous import replacement, shadow_reverse
from test_timing_update import market_inputs
from test_v2 import setup, verify

TERMS = dict(tick=.01,quantum=.01,theta=.095)


@pytest.mark.parametrize("actual,flip", [("DOWN",True),("UP",True),("DOWN",False)])
def test_same_entry_reconciles_and_excel_exports_the_counterfactual(tmp_path,actual,flip):
    store,engine,now=setup(tmp_path)
    engine.mode("SIMULATE",now-90)
    market_inputs(store,now,bid=.24,ask=.25)
    engine.tick(now)
    engine.probability=lambda *a,**kw:(.02,dict(momentum_60=-3,trend_300=-3,volatility=.002))
    market_inputs(store,now+50,bid=.60,ask=.61)
    with store.connect() as db:
        if not flip:
            db.execute("UPDATE v2_accounts SET halted='fixture' WHERE id='adaptive-3'")
    engine.tick(now+50)
    verify(store,now,actual)
    engine.tick(now+300)
    with store.connect() as db:
        report=list(hold_research.rows(db))
        assert len(report)==1
        row=report[0]
        assert row["hold_pnl"]==pytest.approx((row["quantity"] if actual=="UP" else 0)-row["cost"])
        assert row["pnl"]==pytest.approx(row["hold_pnl"])
        assert row["flip_pnl"]==pytest.approx(row["hold_pnl"]+row["flip_minus_hold"])
        assert row["entry_inputs"]["bid"]==.24
        assert row["entry_inputs"]["confirmed"]==now
        assert row["extra_hold"] is not None and row["extra_flip"] is not None
        if flip:
            assert row["flip_side"]=="DOWN" and row["flip_mode"]=="UNFUNDED SHADOW"
            assert row["flip_inputs"]["flip_probability_up"]==.02
            assert row["flip_minus_hold"]>0 if actual=="DOWN" else row["flip_minus_hold"]<0
        else:
            assert row["flip_side"] is None and row["flip_minus_hold"]==0
            assert row["extra_flip"]==row["extra_hold"]
    app=create_app(store.path,collect=False,auth_required=False)
    with TestClient(app) as client:
        response=client.get("/api/export.xlsx")
        assert response.status_code==200
        workbook=load_workbook(io.BytesIO(response.content),read_only=True)
        assert next(workbook["Hold vs Flip Summary"].iter_rows(min_row=2,values_only=True))[2]==1
        assert next(workbook["Hold vs Flip Rounds"].iter_rows(min_row=2,values_only=True))[42]==pytest.approx(row["flip_minus_hold"])


def test_close_during_fill_has_no_trade_and_missing_depth_is_not_a_zero_profit(tmp_path):
    store,engine,now=setup(tmp_path)
    engine.mode("SIMULATE",now-90)
    market_inputs(store,now,bid=.24,ask=.25)
    engine.tick(now)
    market_inputs(store,now+50,bid=.60,ask=.61)
    book=store.get("book")
    options=opportunities(book,3,.02,TERMS)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        order=db.execute("SELECT * FROM v2_orders WHERE account='adaptive-3'").fetchone()
        account=db.execute("SELECT * FROM v2_accounts WHERE id='adaptive-3'").fetchone()
        assert shadow_reverse(db,order,account,options,book,TERMS,.02,"test",now+50,.03,
                       commit_clock=lambda:None) is None
        assert not db.execute("SELECT 1 FROM paper_flips").fetchone()
        assert db.execute("SELECT balance FROM v2_accounts WHERE id='adaptive-3'").fetchone()[0]==account["balance"]

        # The flip was feasible in the base case, but its +1 tick comparison has no valid entry.
        assert shadow_reverse(db,order,account,options,book,TERMS,.02,"test",now+50,.03)
        db.execute("UPDATE hold_flip_inputs SET entry_alt_quantity=NULL,entry_alt_cost=NULL WHERE order_id=?",
                   (order["id"],))
        detail=dict(window_end=now+210,observed_at=now+50)
        hold_research.record_flip(db,order,account,book,TERMS,detail,.02,replacement)
    verify(store,now,"DOWN")
    engine.tick(now+300)
    with store.connect() as db:
        row=next(hold_research.rows(db))
        assert row["flip_alt_reason"]=="EXTRA_TICK_ENTRY_UNAVAILABLE" and row["extra_flip"] is None
        assert next(hold_research.breakdown([row]))[7:]==[0,None,None,None]


@pytest.mark.parametrize("side,book_side", [("UP","bids"),("DOWN","asks")])
def test_shared_depth_is_conserved_under_both_cost_models(side,book_side):
    book=dict(bids=[dict(price=.57,quantity=32)],asks=[dict(price=.60,quantity=32)])
    for ticks in (1,2):
        sold,bought,reason=replacement(book,side,5,3,TERMS,slippage_ticks=ticks)
        assert sold["quantity"]==5 and bought is None and reason=="UNDERSIZED_FILL"
    assert book[book_side][0]["quantity"]==32

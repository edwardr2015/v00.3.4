"""Research-only release regressions using explicitly synthetic observations."""
from openpyxl import load_workbook

from prophet import forecast_metrics, late_exit_research
from prophet.app import create_app
from prophet.v2reporting import write_research_workbook as write_quick_workbook


def test_late_exit_uses_new_full_bid_and_retains_hold_on_no_sale(tmp_path):
    app = create_app(str(tmp_path/"state.sqlite3"),collect=False,auth_required=False)
    terms = dict(theta=.095,tick=.01,quantum=.01)
    full = dict(bids=[dict(price=.75,quantity=100)],asks=[dict(price=.77,quantity=100)])
    thin = dict(full,bids=[dict(price=.75,quantity=1)])
    with app.state.store.connect() as db:
        db.execute("INSERT INTO corrective_rounds VALUES ('fixture',1000,1300,1001)")
        for cost in ("standard","higher"):
            db.execute("INSERT INTO corrective_trials VALUES ('fixture',?,?,1100,'UP',10,5,'{}')",
                       (late_exit_research.POLICY,cost))

        def attempt(t,book=full,block=None,received=None):
            late_exit_research.observe(db,"fixture",1000,1300,t,book,terms,block,
                                       {"received":received if received is not None else t})

        attempt(1239)
        attempt(1240,block="STALE")
        attempt(1241,received=1099)
        attempt(1242,book=thin)
        assert db.execute("SELECT count(*) FROM corrective_late_exits").fetchone()[0]==0
        attempt(1243)
        attempt(1244)
        assert db.execute("SELECT count(*) FROM corrective_late_exits").fetchone()[0]==2
        db.execute("INSERT INTO verified_markets VALUES ('fixture',1000,1300,'DOWN',2,1,1310,'fixture','{}')")
        assert not list(late_exit_research.results(db,1309))
        observed = list(late_exit_research.results(db,1311))
        assert all(r["hold_pnl"]==-5 and r["late_pnl"]>0 for r in observed)


def test_calibration_scores_and_correlations_use_matched_market_pairs():
    pairs=[(.8,True),(.7,True),(.2,False),(.1,True)]
    assert forecast_metrics.scores(pairs)["correct"]==3
    assert forecast_metrics.scores([(0,True)])["log_loss"]>10
    assert sum(s["count"] for s in forecast_metrics.reliability("m",pairs))==4
    rows=[({"A":{"up":p},"B":{"up":p}},outcome) for p,outcome in pairs]
    data=forecast_metrics.panel_dependence(rows)
    assert data[0]["markets"]==4 and data[0]["both_wrong"]==1
    assert abs(data[0]["error_correlation"]-1)<1e-12


def test_quick_export_shows_reasons_and_both_cost_columns(tmp_path):
    app=create_app(str(tmp_path/"state.sqlite3"),collect=False,auth_required=False)
    with app.state.store.connect() as db:
        db.execute("""INSERT INTO selective_forecasts
            (slug,start,end,observed,status,reason,side,fill_reason)
            VALUES ('fixture',1000,1300,1090,'CALL','QUALIFIED','UP','UNDERSIZED_FILL')""")
        db.execute("""INSERT INTO model_paper_positions VALUES
            ('momentum','fixture','fixture','UP',1090,8,4.6,'{}',.2,'SETTLED',1301,'UP',8,0,3.4)""")
    output=tmp_path/"quick.xlsx"
    write_quick_workbook(app.state.store.path,output)
    with output.open("rb") as source:
        report=load_workbook(source,read_only=True,data_only=True)
        assert list(report["Selective 90 Blockers"].values)[1]==("Fill","UNDERSIZED_FILL",1)
        row=list(report["Entry Pricing Audit"].values)[1]
        assert abs(row[5]-.55)<1e-10 and abs(row[6]-.575)<1e-10
        assert "Late Exit Comparison" in report.sheetnames
        report.close()

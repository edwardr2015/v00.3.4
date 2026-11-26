import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from prophet import unified
from prophet.app import create_app
from prophet.store import Store
from prophet.v2reporting import write_quick_workbook
from test_unified_v321 import TERMS, book, verify


def play(db, slug, start, outcome):
    details = dict(window_start=start, window_end=start + 300, book_checked=start + 100,
                   reference_timestamp=start + 100)
    unified.observe(db, slug, start + 100, "SIMULATE", "fixture", .9, None, details, book(), TERMS,
                    lambda: start + 100)
    verify(db, slug, start, outcome, start + 305)
    unified.settle(db, start + 310)


def test_reset_restarts_counter_but_keeps_trades_cash_and_all_time(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    with store.connect() as db:
        play(db, "a", 1000, "UP")
        play(db, "b", 2000, "DOWN")
        before = unified.summary(db, 3000)
        assert before["played"] == 2 and not before["reset"]
        unified.reset_scoreboard(db, 2500)
        after = unified.summary(db, 3000)
        assert (after["played"], after["wins"], after["losses"], after["net"]) == (0, 0, 0, 0)
        assert after["balance"] == before["balance"] and after["reset"] and after["counting_since"] == 2500
        assert after["all_time"] == dict(played=2, wins=before["wins"], losses=before["losses"], net=before["net"])
        assert db.execute("SELECT count(*) FROM unified_trades").fetchone()[0] == 2
        play(db, "c", 3000, "UP")
        later = unified.summary(db, 4000)
        assert later["played"] == 1 and later["wins"] == 1 and later["all_time"]["played"] == 3
    out = io.BytesIO()
    write_quick_workbook(store.path, out)
    rows = {r[0]: (r[1] if len(r) > 1 else None) for r in load_workbook(io.BytesIO(out.getvalue()),
            read_only=True)["Unified Scoreboard"].iter_rows(values_only=True)}
    assert rows["Games"] == 1 and rows["All-time games"] == 3 and rows["Counting since UTC"]


def test_reset_endpoint_requires_dashboard_header(tmp_path):
    store = Store(str(tmp_path / "prophet.sqlite3"))
    app = create_app(store.path, collect=False, auth_required=False)
    app.state.paper.recover_startup(1000)
    with TestClient(app) as client:
        assert client.post("/api/unified/reset-scoreboard").status_code == 403
        r = client.post("/api/unified/reset-scoreboard", headers={"X-Prophet-Control": "local-dashboard"})
        assert r.status_code == 200 and r.json()["counting_since"] > 0
        state = client.get("/api/unified").json()
        assert state["reset"] and state["played"] == 0

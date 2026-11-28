"""2.3: compact keeps trades and results, removes only dead archives, backs up first; lean mode;
research errors never halt the paper engine."""
import asyncio
import json
import os
import time

from prophet.app import create_app
from prophet.store import Store


def test_compact_removes_book_archive_keeps_trades_and_backs_up(tmp_path, monkeypatch):
    from scripts import compact
    store = Store(str(tmp_path / "prophet.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    big = json.dumps(dict(bids=[dict(price=.5, quantity=1)] * 100))
    with store.connect() as db:
        db.executemany("INSERT INTO observations(source,received,payload) VALUES ('book',?,?)", [(i, big) for i in range(3000)])
        db.execute("INSERT INTO observations(source,received,payload) VALUES ('market',1,'{\"slug\":\"m\"}')")
        db.execute("INSERT INTO verified_markets VALUES ('m',1,2,'UP',1,2,3,'f','{}')")
    monkeypatch.setenv("PROPHET_DB", store.path)
    before = os.path.getsize(store.path)
    assert compact.main(["--dry-run"]) == 0
    assert compact.main([]) == 0
    after = os.path.getsize(store.path)
    assert after < before / 2
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM observations WHERE source='book'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM observations WHERE source='market'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM verified_markets").fetchone()[0] == 1
    assert list(tmp_path.glob("backup-before-compact-*.sqlite3"))


def test_lean_mode_turns_research_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PROPHET_LEAN", "1")
    app = create_app(str(tmp_path / "p.sqlite3"), collect=False, auth_required=False)
    assert app.state.paper.smart is None


def test_research_error_never_halts_the_paper_engine(tmp_path):
    app = create_app(str(tmp_path / "p.sqlite3"), collect=False, auth_required=False)
    paper = app.state.paper
    paper.recover_startup(time.time())

    class Broken:
        def tick(self):
            raise RuntimeError("research model crashed")
    paper.smart = Broken()

    async def one_loop():
        task = asyncio.create_task(paper.loop())
        await asyncio.sleep(.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(one_loop())
    with app.state.store.connect() as db:
        assert not db.execute("SELECT 1 FROM v2_accounts WHERE halted IS NOT NULL").fetchone()
        assert db.execute("SELECT count(*) FROM events WHERE message LIKE 'Research step skipped%'").fetchone()[0] >= 1


def test_compact_refuses_while_bot_runs(tmp_path, monkeypatch):
    import fcntl
    from scripts import compact
    store = Store(str(tmp_path / "prophet.sqlite3"))
    create_app(store.path, collect=False, auth_required=False)
    monkeypatch.setenv("PROPHET_DB", store.path)
    held = open(tmp_path / "prophet.lock", "a+")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)          # what the running bot does
    try:
        assert compact.main([]) == 1
        assert not list(tmp_path.glob("backup-before-compact-*"))
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)


def test_quick_report_is_a_decision_report(tmp_path):
    from openpyxl import load_workbook
    from prophet.v2reporting import write_quick_workbook, write_research_workbook
    app = create_app(str(tmp_path / "p.sqlite3"), collect=False, auth_required=False)
    app.state.paper.recover_startup(time.time())
    out = tmp_path / "quick.xlsx"
    write_quick_workbook(app.state.store.path, out)
    wb = load_workbook(out, read_only=True, data_only=True)
    names = wb.sheetnames
    assert names[:2] == ["Start Here", "Real Money Trades"]
    for needed in ("Brain", "System Audit", "Unified Trades", "Selective $50 Account", "Feed Referee", "Fresh Data Accounts"):
        assert needed in names
    for retired in ("Entry Pricing Audit", "Recent Pooled Research", "Unified Observations", "Price Feed Check", "Recent Model Trades"):
        assert retired not in names
    items = {r[0]: r[1] for r in wb["Start Here"].iter_rows(values_only=True)}
    assert items["Real-money trading"] == "OFF" and "Version" in items
    wb.close()
    research = tmp_path / "research.xlsx"
    write_research_workbook(app.state.store.path, research)
    assert "Entry Pricing Audit" in load_workbook(research, read_only=True).sheetnames

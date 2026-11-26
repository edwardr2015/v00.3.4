import asyncio
import gzip
import json
import sqlite3

import httpx
import pytest

from prophet.alerts import Alerts
from prophet.runtime import Runtime
from test_paper import seed


def test_single_instance_lock_and_compressed_backup(tmp_path):
    s, p, now = seed(tmp_path)
    a = Runtime(s, None, p)
    b = Runtime(s, None, p)
    a.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another"):
            b.acquire()
        a.backup()
        file = next((tmp_path / "backups").glob("auto-*.gz"))
        restored = tmp_path / "restored.sqlite"
        restored.write_bytes(gzip.decompress(file.read_bytes()))
        with sqlite3.connect(restored) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 6
    finally:
        a.release()
    b.acquire()
    b.release()


def test_restart_and_model_freeze_never_silently_resume_entries(tmp_path):
    s, p, now = seed(tmp_path)
    p.mode("SIMULATE", now)

    async def restart():
        await p.start()
        await p.close()

    asyncio.run(restart())
    assert s.get("mode") == "OFF" and all(
        a["halted"] == "SERVICE_RESTART_REVIEW" for a in p.summary()["accounts"]
    )
    p.review()
    experiment = s.get("experiment")
    experiment["model_hash"] = "tampered"
    s.put("experiment", experiment)
    with pytest.raises(ValueError, match="model changed"):
        p.mode("SIMULATE", now)


def configure(monkeypatch):
    for k, v in {
        "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
        "TWILIO_AUTH_TOKEN": "synthetic-test-token",
        "TWILIO_FROM_NUMBER": "+15005550006",
        "PROPHET_ALERT_TO": "+15555550123",
    }.items():
        monkeypatch.setenv(k, v)


def test_no_provider_no_http_or_texts(tmp_path, monkeypatch):
    for k in ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PROPHET_ALERT_TO"]:
        monkeypatch.delenv(k, raising=False)
    s, p, now = seed(tmp_path)
    alerts = Alerts(s)
    p.halt("FIVE_CONSECUTIVE_LOSSES")
    alerts.enqueue()
    assert alerts.summary()["configured"] is False
    assert alerts.summary()["messages"][0]["status"] == "NOT_CONFIGURED"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected send"))
        ) as c:
            await alerts.send_pending(c)

    asyncio.run(run())


def test_sms_dedup_submission_receipt_and_uncertain_delivery(tmp_path, monkeypatch):
    configure(monkeypatch)
    s, p, now = seed(tmp_path)
    alerts = Alerts(s)
    p.halt("FIVE_CONSECUTIVE_LOSSES")
    p.halt("FIVE_CONSECUTIVE_LOSSES")
    alerts.enqueue()
    alerts.enqueue()
    assert len(alerts.summary()["messages"]) == 1
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"sid": "SM" + "2" * 32, "status": "queued"})
        return httpx.Response(200, json={"status": "delivered"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await alerts.send_pending(c)
            await alerts.send_pending(c)
            with s.connect() as db:
                db.execute("UPDATE alert_outbox SET updated=0")
            await alerts.poll(c)

    asyncio.run(run())
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert alerts.summary()["messages"][0]["status"] == "DELIVERED"
    p.halt("DIFFERENT_ERROR")
    alerts.enqueue()

    async def timeout():
        async def fail(r):
            raise httpx.ReadTimeout("synthetic timeout")

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as c:
            await alerts.send_pending(c)

    asyncio.run(timeout())
    assert alerts.summary()["messages"][0]["status"] == "UNCERTAIN"
    assert "synthetic-test-token" not in json.dumps(alerts.summary())

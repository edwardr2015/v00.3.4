"""Optional Twilio risk SMS outbox. No configured credentials means no HTTP sends."""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx


class Alerts:
    def __init__(self, store):
        self.store = store
        self.task = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS alert_outbox (
              id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, time REAL, reason TEXT,
              body TEXT, status TEXT, provider_id TEXT, error TEXT, updated REAL)""")

    def config(self):
        try:
            local = json.loads((Path(self.store.path).parent / "alerts.json").read_text())
        except (OSError, ValueError):
            local = {}
        try:
            credentials = json.loads((Path(self.store.path).parent / "sms.json").read_text())
        except (OSError, ValueError):
            credentials = {}
        return dict(
            to=os.getenv("PROPHET_ALERT_TO", credentials.get("to", local.get("to", ""))),
            sid=os.getenv("TWILIO_ACCOUNT_SID", credentials.get("sid", "")),
            token=os.getenv("TWILIO_AUTH_TOKEN", credentials.get("token", "")),
            sender=os.getenv("TWILIO_FROM_NUMBER", credentials.get("sender", "")),
        )

    def configured(self):
        c = self.config()
        return bool(
            re.fullmatch(r"AC[0-9a-fA-F]{32}", c["sid"])
            and c["token"]
            and re.fullmatch(r"\+[1-9][0-9]{7,14}", c["to"])
            and re.fullmatch(r"\+[1-9][0-9]{7,14}", c["sender"])
        )

    def enqueue(self):
        configured = self.configured()
        cursor = self.store.get("alert_cursor", 0)
        with self.store.connect() as db:
            events = db.execute(
                "SELECT * FROM risk_events WHERE id>? ORDER BY id", (cursor,)
            ).fetchall()
            for event in events:
                # One text per reason per five minutes, across six virtual accounts.
                key = f"risk:{event['reason']}:{int(event['time']) // 300}"
                body = f"BTC Prophet PAPER alert: {event['reason']}. New entries stopped for affected accounts. Review the dashboard; no real-money trades are enabled."
                db.execute(
                    "INSERT OR IGNORE INTO alert_outbox(event_key,time,reason,body,status,updated) VALUES (?,?,?,?,?,?)",
                    (
                        key,
                        event["time"],
                        event["reason"],
                        body,
                        "PENDING" if configured else "NOT_CONFIGURED",
                        time.time(),
                    ),
                )
            if events:
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES (?,?,?)",
                    ("alert_cursor", json.dumps(events[-1]["id"]), time.time()),
                )
            cursor2 = self.store.get("alert_error_cursor", 0)
            errors = db.execute(
                "SELECT * FROM events WHERE id>? AND level='ERROR' ORDER BY id", (cursor2,)
            ).fetchall()
            for event in errors:
                key = f"system:{int(event['time']) // 900}"
                db.execute(
                    "INSERT OR IGNORE INTO alert_outbox(event_key,time,reason,body,status,updated) VALUES (?,?,?,?,?,?)",
                    (
                        key,
                        event["time"],
                        "DATA_OR_SYSTEM_ERROR",
                        "BTC Prophet PAPER alert: data collector or system error. Check the dashboard. Real-money trading remains locked.",
                        "PENDING" if configured else "NOT_CONFIGURED",
                        time.time(),
                    ),
                )
            if errors:
                db.execute(
                    "INSERT OR REPLACE INTO state VALUES (?,?,?)",
                    ("alert_error_cursor", json.dumps(errors[-1]["id"]), time.time()),
                )

    async def send_pending(self, client):
        if not self.configured():
            return
        c = self.config()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM alert_outbox WHERE status='PENDING' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return
            # A crash after claiming an alert must not automatically resend an ambiguous POST.
            db.execute(
                "UPDATE alert_outbox SET status='SUBMITTING',updated=? WHERE id=?",
                (time.time(), row["id"]),
            )
        try:
            r = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{c['sid']}/Messages.json",
                data={"To": c["to"], "From": c["sender"], "Body": row["body"]},
                auth=(c["sid"], c["token"]),
            )
            if r.status_code != 201:
                # Avoid logging response bodies, credentials, URLs or phone numbers.
                self.update(row["id"], "REJECTED", error="Provider HTTP " + str(r.status_code))
                return
            payload = r.json()
            sid = payload.get("sid", "")
            if not re.fullmatch(r"SM[0-9a-fA-F]{32}", sid):
                self.update(row["id"], "UNCERTAIN", error="Unexpected provider receipt")
                return
            self.update(row["id"], "SUBMITTED", provider_id=sid)
        except (httpx.HTTPError, ValueError):
            self.update(
                row["id"],
                "UNCERTAIN",
                error="Delivery submission uncertain; inspect provider before retrying",
            )

    def update(self, id, status, provider_id=None, error=None):
        with self.store.connect() as db:
            db.execute(
                "UPDATE alert_outbox SET status=?,provider_id=coalesce(?,provider_id),error=?,updated=? WHERE id=?",
                (status, provider_id, error, time.time(), id),
            )

    async def poll(self, client):
        if not self.configured():
            return
        c = self.config()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM alert_outbox WHERE status IN ('SUBMITTED','SENT') AND updated<? LIMIT 10",
                (time.time() - 60,),
            ).fetchall()
        for row in rows:
            r = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{c['sid']}/Messages/{row['provider_id']}.json",
                auth=(c["sid"], c["token"]),
            )
            if r.status_code != 200:
                continue
            status = r.json().get("status")
            target = {
                "delivered": "DELIVERED",
                "failed": "FAILED",
                "undelivered": "UNDELIVERED",
                "sent": "SENT",
            }.get(status, "SUBMITTED")
            self.update(row["id"], target)

    def summary(self):
        c = self.config()
        with self.store.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT id,time,reason,status,error FROM alert_outbox ORDER BY id DESC LIMIT 20"
                )
            ]
        return dict(
            configured=self.configured(),
            recipient="ending " + c["to"][-4:] if c["to"] else None,
            messages=rows,
            message="SMS provider required; no texts submitted"
            if not self.configured()
            else "Configured; provider delivery status is tracked",
        )

    async def start(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE alert_outbox SET status='UNCERTAIN',error='Service restarted during submission' WHERE status='SUBMITTING'"
            )
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                try:
                    self.enqueue()
                    await self.send_pending(client)
                    await self.poll(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.put(
                        "alert_health",
                        dict(ok=False, message="SMS service error; inspect dashboard"),
                    )
                await asyncio.sleep(10)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

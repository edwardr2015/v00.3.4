"""2.1.1 watchdog: keep paper trading running after a disconnect or restart, as the owner chose.

The owner's last choice is remembered (Start/Resume = running, Stop/Pause/Emergency = stopped).
Every 30 s: if the owner chose running, paper trading is OFF, there is no emergency stop and no
safety lock, and the Polymarket price and order book are fresh again, it resumes by itself and logs
it. It never turns real-money trading on; that switch is separate and stays as the owner set it.
"""
import asyncio
import logging
import time

log = logging.getLogger(__name__)
FRESH = 15


def remember(store, action):
    if action in ("start", "formal-start", "resume"):
        store.put("owner_mode", "SIMULATE")
    elif action in ("stop", "pause", "emergency"):
        store.put("owner_mode", "OFF")


def check(store, paper, now=None):
    now = time.time() if now is None else now
    if store.get("owner_mode") != "SIMULATE" or store.get("mode", "OFF") != "OFF" or store.get("emergency", False):
        return None
    for key in ("health_reference", "health_market"):
        h = store.get(key) or {}
        if not h.get("ok") or now - (h.get("checked") or 0) > FRESH:
            return "WAITING_FOR_CONNECTION"
    try:
        paper.mode("SIMULATE")
    except ValueError as exc:
        return "BLOCKED: " + str(exc)
    store.put("paused_mode", None)
    store.event("INFO", "Watchdog: connection back, paper trading resumed as the owner chose")
    return "RESUMED"


class Watchdog:
    def __init__(self, store, paper):
        self.store, self.paper, self.task, self.last = store, paper, None, None

    async def run(self):
        while True:
            try:
                self.last = check(self.store, self.paper)
            except Exception:  # noqa: BLE001
                log.warning("Watchdog check failed", exc_info=True)
            await asyncio.sleep(30)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

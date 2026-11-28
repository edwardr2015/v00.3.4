"""Remote control: owner-requested updates from the GitHub release feed, plus restart.

Nothing checks or installs on its own. The owner presses Check for update, then Install now.

Safety rules (set by the owner):
- Paper trading only. Nothing here can place orders or enable LIVE.
- Every update is checked against its SHA-256 and the MANIFEST before staging.
- The database is backed up before any new code is staged (updates.stage).
- The ledger is never replaced: new code opens the same database after restart.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time

import httpx

from . import updates
from .operations import journal

BUILD = 20
RELEASE_NAME = "ai-btc-prophet-2.0.0"
DEFAULT_FEED = ("https://raw.githubusercontent.com/edwardr2015/v00.3.4/"
                "claude/btc-prophet-completion-9ym8j7/release/LATEST.json")
CHECK_EVERY = int(os.getenv("PROPHET_UPDATE_EVERY", "120"))
SAFE_WINDOW = (15, 60)  # seconds after each 5-minute market opens
ZIP_NAME = re.compile(r"ai-btc-prophet-[A-Za-z0-9.\-]+\.zip")
log = logging.getLogger(__name__)


def feed_url():
    return os.getenv("PROPHET_UPDATE_URL", DEFAULT_FEED)


def supervised():
    """True when the launcher will start the app again after it exits for an update."""
    return os.getenv("PROPHET_SUPERVISED") == "1"


def restart_marker(store):
    return updates.inbox(store) / "restart-requested"


def parse_feed(data, base):
    info = json.loads(data)
    build, name, digest = int(info["build"]), str(info["file"]), str(info["sha256"]).lower()
    if not ZIP_NAME.fullmatch(name) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("The update feed is malformed.")
    return dict(build=build, file=name, sha256=digest, url=base.rsplit("/", 1)[0] + "/" + name,
                notes=str(info.get("notes", ""))[:500])


def in_safe_window(now):
    second = now % 300
    return SAFE_WINDOW[0] <= second <= SAFE_WINDOW[1]


class Updater:
    def __init__(self, store, paper, fetch=None):
        self.store = store
        self.paper = paper
        self.fetch = fetch or self._fetch
        self.task = None
        self.busy = asyncio.Lock()

    # ----- state -----
    def auto(self):
        return self.store.get("auto_update", False) is True

    def set_auto(self, on):
        self.store.put("auto_update", bool(on))

    def _status(self, message, **extra):
        state = dict(self.store.get("auto_update_status", None) or {})
        state.update(extra, message=message, time=time.time())
        self.store.put("auto_update_status", state)
        log.info("Auto update: %s", message)
        return state

    def status(self):
        return dict(build=BUILD, release=RELEASE_NAME, auto=self.auto(), supervised=supervised(),
                    feed=feed_url(), **(self.store.get("auto_update_status", None) or {}))

    # ----- network -----
    async def _fetch(self, url, limit):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"Cache-Control": "no-cache"}) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("Download is larger than allowed.")
                return bytes(data)

    async def check(self):
        """Look at the feed. Downloads and verifies a newer build but does not install it."""
        url = feed_url()
        try:
            latest = parse_feed(await self.fetch(url + "?t=" + str(int(time.time())), 64 * 1024), url)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return self._status("Could not read the update feed: " + str(exc)[:200], checked=time.time())
        if latest["build"] <= BUILD:
            return self._status(f"Up to date (dev{BUILD}).", checked=time.time(), latest=latest["build"],
                                pending=None)
        try:
            data = await self.fetch(latest["url"], updates.MAX_ARCHIVE)
            descriptor = updates.test_update(self.store, data, latest["sha256"])
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return self._status("Downloaded update failed its checks: " + str(exc)[:200],
                                checked=time.time(), latest=latest["build"], pending=None)
        return self._status(f"dev{latest['build']} downloaded and verified. Press Install now when you are ready.",
                            checked=time.time(), latest=latest["build"],
                            pending=dict(latest, root=descriptor["root"]))

    # ----- install -----
    def _staged(self, digest):
        with self.store.connect() as db:
            for (details,) in db.execute(
                    "SELECT details FROM repair_history WHERE action='stage-update' ORDER BY id DESC"):
                if json.loads(details).get("sha256") == digest:
                    return True
        return False

    def install(self, pending):
        """Pause, back up, stage, select and restart. Returns the status dict."""
        digest = pending["sha256"]
        prior = self.store.get("mode", "OFF")
        paused = prior != "OFF"
        try:
            if paused:
                self.paper.mode("OFF")
                self.store.put("resume_after_update", prior)
            if not self._staged(digest):
                updates.stage(self.store, digest)
            updates.select_release(self.store, digest)
        except (OSError, ValueError) as exc:
            if paused:
                self.store.put("resume_after_update", None)
                try:
                    self.paper.mode(prior)
                except ValueError:
                    self.store.put("paused_mode", prior)
            return self._status("Install stopped, nothing changed: " + str(exc)[:200], pending=None)
        if not supervised():
            return self._status(f"dev{pending['build']} is ready. Quit and reopen Start Prophet to finish.",
                                pending=None, installed=pending["build"])
        state = self._status(f"Restarting into dev{pending['build']}.", pending=None, installed=pending["build"])
        self.restart()
        return state

    def restart(self):
        marker = restart_marker(self.store)
        marker.parent.mkdir(mode=0o700, exist_ok=True)
        marker.write_text(str(time.time()))
        with self.store.connect() as db:
            journal(db, time.time(), "remote-restart", "REQUESTED", dict(build=BUILD))
        # A plain timer thread works from any thread. (dev13-14 used asyncio.get_event_loop(),
        # which raises in a worker thread on Python 3.14, so the restart never happened.)
        timer = threading.Timer(2, os.kill, (os.getpid(), signal.SIGTERM))
        timer.daemon = True
        timer.start()

    def resume_after_restart(self):
        """After an automatic update the bot goes back to the mode it was in."""
        prior = self.store.get("resume_after_update", None)
        restart_marker(self.store).unlink(missing_ok=True)
        if not prior:
            return None
        self.store.put("resume_after_update", None)
        try:
            self.paper.mode(prior)
            self._status(f"Now running dev{BUILD}. Paper trading resumed.", installed=BUILD)
        except ValueError as exc:
            self.store.put("paused_mode", prior)
            self._status(f"Now running dev{BUILD}. Press Resume: " + str(exc)[:200], installed=BUILD)
        return prior

    async def step(self, now=None):
        async with self.busy:
            now = time.time() if now is None else now
            state = self.store.get("auto_update_status", None) or {}
            if not state.get("pending") and now - (state.get("checked") or 0) >= CHECK_EVERY:
                state = await self.check()
            pending = state.get("pending")
            if pending and self.auto() and in_safe_window(now) and not self.store.get("emergency", False):
                return self.install(pending)
            return state

    async def run(self):
        while True:
            try:
                await self.step()
            except Exception:  # keep the bot running whatever the feed does
                log.exception("Auto update step failed")
            await asyncio.sleep(10)

    def start(self):
        if os.getenv("PROPHET_AUTO_UPDATE", "0") == "1":  # off: the owner asks when ready
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

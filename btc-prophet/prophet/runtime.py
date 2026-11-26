"""Single-instance lock, bounded logs, automatic backups and collector supervision."""

import asyncio
import fcntl
import logging
import os
import gzip
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


class Runtime:
    def __init__(self, store, collector, paper):
        self.store = store
        self.collector = collector
        self.paper = paper
        self.task = None
        self.lock = None
        self.root = Path(store.path).parent
        from .operations import initialize, record_release_repairs
        initialize(store)
        record_release_repairs(store)

    def acquire(self):
        self.lock = (self.root / "prophet.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            self.lock = None
            raise RuntimeError(
                "Another Prophet process is using this data directory; use one worker"
            ) from exc
        self.lock.seek(0)
        self.lock.truncate()
        self.lock.write(str(os.getpid()))
        self.lock.flush()
        with self.store.connect() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                self.release()
                raise RuntimeError("Database integrity check failed; restore a private backup")
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logs / "prophet.log", maxBytes=5_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.handler = handler
        logging.getLogger().addHandler(handler)

    def release(self):
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None
        if hasattr(self, "handler"):
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()

    def backup(self):
        folder = self.root / "backups"
        folder.mkdir(exist_ok=True)
        from datetime import datetime, timezone

        path = folder / (datetime.now(timezone.utc).strftime("auto-%Y%m%dT%H%M%SZ.sqlite3"))
        self.store.backup(str(path))
        packed = path.with_suffix(path.suffix + ".gz")
        with path.open("rb") as source, gzip.open(packed, "wb") as target:
            shutil.copyfileobj(source, target)
        path.unlink()
        for old in sorted(folder.glob("auto-*.sqlite3.gz"))[:-48]:
            old.unlink()
        self.store.put("last_backup", dict(time=time.time(), file=packed.name))

    async def start(self):
        self.acquire()
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            try:
                if time.time() - (self.store.get("last_backup") or {}).get("time", 0) >= 3600:
                    await asyncio.to_thread(self.backup)
                if any(t.done() and not t.cancelled() for t in self.collector.tasks):
                    self.store.event("WARNING", "Collector task exited; reconnecting automatically")
                    await self.collector.restart()
                if self.paper.task and self.paper.task.done():
                    self.paper.halt("PAPER_TASK_EXITED")
                    await self.paper.start()
                from .operations import sample
                samples = sample(self.store)
                self.store.put("runtime_health",dict(ok=True,checked=time.time(),
                    free_disk_bytes=samples["free_disk_bytes"],peak_ram_bytes=samples["peak_ram_bytes"],
                    user_cpu_s=samples["user_cpu_s"],evaluation_delay_s=samples["evaluation_delay_s"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).exception("Runtime supervision failed")
                self.paper.halt("CRITICAL_SYSTEM_ERROR")
                self.store.put(
                    "runtime_health", dict(ok=False, error=str(exc), checked=time.time())
                )
            await asyncio.sleep(30)

    async def close(self, *, release=True):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if release:
            self.release()

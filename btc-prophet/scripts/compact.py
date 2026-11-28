"""Make the database smaller without losing anything the bot uses. Run it with the bot STOPPED.

    .venv/bin/python -m scripts.compact --dry-run    # only shows what it would do
    .venv/bin/python -m scripts.compact              # backs up first, then compacts

What it removes (none of it is read by the bot):
  * full order-book copies archived on every change (2.3 stopped saving them; the recorder keeps
    the top 5 levels instead)
  * second-by-second recorder rows older than 21 days, BRTI seconds older than 30 days,
    live-feed ticks older than 14 days (research history; settled results are kept)
Kept: every trade, balance, settlement, forecast, report sheet, audit and the brain's history.
A full backup copy is written next to the database first; nothing runs without enough free disk.
"""
import shutil
import sqlite3
import sys
import time
from pathlib import Path
import os

RULES = [
    ("observations", "source='book'", "full order-book copies (never read back)"),
    ("fresh_recorder", "time < :now - 21*86400", "recorder rows older than 21 days"),
    ("candles", "source='BRTI' AND time < :now - 30*86400", "BRTI seconds older than 30 days"),
    ("feed_ticks", "time < :now - 14*86400", "live-feed ticks older than 14 days"),
]


def mb(n):
    return f"{n / 1e6:,.0f} MB"


def table_sizes(db):
    try:
        return dict(db.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall())
    except sqlite3.Error:
        return {}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv
    path = Path(os.getenv("PROPHET_DB", "data/timing/prophet.sqlite3")).resolve()
    if not path.is_file():
        print("Database not found:", path)
        return 1
    wal = Path(str(path) + "-wal")
    before = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    import fcntl
    lock_path = path.parent / "prophet.lock"   # the bot holds this lock while it runs
    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("The bot is still running. Stop it first (Control + C in the Prophet window), then run this again.")
        return 1
    db = sqlite3.connect(path, timeout=2)
    print(f"Database: {path}  size {mb(before)}")
    sizes = table_sizes(db)
    if sizes:
        print("Biggest parts:")
        for name, size in list(sizes.items())[:8]:
            print(f"  {name:32s} {mb(size)}")
    now = time.time()
    plan = []
    for table, where, label in RULES:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        n = db.execute(f"SELECT count(*) FROM {table} WHERE {where}", dict(now=now)).fetchone()[0]
        plan.append((table, where, label, n))
        print(f"Will remove {n:,} rows: {label}")
    if dry:
        print("Dry run: nothing changed.")
        return 0
    free = shutil.disk_usage(path.parent).free
    if free < before * 1.3:
        print(f"Not enough free disk for the safety backup ({mb(free)} free, need about {mb(before * 1.3)}). Nothing changed.")
        return 1
    backup = path.parent / f"backup-before-compact-{time.strftime('%Y%m%d-%H%M')}.sqlite3"
    print("Backing up to", backup.name, "...")
    with sqlite3.connect(backup) as target:
        db.backup(target)
    with db:
        for table, where, label, n in plan:
            if n:
                db.execute(f"DELETE FROM {table} WHERE {where}", dict(now=now))
    print("Shrinking the file (this can take a few minutes) ...")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # write the shrink into the main file
    db.close()
    after = path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
    print(f"Done: {mb(before)} -> {mb(after)} (saved {mb(before - after)}). Backup kept: {backup.name}")
    print("When you are happy the bot runs fine, you can delete the backup file to free the space.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

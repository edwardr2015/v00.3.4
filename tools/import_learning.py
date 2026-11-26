#!/usr/bin/env python3
"""Import learning history from an old BTC Prophet database into a NEW copy of the current one.

What is imported
  * learning_labels: one per market, the 3 model features + verified outcome, from the old
    database's learning_labels and from settled predictions that were never turned into labels.
    Same eligibility rules as tools/continuity_audit.py (verified outcome, decision before the
    close, outcome received after the close, finite features).
  * optionally the history database (--history-source/--history-target): markets, 15-second
    reference bars and quotes, used by Learning.train for historical examples.

What is never touched
  * balances, accounts, trades, orders, settlements, model artifacts, state, research tables.
  * the source and target files: both are opened read-only. The result is written to --out,
    which must not exist yet. Nothing changes until you swap the files yourself.

Duplicates (same market, same outcome) are skipped. A market whose outcome disagrees between the
databases is a conflict: it is skipped and listed. Run with the app stopped or running; the
target is copied with SQLite's online backup, so the copy is consistent either way.

    python3 tools/import_learning.py \
        --source data/prophet.sqlite3 --target data/timing/prophet.sqlite3 \
        --out data/timing/prophet-imported.sqlite3 \
        --history-source data/history.sqlite3 --history-target data/timing/history.sqlite3 \
        --history-out data/timing/history-imported.sqlite3 --report import-report.json

Then: stop the app, move the current files aside (keep them), rename the -imported files to the
original names, start the app from the same folder. Rollback: put the kept files back.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from continuity_audit import connect_ro, store_examples, tables  # noqa: E402

LABEL_SCHEMA = """CREATE TABLE IF NOT EXISTS learning_labels (
    slug TEXT PRIMARY KEY, entry REAL, label_end REAL, x TEXT, y INTEGER, received REAL)"""


def copy_database(src, dest):
    dest = Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; refusing to overwrite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source, _ = connect_ro(src)
    try:
        with sqlite3.connect(dest) as out:
            source.backup(out)
    finally:
        source.close()
    return dest


def import_labels(source_path, target_path, out_path):
    src, src_resolved = connect_ro(source_path)
    try:
        candidates, rejected = {}, {}
        for ex, why in store_examples(src, "source"):
            if ex is None:
                rejected[why] = rejected.get(why, 0) + 1
                continue
            candidates.setdefault(ex["slug"], ex)
    finally:
        src.close()
    out = copy_database(target_path, out_path)
    report = dict(source=str(src_resolved), target=str(Path(target_path).resolve()), out=str(out),
                  source_eligible=len(candidates), source_rejected=rejected,
                  imported=0, duplicates=0, conflicts=[])
    with sqlite3.connect(out) as db:
        db.row_factory = sqlite3.Row
        db.execute(LABEL_SCHEMA)
        report["target_labels_before"] = db.execute("SELECT count(*) FROM learning_labels").fetchone()[0]
        existing = {r["slug"]: r["y"] for r in db.execute("SELECT slug,y FROM learning_labels")}
        verified = {}
        if "verified_markets" in tables(db):
            verified = {r["slug"]: int(r["outcome"] == "UP") for r in db.execute("SELECT slug,outcome FROM verified_markets")}
        for slug, ex in sorted(candidates.items(), key=lambda kv: kv[1]["decision_time"]):
            known = existing.get(slug, verified.get(slug))
            if known is not None and known != ex["y"]:
                report["conflicts"].append(dict(slug=slug, source_y=ex["y"], target_y=known))
                continue
            if slug in existing:
                report["duplicates"] += 1
                continue
            db.execute("INSERT INTO learning_labels VALUES (?,?,?,?,?,?)",
                       (slug, ex["decision_time"], ex["label_end"], json.dumps(ex["x"]), ex["y"], ex["received"]))
            report["imported"] += 1
        report["target_labels_after"] = db.execute("SELECT count(*) FROM learning_labels").fetchone()[0]
        report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    return report


HISTORY_TABLES = {
    "markets": ("slug", "outcome"),      # primary key, value that must agree
    "reference15": ("time", "close"),
    "quotes": (("slug", "time"), None),
}


def import_history(source_path, target_path, out_path):
    src, src_resolved = connect_ro(source_path)
    out = copy_database(target_path, out_path)
    report = dict(source=str(src_resolved), out=str(out), tables={})
    try:
        with sqlite3.connect(out) as db:
            db.row_factory = sqlite3.Row
            present = tables(db)
            for table, (key, value) in HISTORY_TABLES.items():
                if table not in tables(src) or table not in present:
                    continue
                cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                keys = key if isinstance(key, tuple) else (key,)
                where = " AND ".join(f"{k}=?" for k in keys)
                stats = dict(imported=0, duplicates=0, conflicts=0)
                for row in src.execute(f"SELECT {','.join(cols)} FROM {table}"):
                    row = dict(zip(cols, row))
                    if table == "markets" and row.get("outcome") not in (0, 1):
                        continue
                    have = db.execute(f"SELECT * FROM {table} WHERE {where}", [row[k] for k in keys]).fetchone()
                    if have is not None:
                        if value and have[value] != row[value]:
                            stats["conflicts"] += 1
                        else:
                            stats["duplicates"] += 1
                        continue
                    db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [row[c] for c in cols])
                    stats["imported"] += 1
                report["tables"][table] = stats
            report["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        src.close()
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="old store database (read-only)")
    ap.add_argument("--target", required=True, help="current store database (read-only)")
    ap.add_argument("--out", required=True, help="new combined copy (must not exist)")
    ap.add_argument("--history-source")
    ap.add_argument("--history-target")
    ap.add_argument("--history-out")
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    if any((a.history_source, a.history_target, a.history_out)) and not all(
            (a.history_source, a.history_target, a.history_out)):
        ap.error("--history-source, --history-target and --history-out go together")
    report = dict(labels=import_labels(a.source, a.target, a.out))
    if a.history_source:
        report["history"] = import_history(a.history_source, a.history_target, a.history_out)
    text = json.dumps(report, indent=2)
    print(text)
    if a.report:
        Path(a.report).write_text(text)
    return report


if __name__ == "__main__":
    main()

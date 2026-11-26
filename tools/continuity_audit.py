#!/usr/bin/env python3
"""BTC Prophet database-continuity audit (read-only).

Opens every database given on the command line in SQLite read-only mode, counts the
tables that matter for learning continuity, and computes how many *independent,
verified, feature-complete, leakage-free* five-minute markets exist across all of
them, deduplicated by market slug.

It never writes to an input database. With --export it writes a NEW isolated
dataset file (refusing to overwrite an existing file) that a training run can use.

Usage (from the Mac project folder, app stopped or running - reads are safe):

    python3 tools/continuity_audit.py \
        --store old=data/prophet.sqlite3 \
        --store new=data/timing/prophet.sqlite3 \
        --history old=data/history.sqlite3 \
        --history new=data/timing/history.sqlite3 \
        --json data/continuity-audit.json

Eligibility rules mirror prophet/learning.py and prophet/research.py (v0.2.3):
  * store DBs: rows of `learning_labels`, plus `predictions` that have an
    `outcome_<slug>` state row but were never turned into labels;
    the outcome must have been received after the decision and the decision must be
    before the market window end; features must be three finite numbers; y in {0,1}.
  * history DBs: `markets` rows with outcome in {0,1} whose 21 complete 15-second
    `reference15` bars before start+entry_offset exist (same rule as research.features).
  * A slug that carries different outcomes in different sources is a CONFLICT and is
    excluded from every count. Duplicates never count twice.
"""

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path

SLUG = re.compile(r"^cpc-btc-updown-5m-\d{4}-\d{2}-\d{2}-\d{4}z$")
COUNT_TABLES = [
    "candles", "predictions", "learning_labels", "lab_orders", "lab_predictions",
    "lab_configs", "lab_skips", "model_runs", "model_shadows", "markets", "reference15",
    "quotes", "trades", "events",
]
LEARNING_MINIMUM = 200   # prophet/learning.py: Learning.train
CHALLENGER_MINIMUM = 300  # prophet/models.py: Models.train (optional challengers)
SPLIT_MINIMUM = 30        # prophet/models.py: each of the 60/20/20 purged partitions
LSTM_MINIMUM = 2000       # prophet/models.py: fit_artifact("lstm")


def connect_ro(path):
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db, p


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def counts(db):
    present = tables(db)
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in COUNT_TABLES if t in present}


def finite_vector(x):
    return (isinstance(x, list) and len(x) == 3
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in x))


def store_examples(db, source):
    """Yield (example | None, reject_reason) from a prophet store database."""
    present = tables(db)
    seen = set()
    if "learning_labels" in present:
        for r in db.execute("SELECT * FROM learning_labels"):
            seen.add(r["slug"])
            try:
                x = json.loads(r["x"])
            except (TypeError, ValueError):
                yield None, "bad_feature_json"
                continue
            yield check(source, "learning_labels", r["slug"], r["entry"], r["label_end"],
                        r["received"], x, r["y"])
    if "predictions" in present and "state" in present:
        for p in db.execute("SELECT * FROM predictions WHERE up IS NOT NULL"):
            if p["slug"] in seen:
                continue
            o = db.execute("SELECT payload FROM state WHERE key=?",
                           ("outcome_" + p["slug"],)).fetchone()
            if not o:
                yield None, "prediction_without_outcome"
                continue
            seen.add(p["slug"])
            try:
                o, d = json.loads(o[0]), json.loads(p["details"] or "{}")
            except ValueError:
                yield None, "bad_json"
                continue
            if not all(k in d for k in ("momentum_60", "trend_300", "volatility")):
                yield None, "missing_features"
                continue
            x = [d["momentum_60"], d["trend_300"], math.log(max(d["volatility"], 1e-7))]
            yield check(source, "predictions", p["slug"], p["time"], d.get("window_end"),
                        o.get("received"), x, o.get("yes_payout"))


def check(source, table, slug, entry, label_end, received, x, y):
    if not slug or not SLUG.match(slug):
        return None, "unrecognised_slug"
    if y not in (0, 1):
        return None, "unverified_outcome"
    if entry is None or label_end is None or received is None:
        return None, "missing_timestamps"
    if entry >= label_end:
        return None, "decision_not_before_close"   # future leakage
    if received < label_end:
        return None, "outcome_before_close"        # provisional / leaked label
    if not finite_vector(x):
        return None, "missing_features"
    return dict(slug=slug, decision_time=entry, label_end=label_end, x=x, y=int(y),
                source=source, table=table), None


def history_features(bars, decision_time):
    required = list(range(decision_time - 315, decision_time, 15))
    if any(t not in bars or bars[t]["ticks"] != 15 for t in required):
        return None
    closes = [bars[t]["close"] for t in required]
    if any(c is None or c <= 0 for c in closes):
        return None
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    sigma = max(statistics.pstdev(returns), 1e-7)
    return [math.log(closes[-1] / closes[-5]) / (sigma * 2),
            math.log(closes[-1] / closes[0]) / (sigma * math.sqrt(20)), math.log(sigma)]


def history_examples(db, source, entry_offset):
    present = tables(db)
    if not {"markets", "reference15"} <= present:
        return
    bars = {r["time"]: dict(r) for r in db.execute("SELECT * FROM reference15")}
    for m in db.execute("SELECT * FROM markets ORDER BY start"):
        if m["outcome"] not in (0, 1):
            yield None, "unverified_outcome"
            continue
        entry = m["start"] + entry_offset
        x = history_features(bars, entry)
        if x is None:
            yield None, "incomplete_reference_bars"
            continue
        yield check(source, "markets", m["slug"], entry, m["end"], m["end"], x, m["outcome"])


def purged_split(rows):
    if len(rows) < 3:
        return dict(train=0, validation=0, test=0)
    a = rows[int(0.6 * len(rows))]["decision_time"]
    b = rows[int(0.8 * len(rows))]["decision_time"]
    return dict(
        train=sum(r["label_end"] <= a for r in rows),
        validation=sum(a <= r["decision_time"] and r["label_end"] <= b for r in rows),
        test=sum(r["decision_time"] >= b for r in rows),
    )


def audit(stores, histories, entry_offset=60):
    report = dict(databases=[], rejected={}, conflicts=[], read_only=True)
    by_slug = {}
    for kind, items in (("store", stores), ("history", histories)):
        for label, path in items:
            db, resolved = connect_ro(path)
            try:
                info = dict(label=label, kind=kind, path=str(resolved),
                            bytes=resolved.stat().st_size, counts=counts(db),
                            eligible=0, rejected={})
                gen = (store_examples(db, label) if kind == "store"
                       else history_examples(db, label, entry_offset))
                for ex, reason in gen:
                    if ex is None:
                        info["rejected"][reason] = info["rejected"].get(reason, 0) + 1
                        continue
                    info["eligible"] += 1
                    by_slug.setdefault(ex["slug"], []).append(ex)
                report["databases"].append(info)
            finally:
                db.close()
    final = []
    for slug, exs in by_slug.items():
        if len({e["y"] for e in exs}) > 1:
            report["conflicts"].append(dict(slug=slug, sources=[(e["source"], e["y"]) for e in exs]))
            continue
        # Prefer the earliest decision (history examples use a fixed offset).
        final.append(min(exs, key=lambda e: e["decision_time"]))
    final.sort(key=lambda e: e["decision_time"])
    n = len(final)
    split = purged_split(final)
    report.update(
        unique_eligible_markets=n,
        duplicates_removed=sum(len(v) - 1 for v in by_slug.values() if len({e["y"] for e in v}) == 1),
        by_origin={s: sum(e["source"] == s for e in final) for s in {e["source"] for e in final}},
        purged_split=split,
        gates=dict(
            learning_challenger=dict(minimum=LEARNING_MINIMUM, have=n, ready=n >= LEARNING_MINIMUM,
                                     shortage=max(0, LEARNING_MINIMUM - n)),
            optional_challengers=dict(minimum=CHALLENGER_MINIMUM, have=n,
                                      ready=n >= CHALLENGER_MINIMUM and min(split.values()) >= SPLIT_MINIMUM,
                                      shortage=max(0, CHALLENGER_MINIMUM - n)),
            lstm=dict(minimum=LSTM_MINIMUM, have=n, ready=n >= LSTM_MINIMUM,
                      shortage=max(0, LSTM_MINIMUM - n)),
        ),
        first_market=final[0]["slug"] if final else None,
        last_market=final[-1]["slug"] if final else None,
    )
    return report, final


def export(rows, destination):
    dest = Path(destination)
    if dest.exists():
        raise FileExistsError(f"{dest} exists; refusing to overwrite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dest) as db:
        db.execute("CREATE TABLE examples (slug TEXT PRIMARY KEY, decision_time REAL, "
                   "label_end REAL, x TEXT, y INTEGER, source TEXT, origin_table TEXT)")
        db.executemany("INSERT INTO examples VALUES (?,?,?,?,?,?,?)",
                       [(r["slug"], r["decision_time"], r["label_end"], json.dumps(r["x"]),
                         r["y"], r["source"], r["table"]) for r in rows])


def pair(value):
    if "=" not in value:
        return Path(value).stem, value
    label, path = value.split("=", 1)
    return label, path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", action="append", default=[], type=pair, metavar="LABEL=PATH")
    ap.add_argument("--history", action="append", default=[], type=pair, metavar="LABEL=PATH")
    ap.add_argument("--entry-offset", type=int, default=60)
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--export", help="write isolated deduplicated dataset to this NEW sqlite file")
    a = ap.parse_args(argv)
    if not a.store and not a.history:
        ap.error("give at least one --store or --history database")
    report, rows = audit(a.store, a.history, a.entry_offset)
    text = json.dumps(report, indent=2)
    print(text)
    if a.json:
        Path(a.json).write_text(text)
    if a.export:
        export(rows, a.export)
        print(f"exported {len(rows)} examples to {a.export}", file=sys.stderr)


if __name__ == "__main__":
    main()
